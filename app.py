"""
Team management portal — production web app (Flask + SQLite).
"""

from __future__ import annotations

import calendar
import csv
import difflib
import hashlib
import hmac
import re
import base64
import io
import json
import requests
import logging
import os
import secrets
import shutil
import smtplib
import subprocess
import threading
import sqlite3
import unicodedata
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

from dotenv import dotenv_values, load_dotenv

from nokia_portal_roster import NOKIA_PORTAL_DIRECTORY, lookup_portal_directory, normalize_email
from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_wtf import CSRFProtect
from flask_wtf.csrf import validate_csrf

DB_DEFAULT = Path(__file__).resolve().parent / "data" / "team_tracker.db"

_log = logging.getLogger(__name__)

# Windows: try winget once if Tesseract is missing (see _resolve_tesseract_cmd).
_tesseract_win_install_lock = threading.Lock()
_tesseract_win_install_tried = False

# Canonical roster — aligned with Nokia e-tool team grid (names as used on leave tracker / meet / audit).
EMPLOYEES: tuple[str, ...] = (
    "Akanksha Jha",
    "Anas P",
    "Archit Sugha",
    "Bharath G Krishna",
    "Dabbiru Siva Seshasai",
    "Farhan Thumalla",
    "Gajendra Singh Thakur",
    "Gumparlapati Penchala Sasi Kumar",
    "Harshitha K",
    "Jayachandra Reddy Mure",
    "Jancy Mariam Jose",
    "Maddala Satyasai",
    "Mubarak Palagiri",
    "Ramya Ure",
    "Rashmi K Nazare",
    "Sangjukta Giri",
    "Sasikumar Sampath",
    "Shaishta Anjum",
    "Shyam Bhaskar Katuri",
    "Siddhant Mandal",
    "Siya Chugh",
    "Shruthi K",
    "Sumit Patra",
    "Varshini Raj",
    "Varshitha S",
    "Zaiba Nousheen Khanum",
)

LEAVE_REASONS = [
    ("pl", "PL — Planned leave"),
    ("ul", "UL — Unplanned leave"),
    ("sl", "SL — Sick leave"),
    ("ll", "LL — Long leave"),
    ("wfh", "WFH — Work from home"),
]

# Portal apply form maps UI labels to stored `reason` codes (manager grid uses same labels dict).
PORTAL_LEAVE_FORM_CHOICES = [
    ("pl", "Annual leave"),
    ("sl", "Sick leave"),
    ("ul", "Casual leave"),
    ("wfh", "Work from home"),
]

PORTAL_DOD_CHECKLIST_LABELS: tuple[str, ...] = (
    "Code reviewed",
    "Unit tests written and passing",
    "No open blockers",
    "Acceptance criteria verified",
    "Documentation updated (if applicable)",
)

# Single calendar day: full or half; multi-day ranges use duration_type "multi".
DAY_PART_CHOICES = [
    ("full", "Full day"),
    ("half_am", "Half day — morning"),
    ("half_pm", "Half day — afternoon"),
]

DURATION_CHOICES = DAY_PART_CHOICES + [
    ("multi", "Multiple days (date range)"),
]

ATTENDANCE_STATUS = [
    ("present", "In office"),
    ("remote", "Remote / WFH"),
    ("leave", "On approved leave"),
    ("absent", "Absent (unplanned)"),
]

SCRUM_STATUS_CHOICES: tuple[tuple[str, str], ...] = (
    ("open", "Open"),
    ("doing", "In progress"),
    ("done", "Done"),
    ("blocked", "Blocked"),
)

# Kanban columns (sticky board). Sticky TYPE is the fixed set in SCRUM_BUILTIN_TASK_KIND_ROWS (`scrum_team_task_kind` rows).
SCRUM_KANBAN_COLUMNS: tuple[str, ...] = ("backlog", "do", "doing", "done")
SCRUM_LEGACY_TASK_KIND_MAP: dict[str, str] = {"story": "code", "task": "ndy", "bug": "fsy"}
_SCRUM_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

SCRUM_HOUR_EPS = 0.05
SCRUM_SPRINT_READONLY_FLASH = (
    "This sprint is closed and cannot be edited. On the sprint team page, click “Open sprint” to unlock it."
)
SCRUM_STICKY_AREA_MAX_LEN = 500
SCRUM_KANBAN_WEEKDAY_HOURS = 8.0
SCRUM_TEAM_KIND_BURN_BREAKDOWN_BELOW_PCT = 67.0
SCRUM_DONE_ARTIFACT_MAX = 20
SCRUM_DONE_ARTIFACT_URL_MAX = 600
SCRUM_DONE_ARTIFACT_LABEL_MAX = 120
SCRUM_VERIFICATION_URL_MAX = 12
SCRUM_APPRECIATION_BODY_MAX = 2000
# Fixed sprint window on create / date save: 14 calendar days inclusive of the start day (2 weeks).
SCRUM_SPRINT_DEFAULT_CALENDAR_DAYS_INCLUSIVE = 14


def scrum_sprint_default_end_date(start: date) -> date:
    """Last day of the default sprint window: N calendar days inclusive of start (N=14 → end = start + 13)."""
    return start + timedelta(days=SCRUM_SPRINT_DEFAULT_CALENDAR_DAYS_INCLUSIVE - 1)


# Default rows for scrum_team_task_kind (code, label, color_hex, sort_order). One row per team; no ad-hoc types.
SCRUM_BUILTIN_TASK_KIND_ROWS: tuple[tuple[str, str, str, int], ...] = (
    ("ndy", "NDY", "#7c3aed", 0),
    ("code", "CODE", "#eab308", 1),
    ("fsy", "FSY", "#f97316", 2),
    ("improvement", "Improvement", "#22c55e", 3),
    ("process_tools", "Process&Tools", "#06b6d4", 4),
)
SCRUM_TASK_KIND_CODES: frozenset[str] = frozenset(r[0] for r in SCRUM_BUILTIN_TASK_KIND_ROWS)

# Sprint team hero: stacked bar chart column order (labels from `scrum_team_task_kind`).
SPRINT_TEAM_KIND_STACK_ORDER: tuple[str, ...] = (
    "ndy",
    "fsy",
    "code",
    "process_tools",
    "improvement",
)

# Sprint export Summary charts: kind order for clustered est vs burnt (includes CODE).
_SUMMARY_EXPORT_KIND_CHART_CODES: tuple[str, ...] = (
    "ndy",
    "fsy",
    "code",
    "improvement",
    "process_tools",
)


def _export_coerce_task_kind(code: object) -> str:
    raw = str(code or "ndy").strip().lower()
    if raw in SCRUM_LEGACY_TASK_KIND_MAP:
        raw = SCRUM_LEGACY_TASK_KIND_MAP[raw]
    if raw not in SCRUM_TASK_KIND_CODES:
        return "ndy"
    return raw


def _export_roll_kind_est_burnt(items: list[sqlite3.Row] | list[dict]) -> tuple[dict[str, float], dict[str, float]]:
    est: dict[str, float] = {k: 0.0 for k in _SUMMARY_EXPORT_KIND_CHART_CODES}
    brn: dict[str, float] = {k: 0.0 for k in _SUMMARY_EXPORT_KIND_CHART_CODES}
    for it in items:
        k = _export_coerce_task_kind(it["task_kind"])
        if k not in est:
            k = "ndy"
        est[k] += float(it["estimate_hours"] or 0)
        brn[k] += float(it["total_burnt_hours"] or 0)
    return est, brn


def _export_roll_kind_est_burnt_assignee(items: list[sqlite3.Row] | list[dict], assignee: str) -> tuple[dict[str, float], dict[str, float]]:
    emp = (assignee or "").strip()
    sub = [it for it in items if str(it["assignee"] or "").strip() == emp]
    return _export_roll_kind_est_burnt(sub)


def _safe_done_artifact_url(u: str | None) -> str | None:
    u = (u or "").strip()
    if not u or len(u) > SCRUM_DONE_ARTIFACT_URL_MAX:
        return None
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    return u


def _normalize_done_artifacts_from_api(raw) -> tuple[str | None, str | None]:
    """Validate API payload; returns (json array string, error code or None)."""
    if raw is None:
        return "[]", None
    if not isinstance(raw, list):
        return None, "bad_artifacts"
    if len(raw) > SCRUM_DONE_ARTIFACT_MAX:
        return None, "artifacts_limit"
    out: list[dict[str, str]] = []
    for ent in raw:
        if isinstance(ent, str):
            url = _safe_done_artifact_url(ent)
            if not url:
                return None, "bad_artifact_url"
            out.append({"url": url, "label": ""})
        elif isinstance(ent, dict):
            url = _safe_done_artifact_url(str(ent.get("url") or ent.get("u") or ""))
            if not url:
                return None, "bad_artifact_url"
            label = str(ent.get("label") or ent.get("l") or "").strip()[:SCRUM_DONE_ARTIFACT_LABEL_MAX]
            out.append({"url": url, "label": label})
        else:
            return None, "bad_artifacts"
    return json.dumps(out), None


def _normalize_done_artifacts_from_lines(text: str) -> tuple[str | None, str | None]:
    raw_list: list[dict[str, str]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            label, _, rest = line.partition("|")
            raw_list.append({"label": label.strip(), "url": rest.strip()})
        else:
            raw_list.append({"label": "", "url": line})
    return _normalize_done_artifacts_from_api(raw_list)


def _normalize_verification_urls_mixed(raw) -> list[str]:
    """Normalize optional proof URLs from API (list of strings) or pasted lines (single string)."""
    out: list[str] = []
    if raw is None:
        return out
    if isinstance(raw, list):
        for ent in raw:
            u = _safe_done_artifact_url(str(ent).strip())
            if u and u not in out:
                out.append(u)
    elif isinstance(raw, str):
        for line in raw.splitlines():
            u = _safe_done_artifact_url(line.strip())
            if u and u not in out:
                out.append(u)
    return out[:SCRUM_VERIFICATION_URL_MAX]


def _parse_done_artifacts_db(raw: str | None) -> list[dict[str, str]]:
    if not raw or not str(raw).strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for ent in data:
        if not isinstance(ent, dict):
            continue
        url = _safe_done_artifact_url(str(ent.get("url") or ""))
        if not url:
            continue
        label = str(ent.get("label") or "").strip()[:SCRUM_DONE_ARTIFACT_LABEL_MAX]
        out.append({"url": url, "label": label})
    return out


def _done_artifacts_to_lines(items: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for it in items:
        u = (it.get("url") or "").strip()
        if not u:
            continue
        lb = (it.get("label") or "").strip()
        if lb:
            lines.append(f"{lb} | {u}")
        else:
            lines.append(u)
    return "\n".join(lines)


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").strip().lower()


def fuzzy_employee_matches(query: str, limit: int = 5, roster: Sequence[str] | None = None) -> list[str]:
    q = _norm(query)
    if len(q) < 1:
        return []
    roster_t = tuple(roster) if roster is not None else EMPLOYEES
    scored: list[tuple[float, str]] = []
    for e in roster_t:
        en = _norm(e)
        if not en:
            continue
        if q in en:
            scored.append((0.0, e))
            continue
        ratio = difflib.SequenceMatcher(None, q, en).ratio()
        if ratio >= 0.35:
            scored.append((1.0 - ratio, e))
    scored.sort(key=lambda x: (x[0], x[1].lower()))
    out: list[str] = []
    seen: set[str] = set()
    for _, name in scored:
        if name not in seen:
            seen.add(name)
            out.append(name)
        if len(out) >= limit:
            break
    return out


def resolve_employee_name(raw: str, roster: Sequence[str] | None = None) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    roster_t = tuple(roster) if roster is not None else EMPLOYEES
    if raw in roster_t:
        return raw
    m = difflib.get_close_matches(raw, roster_t, n=1, cutoff=0.55)
    if m:
        return m[0]
    m2 = fuzzy_employee_matches(raw, 1, roster=roster_t)
    return m2[0] if m2 else None


# Pasted Nokia / Excel grid: treat these cell values as "no leave" in Nokia.
NOKIA_EMPTY_MARKERS = frozenset({".", "-", "—", "", "0", "x", "X"})


def _resolve_nokia_row_name(name_raw: str, roster: Sequence[str] | None = None) -> str | None:
    name_raw = (name_raw or "").strip()
    if not name_raw:
        return None
    roster_t = tuple(roster) if roster is not None else EMPLOYEES
    if "," in name_raw:
        last, first = name_raw.split(",", 1)
        last, first = last.strip(), first.strip()
        if first and last:
            for candidate in (f"{first} {last}", f"{last} {first}", first, last):
                r2 = resolve_employee_name(candidate, roster=roster_t)
                if r2:
                    return r2
            m = fuzzy_employee_matches(f"{first} {last}", 1, roster=roster_t)
            if m:
                return m[0]
    r = resolve_employee_name(name_raw, roster=roster_t)
    if r:
        return r
    m = fuzzy_employee_matches(name_raw.replace(",", " "), 1, roster=roster_t)
    return m[0] if m else None


def _find_nokia_day_start_index(header_cells: list[str]) -> int | None:
    """Column index where day-of-month 1 starts (header row from Nokia / Excel)."""
    raw: list[str | None] = []
    for c in header_cells:
        s = c.strip()
        if s.isdigit():
            raw.append(str(int(s)))  # normalize 01 -> 1
        else:
            raw.append(None)

    for i, cell in enumerate(raw):
        if cell != "1":
            continue
        ok = 1
        for day in range(2, 32):
            j = i + day - 1
            if j >= len(raw):
                break
            c = raw[j]
            if c is None:
                break
            try:
                if int(c) != day:
                    break
            except ValueError:
                break
            ok += 1
        if ok >= 10:
            return i
    return None


def _grid_looks_tab_separated(text: str) -> bool:
    for line in (text or "").splitlines():
        if line.count("\t") >= 8:
            return True
    return False


def _grid_looks_csv(text: str) -> bool:
    """Comma-separated export (not tab-pasted Excel). First row may be a title line."""
    if _grid_looks_tab_separated(text):
        return False
    n = 0
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.count(",") >= 4:
            return True
        n += 1
        if n >= 24:
            break
    return False


def _csv_to_tsv(text: str) -> str:
    """Normalize CSV (possibly quoted) to tab-separated lines for parse_nokia_grid_tsv."""
    raw = (text or "").strip()
    if not raw:
        return ""
    sample = raw[:16384] if len(raw) > 16384 else raw
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    buf = io.StringIO(raw)
    reader = csv.reader(buf, dialect)
    out_lines: list[str] = []
    for row in reader:
        if not row or not any((c or "").strip() for c in row):
            continue
        out_lines.append("\t".join((c or "").strip() for c in row))
    return "\n".join(out_lines)


def _ocr_header_score(text: str) -> int:
    """Higher = more likely to contain a 1…31 style header for parse_nokia_grid_whitespace."""
    best = 0
    for ln in (text or "").replace("\r\n", "\n").split("\n"):
        parts = ln.split()
        if len(parts) < 5:
            continue
        if _find_nokia_day_start_index(parts) is not None:
            return 2000 + len(parts)
        best = max(best, sum(1 for p in parts if p.isdigit() and 1 <= int(p) <= 31))
    return best


def parse_nokia_grid_whitespace(
    text: str, year: int, month: int, roster: Sequence[str] | None = None
) -> tuple[dict[str, set[int]] | None, list[tuple[str, str]], str | None]:
    """
    Parse Nokia-like grid when cells are separated by spaces (e.g. OCR from a screenshot).
    Expects a header line containing 1, 2, 3… and data rows starting with numeric Emp ID.
    """
    roster_t = tuple(roster) if roster is not None else EMPLOYEES
    unmatched: list[tuple[str, str]] = []
    _, last_day = monthrange(year, month)
    lines = [ln for ln in (text or "").replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        return None, [], "Paste is empty."

    data_start = 0
    found_header = False
    for li, line in enumerate(lines):
        parts = line.split()
        if len(parts) < 10:
            continue
        if _find_nokia_day_start_index(parts) is not None:
            data_start = li + 1
            found_header = True
            break

    if not found_header:
        for li in range(len(lines) - 1):
            merged = f"{lines[li]} {lines[li + 1]}"
            parts = merged.split()
            if len(parts) < 10:
                continue
            if _find_nokia_day_start_index(parts) is not None:
                data_start = li + 2
                found_header = True
                break

    if not found_header:
        return None, [], (
            "Could not find day columns (1, 2, 3…) in the text read from the image. "
            "Try a larger or sharper screenshot, or paste / upload a CSV export of the same grid (comma-separated, from Excel)."
        )

    nokia_days: dict[str, set[int]] = {e: set() for e in roster_t}

    for line in lines[data_start:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        if not parts[0].isdigit():
            continue
        emp_id = parts[0]
        best_pick: tuple[int, int, str, str] | None = None  # (name_len, ds, resolved, name_raw)
        max_ds = min(len(parts), max(2, len(parts) - last_day + 6))
        for ds in range(1, max_ds):
            name_raw = " ".join(parts[1:ds]).strip()
            if not name_raw:
                continue
            resolved = _resolve_nokia_row_name(name_raw, roster=roster_t)
            if not resolved:
                continue
            remainder = len(parts) - ds
            if remainder < last_day or remainder > last_day + 10:
                continue
            pick = (len(name_raw), ds, resolved, name_raw)
            if best_pick is None or pick[:2] > best_pick[:2]:
                best_pick = pick

        if best_pick is None:
            unmatched.append((emp_id, " ".join(parts[1 : min(4, len(parts))])))
            continue
        _, ds, resolved, _name_raw = best_pick
        day_cells = parts[ds:]
        for dom in range(1, last_day + 1):
            if dom - 1 >= len(day_cells):
                break
            val = day_cells[dom - 1].strip()
            if not val or val in NOKIA_EMPTY_MARKERS:
                continue
            if val.lower() in ("w", "wo", "na", "n/a"):
                continue
            nokia_days[resolved].add(dom)

    return nokia_days, unmatched, None


def parse_nokia_grid_tsv(
    text: str, year: int, month: int, roster: Sequence[str] | None = None
) -> tuple[dict[str, set[int]] | None, list[tuple[str, str]], str | None]:
    """
    Parse tab-separated grid copied from Excel (Nokia e-tool export view).
    Returns (nokia_leave_by_roster_name -> set of day-of-month), unmatched (emp_id, raw_name), error_message.
    """
    roster_t = tuple(roster) if roster is not None else EMPLOYEES
    unmatched: list[tuple[str, str]] = []
    _, last_day = monthrange(year, month)
    lines = [ln for ln in (text or "").replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        return None, [], "Paste is empty."

    day_start_idx: int | None = None
    data_start = 0
    for li, line in enumerate(lines):
        cells = line.split("\t")
        idx = _find_nokia_day_start_index(cells)
        if idx is not None:
            day_start_idx = idx
            data_start = li + 1
            break

    if day_start_idx is None:
        for li, line in enumerate(lines):
            cells = line.split("\t")
            low0 = cells[0].strip().lower() if cells else ""
            if "emp" in low0 and "id" in low0.replace(" ", "") and len(cells) >= 10:
                day_start_idx = 2
                data_start = li + 1
                if data_start < len(lines):
                    nxt = lines[data_start].split("\t")
                    if len(nxt) > day_start_idx and not (nxt[day_start_idx].strip().isdigit()):
                        data_start += 1
                break

    if day_start_idx is None:
        return None, [], (
            "Could not find day columns (1…31). The grid text must include a header row with day numbers 1, 2, 3…"
        )

    while data_start < len(lines):
        cells = lines[data_start].split("\t")
        if len(cells) > day_start_idx + 1:
            c0, c1 = cells[0].strip(), cells[1].strip() if len(cells) > 1 else ""
            if c0.isdigit() or "," in c1 or resolve_employee_name(c1, roster=roster_t) or fuzzy_employee_matches(c1, 1, roster=roster_t):
                break
        data_start += 1

    nokia_days: dict[str, set[int]] = {e: set() for e in roster_t}

    for line in lines[data_start:]:
        cells = line.split("\t")
        if len(cells) <= day_start_idx:
            continue
        emp_id = cells[0].strip() if cells else ""
        name_raw = cells[1].strip() if len(cells) > 1 else ""
        if not name_raw and not emp_id:
            continue
        resolved = _resolve_nokia_row_name(name_raw, roster=roster_t)
        if not resolved:
            unmatched.append((emp_id or "—", name_raw or "—"))
            continue
        for dom in range(1, last_day + 1):
            ci = day_start_idx + dom - 1
            if ci >= len(cells):
                break
            val = cells[ci].strip()
            if not val or val in NOKIA_EMPTY_MARKERS:
                continue
            if val.lower() in ("w", "wo", "na", "n/a"):
                continue
            nokia_days[resolved].add(dom)

    return nokia_days, unmatched, None


def parse_nokia_grid_combined(
    text: str, year: int, month: int, roster: Sequence[str] | None = None
) -> tuple[dict[str, set[int]] | None, list[tuple[str, str]], str | None]:
    """Try tab-separated, then CSV, then whitespace / OCR-style lines."""
    t = (text or "").strip()
    if not t:
        return None, [], "Paste is empty."
    errs: list[str] = []
    if _grid_looks_tab_separated(t):
        m, u, e = parse_nokia_grid_tsv(t, year, month, roster=roster)
        if e is None:
            return m, u, e
        errs.append(e)
    if _grid_looks_csv(t):
        tsv = _csv_to_tsv(t)
        if tsv.strip():
            m, u, e = parse_nokia_grid_tsv(tsv, year, month, roster=roster)
            if e is None:
                return m, u, e
            errs.append(e)
    m2, u2, e2 = parse_nokia_grid_whitespace(t, year, month, roster=roster)
    if e2 is None:
        return m2, u2, e2
    errs.append(e2)
    return None, [], errs[0] if errs else e2


def _try_winget_install_tesseract_windows(max_wait_s: int = 600) -> bool:
    """Best-effort silent install of Tesseract via winget (Windows only)."""
    try:
        kwargs: dict = dict(
            capture_output=True,
            text=True,
            timeout=max_wait_s,
        )
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        r = subprocess.run(
            [
                "winget",
                "install",
                "-e",
                "--id",
                "UB-Mannheim.TesseractOCR",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--disable-interactivity",
            ],
            **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _log.warning("winget install Tesseract failed: %s", e)
        return False
    if r.returncode != 0:
        _log.warning(
            "winget install Tesseract exit %s: %s",
            r.returncode,
            ((r.stderr or "") + (r.stdout or ""))[:800],
        )
    return r.returncode == 0


def _resolve_tesseract_cmd() -> str | None:
    """Path to the tesseract executable: TESSERACT_CMD, PATH, then common Windows install locations."""
    env = (os.environ.get("TESSERACT_CMD") or "").strip()
    if env:
        return env
    found = shutil.which("tesseract")
    if found:
        return found

    def _windows_candidates() -> list[str]:
        cands: list[str] = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
        local = (os.environ.get("LOCALAPPDATA") or "").strip()
        if local:
            cands.append(str(Path(local) / "Programs" / "Tesseract-OCR" / "tesseract.exe"))
        pf = (os.environ.get("ProgramFiles") or "").strip()
        if pf:
            cands.append(str(Path(pf) / "Tesseract-OCR" / "tesseract.exe"))
        pfx86 = (os.environ.get("ProgramFiles(x86)") or "").strip()
        if pfx86:
            cands.append(str(Path(pfx86) / "Tesseract-OCR" / "tesseract.exe"))
        return cands

    if os.name == "nt":
        for p in _windows_candidates():
            if Path(p).is_file():
                return str(Path(p))
        auto = (os.environ.get("TEAM_TRACKER_AUTO_TESSERACT") or "1").lower() in ("1", "true", "yes")
        if auto:
            global _tesseract_win_install_tried
            with _tesseract_win_install_lock:
                if not _tesseract_win_install_tried:
                    _tesseract_win_install_tried = True
                    _try_winget_install_tesseract_windows()
            for p in _windows_candidates():
                if Path(p).is_file():
                    return str(Path(p))
    return None


def _ocr_image_to_text(raw: bytes) -> tuple[str | None, str | None]:
    """Run Tesseract on image bytes. Returns (text, None) or (None, user-facing error)."""
    try:
        from PIL import Image, ImageOps
        import pytesseract
    except ImportError:
        return None, (
            "Nokia compare needs Pillow and pytesseract on the server (pip install pillow pytesseract)."
        )
    tess = _resolve_tesseract_cmd()
    if not tess:
        return None, (
            "Tesseract OCR was not found and could not be installed automatically. On Windows with winget, "
            "run: winget install -e --id UB-Mannheim.TesseractOCR --accept-package-agreements --accept-source-agreements "
            "(or install from https://github.com/tesseract-ocr/tesseract), then set TESSERACT_CMD to tesseract.exe if needed. "
            "If automatic winget is blocked, set TEAM_TRACKER_AUTO_TESSERACT=0 and install Tesseract manually."
        )
    pytesseract.pytesseract.tesseract_cmd = tess
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except OSError as e:
        return None, f"Could not read image: {e}"
    w, h = img.size
    if w and w < 1000:
        scale = min(3, max(2, 1000 // w))
        img = img.resize((w * scale, h * scale), Image.Resampling.LANCZOS)
    gray = ImageOps.autocontrast(img.convert("L"))
    configs = [
        "--oem 3 --psm 6",
        "--oem 3 --psm 11",
        "--oem 3 --psm 4",
        "--oem 3 --psm 12",
        "--oem 3 --psm 3",
    ]
    best_txt = ""
    best_sc = -1
    last_err: str | None = None
    for cfg in configs:
        try:
            cand = (pytesseract.image_to_string(gray, config=cfg) or "").strip()
        except Exception as e:
            msg = str(e).lower()
            if "tesseract" in msg or "not installed" in msg or "the path" in msg:
                return None, (
                    "Tesseract failed to run. Check that it is installed correctly, or set TESSERACT_CMD to the full path "
                    r"of tesseract.exe (e.g. C:\Program Files\Tesseract-OCR\tesseract.exe). "
                    "Details: https://github.com/tesseract-ocr/tesseract"
                )
            last_err = str(e)
            continue
        if not cand:
            continue
        sc = _ocr_header_score(cand)
        if sc > best_sc:
            best_sc = sc
            best_txt = cand
    if best_txt:
        return best_txt, None
    if last_err:
        return None, last_err
    return None, (
        "OCR returned no readable text. Try a larger, sharper screenshot, or paste / upload a CSV export of the grid. "
        "Also verify Tesseract language data is installed (eng)."
    )


def app_leave_days_by_employee_month(
    app: Flask, year: int, month: int, roster: Sequence[str] | None = None
) -> dict[str, set[int]]:
    """Calendar days in month (1…last) where the leave tracker would show leave (pending/approved, meet-day rules)."""
    roster_t = tuple(roster) if roster is not None else EMPLOYEES
    first = date(year, month, 1)
    _, last_day = monthrange(year, month)
    last = date(year, month, last_day)
    start_iso = first.isoformat()
    end_iso = last.isoformat()
    day_dec = _load_meet_leave_day_map(app, start_iso, end_iso)

    conn = get_db(app)
    leaves = list(
        conn.execute(
            """
            SELECT * FROM leave_requests
            WHERE start_date <= ? AND end_date >= ?
              AND status IN ('pending', 'approved')
            ORDER BY id ASC
            """,
            (end_iso, start_iso),
        )
    )
    conn.close()

    by_emp: dict[str, list[sqlite3.Row]] = {e: [] for e in roster_t}
    for row in leaves:
        if row["employee_name"] in by_emp:
            by_emp[row["employee_name"]].append(row)

    out: dict[str, set[int]] = {e: set() for e in roster_t}
    for emp in roster_t:
        for dom in range(1, last_day + 1):
            d = date(year, month, dom)
            overlapping = _overlapping_leaves_for_day(by_emp[emp], d, day_dec)
            if overlapping:
                out[emp].add(dom)
    return out


def _leave_tracker_day_rows_for_range(
    app: Flask, roster: Sequence[str], sd: date, ed: date
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Per calendar day in [sd, ed], rows and totals using the same rules as the leave tracker / worksheet
    (pending + approved only, meet_leave_day removals and per-day approvals, winning row by id, half days = 0.5).
    """
    roster_t = tuple(roster)
    if not roster_t:
        return [], {}
    start_iso = sd.isoformat()
    end_iso = ed.isoformat()
    day_dec = _load_meet_leave_day_map(app, start_iso, end_iso)
    ph = ",".join("?" * len(roster_t))
    conn = get_db(app)
    leaves = list(
        conn.execute(
            f"""
            SELECT * FROM leave_requests
            WHERE start_date <= ? AND end_date >= ?
              AND status IN ('pending', 'approved')
              AND employee_name IN ({ph})
            ORDER BY id ASC
            """,
            (end_iso, start_iso, *roster_t),
        )
    )
    conn.close()

    by_emp: dict[str, list[sqlite3.Row]] = {e: [] for e in roster_t}
    for row in leaves:
        if row["employee_name"] in by_emp:
            by_emp[row["employee_name"]].append(row)

    reason_l = dict(LEAVE_REASONS)
    dur_l = dict(DURATION_CHOICES)
    detail: list[dict[str, Any]] = []
    totals: dict[str, float] = {e: 0.0 for e in roster_t}

    for emp in roster_t:
        for d in _daterange_inclusive(sd, ed):
            d_iso = d.isoformat()
            overlapping = _overlapping_leaves_for_day(by_emp[emp], d, day_dec)
            if not overlapping:
                continue
            pending_only = [r for r in overlapping if _is_effectively_pending(r, d_iso, day_dec)]
            pool = pending_only or overlapping
            row = max(pool, key=lambda r: int(r["id"]))
            eff = _effective_leave_status(row, d_iso, day_dec)
            if eff not in ("pending", "approved"):
                continue
            dur = (row["duration_type"] or "full").strip()
            if dur in ("half_am", "half_pm"):
                unit = 0.5
            else:
                unit = 1.0
            totals[emp] += unit
            detail.append(
                {
                    "employee_name": emp,
                    "leave_date": d_iso,
                    "day_units": unit,
                    "reason": row["reason"],
                    "reason_label": reason_l.get(row["reason"], row["reason"]),
                    "duration_type": dur,
                    "duration_label": dur_l.get(dur, dur),
                    "status": eff,
                    "leave_request_id": int(row["id"]),
                    "request_start": row["start_date"],
                    "request_end": row["end_date"],
                }
            )

    detail.sort(key=lambda r: (r["employee_name"], r["leave_date"], r["leave_request_id"]))
    return detail, totals


def _leave_report_overlap_window(
    leave_start_iso: str, leave_end_iso: str, win_start: date, win_end: date
) -> tuple[date, date] | None:
    """Intersection of [leave_start, leave_end] with [win_start, win_end], inclusive, or None if empty."""
    try:
        ls = date.fromisoformat(str(leave_start_iso)[:10])
        le = date.fromisoformat(str(leave_end_iso)[:10])
    except ValueError:
        return None
    if le < ls:
        ls, le = le, ls
    lo = max(ls, win_start)
    hi = min(le, win_end)
    if lo > hi:
        return None
    return lo, hi


def compare_nokia_vs_app(
    nokia_days: dict[str, set[int]],
    app_days: dict[str, set[int]],
    year: int,
    month: int,
    roster: Sequence[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Returns (in_app_not_nokia, in_nokia_not_app) as list of {employee, day, date}."""
    roster_t = tuple(roster) if roster is not None else EMPLOYEES
    _, last_day = monthrange(year, month)
    in_app_not_nokia: list[dict] = []
    in_nokia_not_app: list[dict] = []
    for emp in roster_t:
        for dom in range(1, last_day + 1):
            in_app = dom in app_days.get(emp, set())
            in_nokia = dom in nokia_days.get(emp, set())
            d_iso = date(year, month, dom).isoformat()
            if in_app and not in_nokia:
                in_app_not_nokia.append({"employee": emp, "day": dom, "date": d_iso})
            if in_nokia and not in_app:
                in_nokia_not_app.append({"employee": emp, "day": dom, "date": d_iso})
    return in_app_not_nokia, in_nokia_not_app


def client_ip() -> str:
    xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if xff:
        return xff[:200]
    return (request.remote_addr or "")[:200]


def _portal_otp_smtp_ready(app: Flask) -> bool:
    host = (app.config.get("PORTAL_OTP_SMTP_HOST") or "").strip()
    from_addr = (app.config.get("PORTAL_OTP_FROM") or "").strip()
    return bool(host and from_addr)


def _portal_otp_mail_configured(app: Flask) -> bool:
    """True when email OTP is available (SMTP + From, or local dev console delivery)."""
    if _portal_otp_smtp_ready(app):
        return True
    return bool(app.config.get("PORTAL_OTP_DEV_CONSOLE"))


def _portal_otp_code_hmac(app: Flask, code: str) -> str:
    secret = (app.config.get("SECRET_KEY") or "dev").encode("utf-8")
    return hmac.new(secret, code.strip().encode("utf-8"), hashlib.sha256).hexdigest()


def send_portal_otp_smtp(app: Flask, to_addr: str, code: str) -> None:
    """Send a one-time sign-in code (raises on SMTP failure)."""
    host = (app.config.get("PORTAL_OTP_SMTP_HOST") or "").strip()
    if not host:
        raise RuntimeError("PORTAL_OTP_SMTP_HOST is not set")
    port = int(app.config.get("PORTAL_OTP_SMTP_PORT") or 587)
    user = (app.config.get("PORTAL_OTP_SMTP_USER") or "").strip()
    password = (app.config.get("PORTAL_OTP_SMTP_PASSWORD") or "").strip()
    from_addr = (app.config.get("PORTAL_OTP_FROM") or "").strip()
    from_name = (app.config.get("PORTAL_OTP_FROM_NAME") or "TEAM MANAGEMENT PORTAL").strip()
    use_tls = bool(app.config.get("PORTAL_OTP_USE_TLS", True))
    minutes = int(app.config.get("PORTAL_OTP_TTL_MINUTES") or 15)

    msg = EmailMessage()
    msg["Subject"] = "Your TEAM MANAGEMENT PORTAL sign-in code"
    msg["From"] = f"{from_name} <{from_addr}>"
    msg["To"] = to_addr
    msg.set_content(
        f"Your one-time sign-in code is: {code}\n\n"
        f"It expires in {minutes} minutes. If you did not request this, you can ignore this email.\n"
    )

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        return
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        if use_tls:
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)


def leave_cell_code(reason: str, duration_type: str, status: str) -> str:
    """Leave tracker cell: PL/UL/SL/LL with optional ½ for half-day."""
    half = duration_type in ("half_am", "half_pm")
    suf = "\u00bd" if half else ""
    letter = {"pl": "PL", "ul": "UL", "sl": "SL", "ll": "LL"}.get(reason, "UL")
    return letter + suf


def cell_css_class(reason: str, status: str) -> str:
    base = {
        "pl": "cell-pl",
        "ul": "cell-ul",
        "sl": "cell-sl",
        "ll": "cell-ll",
    }.get(reason, "cell-ul")
    return f"{base} cell-pending" if status == "pending" else base


def _db_path(app: Flask) -> Path:
    return Path(app.config["DB_PATH"])


def get_db(app: Flask) -> sqlite3.Connection:
    p = _db_path(app)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_default_team_if_empty(conn: sqlite3.Connection) -> None:
    n = int(conn.execute("SELECT COUNT(*) AS c FROM teams").fetchone()["c"])
    if n > 0:
        return
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    cur = conn.execute("INSERT INTO teams (name, created_at) VALUES (?, ?)", ("Default", ts))
    tid = int(cur.lastrowid)
    for i, name in enumerate(EMPLOYEES):
        conn.execute(
            "INSERT INTO team_roster (team_id, employee_name, sort_order) VALUES (?, ?, ?)",
            (tid, name, i),
        )


def _migrate_scrum_team_task_kinds(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("scrum_team_task_kind_v1",)).fetchone():
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scrum_team_task_kind (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            code TEXT NOT NULL,
            label TEXT NOT NULL,
            color_hex TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            UNIQUE(team_id, code)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scrum_task_kind_team ON scrum_team_task_kind(team_id, sort_order)"
    )
    for t in conn.execute("SELECT id FROM teams"):
        tid = int(t["id"])
        for code, label, color, so in SCRUM_BUILTIN_TASK_KIND_ROWS:
            conn.execute(
                """
                INSERT OR IGNORE INTO scrum_team_task_kind (team_id, code, label, color_hex, sort_order)
                VALUES (?, ?, ?, ?, ?)
                """,
                (tid, code, label, color, so),
            )
    conn.execute("UPDATE scrum_sprint_item SET task_kind = 'code' WHERE lower(trim(task_kind)) = 'story'")
    conn.execute("UPDATE scrum_sprint_item SET task_kind = 'ndy' WHERE lower(trim(task_kind)) = 'task'")
    conn.execute("UPDATE scrum_sprint_item SET task_kind = 'fsy' WHERE lower(trim(task_kind)) = 'bug'")
    for r in conn.execute(
        """
        SELECT i.id AS iid, i.task_kind AS tc, s.team_id AS team_id
        FROM scrum_sprint_item i
        JOIN scrum_sprint s ON s.id = i.sprint_id
        """
    ):
        code = (r["tc"] or "").strip().lower()
        tid = int(r["team_id"])
        hit = conn.execute(
            "SELECT 1 FROM scrum_team_task_kind WHERE team_id = ? AND code = ?",
            (tid, code),
        ).fetchone()
        if not hit:
            conn.execute("UPDATE scrum_sprint_item SET task_kind = 'ndy' WHERE id = ?", (int(r["iid"]),))
    conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_team_task_kind_v1')")


def _migrate_scrum_task_kind_short_labels(conn: sqlite3.Connection) -> None:
    """Rename built-in kind labels from 'NDY — purple' style to short 'NDY' (one-time)."""
    if conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("scrum_task_kind_short_labels_v1",)).fetchone():
        return
    if conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='scrum_team_task_kind'"
    ).fetchone():
        pairs: tuple[tuple[str, str, str], ...] = (
            ("ndy", "NDY", "NDY — purple"),
            ("ndy", "NDY", "NDY-purple"),
            ("ndy", "NDY", "NDY - purple"),
            ("code", "CoDe", "CoDe — yellow"),
            ("code", "CoDe", "CoDe-yellow"),
            ("code", "CoDe", "CoDe - yellow"),
            ("fsy", "FSY", "FSY — orange"),
            ("fsy", "FSY", "FSY-orange"),
            ("fsy", "FSY", "FSY - orange"),
        )
        for code, new_label, old_label in pairs:
            conn.execute(
                "UPDATE scrum_team_task_kind SET label = ? WHERE code = ? AND label = ?",
                (new_label, code, old_label),
            )
    conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_task_kind_short_labels_v1')")


def _migrate_scrum_task_kind_fixed_three_v1(conn: sqlite3.Connection) -> None:
    """Keep only NDY, CODE, and FSY task types; remap stray item kinds to NDY."""
    if conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("scrum_task_kind_fixed_three_v1",)).fetchone():
        return
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='scrum_team_task_kind'"
    ).fetchone():
        conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_task_kind_fixed_three_v1')")
        return
    conn.execute(
        """
        UPDATE scrum_sprint_item SET task_kind = 'ndy'
        WHERE task_kind IS NULL OR trim(task_kind) = ''
           OR lower(trim(task_kind)) NOT IN ('ndy', 'fsy', 'code')
        """
    )
    conn.execute(
        "UPDATE scrum_sprint_item SET sticky_color_hex = NULL WHERE sticky_color_hex IS NOT NULL AND trim(sticky_color_hex) != ''"
    )
    conn.execute("DELETE FROM scrum_team_task_kind WHERE code NOT IN ('ndy', 'fsy', 'code')")
    for t in conn.execute("SELECT id FROM teams"):
        tid = int(t["id"])
        for code, label, color, so in SCRUM_BUILTIN_TASK_KIND_ROWS:
            conn.execute(
                """
                INSERT OR IGNORE INTO scrum_team_task_kind (team_id, code, label, color_hex, sort_order)
                VALUES (?, ?, ?, ?, ?)
                """,
                (tid, code, label, color, so),
            )
            conn.execute(
                """
                UPDATE scrum_team_task_kind SET label = ?, color_hex = ?, sort_order = ?
                WHERE team_id = ? AND code = ?
                """,
                (label, color, so, tid, code),
            )
    conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_task_kind_fixed_three_v1')")


def _migrate_scrum_task_kind_builtin_five_v1(conn: sqlite3.Connection) -> None:
    """Add Improvement and Process&Tools built-in kinds; keep only SCRUM_TASK_KIND_CODES in DB."""
    if conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("scrum_task_kind_builtin_five_v1",)).fetchone():
        return
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='scrum_team_task_kind'"
    ).fetchone():
        conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_task_kind_builtin_five_v1')")
        return
    allowed = tuple(sorted(SCRUM_TASK_KIND_CODES))
    ph = ",".join("?" * len(allowed))
    conn.execute(
        f"""
        UPDATE scrum_sprint_item SET task_kind = 'ndy'
        WHERE task_kind IS NULL OR trim(task_kind) = ''
           OR lower(trim(task_kind)) NOT IN ({ph})
        """,
        allowed,
    )
    conn.execute(f"DELETE FROM scrum_team_task_kind WHERE code NOT IN ({ph})", allowed)
    for t in conn.execute("SELECT id FROM teams"):
        tid = int(t["id"])
        for code, label, color, so in SCRUM_BUILTIN_TASK_KIND_ROWS:
            conn.execute(
                """
                INSERT OR IGNORE INTO scrum_team_task_kind (team_id, code, label, color_hex, sort_order)
                VALUES (?, ?, ?, ?, ?)
                """,
                (tid, code, label, color, so),
            )
            conn.execute(
                """
                UPDATE scrum_team_task_kind SET label = ?, color_hex = ?, sort_order = ?
                WHERE team_id = ? AND code = ?
                """,
                (label, color, so, tid, code),
            )
    conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_task_kind_builtin_five_v1')")


def _migrate_scrum_sprint_team_capacity_hours_v1(conn: sqlite3.Connection) -> None:
    """Store leave-adjusted team capacity (sum of roster Mon–Fri hours minus leave) on each sprint."""
    if conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("scrum_sprint_team_capacity_v1",)).fetchone():
        return
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='scrum_sprint'"
    ).fetchone():
        conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_sprint_team_capacity_v1')")
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(scrum_sprint)")}
    if "team_capacity_hours" not in cols:
        conn.execute("ALTER TABLE scrum_sprint ADD COLUMN team_capacity_hours REAL")
    conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_sprint_team_capacity_v1')")


def _migrate_scrum_sprint_is_closed_v1(conn: sqlite3.Connection) -> None:
    """Sprint lock: when is_closed=1, board and sprint metadata are read-only until a manager re-opens."""
    if conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("scrum_sprint_is_closed_v1",)).fetchone():
        return
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='scrum_sprint'"
    ).fetchone():
        conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_sprint_is_closed_v1')")
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(scrum_sprint)")}
    if "is_closed" not in cols:
        conn.execute("ALTER TABLE scrum_sprint ADD COLUMN is_closed INTEGER NOT NULL DEFAULT 0")
    conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_sprint_is_closed_v1')")


def _migrate_scrum_item_appreciation_v1(conn: sqlite3.Connection) -> None:
    """Manager 'Well Done' appreciation comments per sticky (scrum_sprint_item)."""
    if conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("scrum_item_appreciation_v1",)).fetchone():
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scrum_item_appreciation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES scrum_sprint_item(id) ON DELETE CASCADE,
            author TEXT NOT NULL DEFAULT '',
            comment TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scrum_appreciation_item ON scrum_item_appreciation(item_id, created_at DESC)"
    )
    conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_item_appreciation_v1')")


def _migrate_portal_email_otp_v1(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("portal_email_otp_v1",)).fetchone():
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS portal_email_otp (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            code_hmac TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            client_ip TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_portal_email_otp_email_created ON portal_email_otp(email, created_at DESC)"
    )
    conn.execute("INSERT INTO app_migrations (id) VALUES ('portal_email_otp_v1')")


def _migrate_scrum_portal_proposal_v1(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("scrum_portal_proposal_v1",)).fetchone():
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scrum_portal_proposal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL,
            sprint_id INTEGER NOT NULL,
            item_id INTEGER REFERENCES scrum_sprint_item(id) ON DELETE SET NULL,
            proposer_roster_name TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution_note TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scrum_portal_proposal_team_status ON scrum_portal_proposal(team_id, status, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scrum_portal_proposal_sprint ON scrum_portal_proposal(sprint_id, status)"
    )
    conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_portal_proposal_v1')")


def _migrate_scrum_item_activity_committed_hours_nullable_v1(conn: sqlite3.Connection) -> None:
    """Allow NULL committed_hours so a Do → In progress move can omit burnt hours (unset vs zero)."""
    if conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("scrum_item_activity_hours_nullable_v1",)).fetchone():
        return
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scrum_item_activity'"
    ).fetchone()
    if not row:
        conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_item_activity_hours_nullable_v1')")
        return
    notnull = 1
    for col in conn.execute("PRAGMA table_info(scrum_item_activity)"):
        if str(col[1]) == "committed_hours":
            notnull = int(col[3])
            break
    if notnull == 0:
        conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_item_activity_hours_nullable_v1')")
        return
    conn.executescript(
        """
        CREATE TABLE scrum_item_activity__m (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES scrum_sprint_item(id) ON DELETE CASCADE,
            body TEXT NOT NULL DEFAULT '',
            committed_hours REAL,
            from_column TEXT NOT NULL DEFAULT '',
            to_column TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        INSERT INTO scrum_item_activity__m (id, item_id, body, committed_hours, from_column, to_column, created_at)
            SELECT id, item_id, body, committed_hours, from_column, to_column, created_at FROM scrum_item_activity;
        DROP TABLE scrum_item_activity;
        ALTER TABLE scrum_item_activity__m RENAME TO scrum_item_activity;
        CREATE INDEX IF NOT EXISTS idx_scrum_activity_item ON scrum_item_activity(item_id, created_at DESC);
        """
    )
    conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_item_activity_hours_nullable_v1')")


def _migrate_managers_table_v1(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("managers_table_v1",)).fetchone():
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS managers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            team_name TEXT NOT NULL DEFAULT 'Default',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_managers_email ON managers(email)"
    )
    conn.execute("INSERT INTO app_migrations (id) VALUES ('managers_table_v1')")


def _migrate_teams_owner_email_v1(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("teams_owner_email_v1",)).fetchone():
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(teams)")}
    if "owner_email" not in cols:
        conn.execute("ALTER TABLE teams ADD COLUMN owner_email TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            UPDATE teams
            SET owner_email = (
                SELECT email FROM managers WHERE team_name = teams.name LIMIT 1
            )
            WHERE owner_email = '' AND EXISTS (
                SELECT 1 FROM managers WHERE team_name = teams.name
            )
            """
        )
    conn.execute("INSERT INTO app_migrations (id) VALUES ('teams_owner_email_v1')")


def _hash_manager_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 260000)
    return f"pbkdf2:sha256:260000${salt}${digest.hex()}"


def _verify_manager_password(stored_hash: str, candidate: str) -> bool:
    try:
        parts = stored_hash.split("$")
        if len(parts) != 3:
            return False
        salt = parts[1]
        expected_hex = parts[2]
        digest = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"), salt.encode("utf-8"), 260000)
        return secrets.compare_digest(digest.hex(), expected_hex)
    except Exception:
        return False


def _seed_primary_owner_manager_from_master_pin(app: Flask, conn: sqlite3.Connection) -> None:
    """If PRIMARY_OWNER_MANAGER_EMAIL and MANAGER_DASHBOARD_PASSWORD are set, create that managers row once.

    Use this to mirror the legacy master-pin flow: one email + password_hash from MANAGER_DASHBOARD_PASSWORD.
    Leave PRIMARY_OWNER_MANAGER_EMAIL unset to skip automatic seeding (register via /register or insert manually).
    """
    email = (app.config.get("PRIMARY_OWNER_MANAGER_EMAIL") or "").strip()
    if not email or "@" not in email:
        return
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='managers' LIMIT 1"
    ).fetchone():
        return
    if conn.execute("SELECT 1 FROM managers WHERE email = ? COLLATE NOCASE", (email,)).fetchone():
        return
    plain = (app.config.get("MANAGER_DASHBOARD_PASSWORD") or "").strip()
    if not plain:
        return
    team_row = conn.execute(
        "SELECT name FROM teams WHERE lower(trim(name)) = 'default' LIMIT 1"
    ).fetchone()
    if not team_row:
        team_row = conn.execute("SELECT name FROM teams ORDER BY id ASC LIMIT 1").fetchone()
    team_name = str(team_row["name"]).strip() if team_row and team_row["name"] else "Default"
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn.execute(
        "INSERT INTO managers (email, password_hash, team_name, created_at) VALUES (?, ?, ?, ?)",
        (email, _hash_manager_password(plain), team_name, ts),
    )


def init_db(app: Flask) -> None:
    conn = get_db(app)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS leave_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_name TEXT NOT NULL,
            reason TEXT NOT NULL,
            description TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            duration_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            submitted_ip TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_name TEXT NOT NULL,
            work_date TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(employee_name, work_date)
        );

        CREATE TABLE IF NOT EXISTS meet_attendance (
            employee_name TEXT NOT NULL,
            work_date TEXT NOT NULL,
            pl_verified TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (employee_name, work_date)
        );

        CREATE TABLE IF NOT EXISTS meet_leave_day (
            leave_id INTEGER NOT NULL,
            work_date TEXT NOT NULL,
            decision TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (leave_id, work_date),
            CHECK (decision IN ('approved', 'removed'))
        );

        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT '',
            hub_mode TEXT NOT NULL DEFAULT 'leave',
            owner_email TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS team_roster (
            team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            employee_name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (team_id, employee_name)
        );

        CREATE INDEX IF NOT EXISTS idx_team_roster_team ON team_roster(team_id);

        CREATE TABLE IF NOT EXISTS scrum_sprint (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            goal TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_scrum_sprint_team ON scrum_sprint(team_id, start_date);

        CREATE TABLE IF NOT EXISTS scrum_sprint_item (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sprint_id INTEGER NOT NULL REFERENCES scrum_sprint(id) ON DELETE CASCADE,
            assignee TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            estimate_hours REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            notes TEXT NOT NULL DEFAULT '',
            dod TEXT NOT NULL DEFAULT '',
            done_artifacts TEXT NOT NULL DEFAULT '[]',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            kanban_column TEXT NOT NULL DEFAULT 'backlog',
            task_kind TEXT NOT NULL DEFAULT 'task',
            area TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_scrum_item_sprint ON scrum_sprint_item(sprint_id);

        CREATE TABLE IF NOT EXISTS scrum_daily_task (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            work_date TEXT NOT NULL,
            assignee TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            planned_hours REAL NOT NULL DEFAULT 0,
            committed_hours REAL NOT NULL DEFAULT 0,
            actual_hours REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            notes TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            sprint_id INTEGER REFERENCES scrum_sprint(id) ON DELETE SET NULL,
            sprint_item_id INTEGER REFERENCES scrum_sprint_item(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_scrum_daily_team_date ON scrum_daily_task(team_id, work_date);

        CREATE TABLE IF NOT EXISTS scrum_sprint_member_goal (
            sprint_id INTEGER NOT NULL REFERENCES scrum_sprint(id) ON DELETE CASCADE,
            employee_name TEXT NOT NULL,
            goal TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (sprint_id, employee_name)
        );

        CREATE INDEX IF NOT EXISTS idx_sprint_member_goal_sprint ON scrum_sprint_member_goal(sprint_id);

        CREATE TABLE IF NOT EXISTS scrum_item_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES scrum_sprint_item(id) ON DELETE CASCADE,
            body TEXT NOT NULL DEFAULT '',
            committed_hours REAL,
            from_column TEXT NOT NULL DEFAULT '',
            to_column TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_scrum_activity_item ON scrum_item_activity(item_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS app_migrations (
            id TEXT PRIMARY KEY
        );
        """
    )
    cols_teams = {row[1] for row in conn.execute("PRAGMA table_info(teams)")}
    if "hub_mode" not in cols_teams:
        conn.execute("ALTER TABLE teams ADD COLUMN hub_mode TEXT NOT NULL DEFAULT 'leave'")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(leave_requests)")}
    if "submitted_ip" not in cols:
        conn.execute("ALTER TABLE leave_requests ADD COLUMN submitted_ip TEXT NOT NULL DEFAULT ''")
    for old, new in (
        ("vacation", "pl"),
        ("personal", "pl"),
        ("family", "ul"),
        ("bereavement", "ul"),
        ("sick", "sl"),
        ("other", "ul"),
    ):
        conn.execute("UPDATE leave_requests SET reason = ? WHERE reason = ?", (new, old))
    conn.execute(
        "UPDATE leave_requests SET reason = 'ul' WHERE reason NOT IN ('pl', 'ul', 'sl', 'll')"
    )
    cols_scrum = {row[1] for row in conn.execute("PRAGMA table_info(scrum_daily_task)")}
    if "sprint_id" not in cols_scrum:
        conn.execute(
            "ALTER TABLE scrum_daily_task ADD COLUMN sprint_id INTEGER REFERENCES scrum_sprint(id) ON DELETE SET NULL"
        )
    if "sprint_item_id" not in cols_scrum:
        conn.execute(
            "ALTER TABLE scrum_daily_task ADD COLUMN sprint_item_id INTEGER REFERENCES scrum_sprint_item(id) ON DELETE SET NULL"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scrum_daily_sprint ON scrum_daily_task(sprint_id, work_date)")
    cols_item = {row[1] for row in conn.execute("PRAGMA table_info(scrum_sprint_item)")}
    if "kanban_column" not in cols_item:
        conn.execute("ALTER TABLE scrum_sprint_item ADD COLUMN kanban_column TEXT NOT NULL DEFAULT 'backlog'")
    if "task_kind" not in cols_item:
        conn.execute("ALTER TABLE scrum_sprint_item ADD COLUMN task_kind TEXT NOT NULL DEFAULT 'task'")
    cols_item2 = {row[1] for row in conn.execute("PRAGMA table_info(scrum_sprint_item)")}
    if "sticky_color_hex" not in cols_item2:
        conn.execute("ALTER TABLE scrum_sprint_item ADD COLUMN sticky_color_hex TEXT")
    cols_item3 = {row[1] for row in conn.execute("PRAGMA table_info(scrum_sprint_item)")}
    if "dod" not in cols_item3:
        conn.execute("ALTER TABLE scrum_sprint_item ADD COLUMN dod TEXT NOT NULL DEFAULT ''")
    cols_item4 = {row[1] for row in conn.execute("PRAGMA table_info(scrum_sprint_item)")}
    if "done_artifacts" not in cols_item4:
        conn.execute("ALTER TABLE scrum_sprint_item ADD COLUMN done_artifacts TEXT NOT NULL DEFAULT '[]'")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scrum_item_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES scrum_sprint_item(id) ON DELETE CASCADE,
            body TEXT NOT NULL DEFAULT '',
            committed_hours REAL,
            from_column TEXT NOT NULL DEFAULT '',
            to_column TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scrum_activity_item ON scrum_item_activity(item_id, created_at DESC)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS app_migrations (id TEXT PRIMARY KEY)")
    if not conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("scrum_kanban_v1",)).fetchone():
        conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_kanban_v1')")
        conn.execute(
            """
            UPDATE scrum_sprint_item SET kanban_column = CASE lower(trim(status))
                WHEN 'done' THEN 'done'
                WHEN 'doing' THEN 'doing'
                WHEN 'blocked' THEN 'doing'
                ELSE 'backlog'
            END
            """
        )
    _migrate_employee_portal_v1(conn)
    _migrate_scrum_sprint_item_area_v1(conn)
    _seed_default_team_if_empty(conn)
    _seed_portal_demo_scrum_items_if_requested(conn)
    _migrate_scrum_team_task_kinds(conn)
    _migrate_scrum_task_kind_short_labels(conn)
    _migrate_scrum_task_kind_fixed_three_v1(conn)
    _migrate_scrum_task_kind_builtin_five_v1(conn)
    _migrate_scrum_sprint_team_capacity_hours_v1(conn)
    _migrate_scrum_sprint_is_closed_v1(conn)
    _migrate_scrum_item_appreciation_v1(conn)
    _migrate_portal_email_otp_v1(conn)
    _migrate_scrum_portal_proposal_v1(conn)
    _migrate_scrum_item_activity_committed_hours_nullable_v1(conn)
    _migrate_managers_table_v1(conn)
    _migrate_teams_owner_email_v1(conn)
    _seed_primary_owner_manager_from_master_pin(app, conn)
    conn.commit()
    conn.close()


def get_union_roster_for_app(app: Flask) -> tuple[str, ...]:
    """Distinct members across all teams (for public leave / typeahead)."""
    conn = get_db(app)
    rows = conn.execute(
        "SELECT DISTINCT employee_name FROM team_roster ORDER BY employee_name COLLATE NOCASE"
    ).fetchall()
    conn.close()
    return tuple(r[0] for r in rows) if rows else EMPLOYEES


def build_team_roster_export_rows(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """
    Rows for roster Excel: (TeamName, EmployeeName) for each team_roster row,
    then one Default row per seeded EMPLOYEES entry that does not appear on any team (case-insensitive).
    """
    rows = conn.execute(
        """
        SELECT t.name AS team_name, tr.employee_name AS employee_name
        FROM team_roster tr
        JOIN teams t ON t.id = tr.team_id
        ORDER BY t.name COLLATE NOCASE, tr.sort_order, tr.employee_name COLLATE NOCASE
        """
    ).fetchall()
    pairs: list[tuple[str, str]] = []
    for r in rows:
        tn = str(r["team_name"] or "").strip() or "Default"
        en = str(r["employee_name"] or "").strip()
        if en:
            pairs.append((tn, en))
    mapped_ci = {n.casefold() for _, n in pairs}
    drow = conn.execute("SELECT name FROM teams WHERE lower(trim(name)) = 'default' LIMIT 1").fetchone()
    default_label = "Default"
    if drow and (drow["name"] or "").strip():
        default_label = str(drow["name"]).strip()
    for emp in EMPLOYEES:
        e = (emp or "").strip()
        if not e or e.casefold() in mapped_ci:
            continue
        pairs.append((default_label, e))
        mapped_ci.add(e.casefold())
    return pairs


def _find_csv_col(header: list[str], candidates: tuple[str, ...]) -> int | None:
    hlow = [(i, (c or "").strip().lower()) for i, c in enumerate(header)]
    for cand in candidates:
        c = cand.lower()
        for i, h in hlow:
            if h == c or h.replace(" ", "") == c.replace(" ", ""):
                return i
    for cand in candidates:
        c = cand.lower()
        for i, h in hlow:
            if c in h:
                return i
    return None


def _roster_table_rows_to_pairs(header: list[str], data_rows: list[list[str]]) -> list[tuple[str, str]]:
    """Map tabular rows to (team_name, employee_name) using flexible header matching."""
    header = [(c or "").strip() for c in header]
    ti = _find_csv_col(header, ("teamname", "team", "team name", "squad", "group", "pod"))
    ni = _find_csv_col(header, ("employeename", "name", "employee", "member", "display name", "full name", "person"))
    if ni is None and len(header) >= 2 and ti == 0:
        ni = 1
    if ni is None and len(header) >= 2 and ti is None:
        ti, ni = 0, 1
    if ni is None:
        raise ValueError("Could not detect employee column (try headers: TeamName, EmployeeName).")

    pairs: list[tuple[str, str]] = []
    for r in data_rows:

        def cell(idx: int | None) -> str:
            if idx is None or idx >= len(r):
                return ""
            return (r[idx] or "").strip()

        emp = cell(ni)
        if not emp:
            continue
        team = cell(ti) if ti is not None else "Default"
        if not team:
            team = "Default"
        pairs.append((team, emp))
    return pairs


def ingest_team_roster_pairs(app: Flask, pairs: list[tuple[str, str]]) -> tuple[int, int, list[str]]:
    """Replace roster rows for each team present in pairs (teams are created if missing)."""
    warnings: list[str] = []
    if not pairs:
        raise ValueError("No member rows found below the header.")

    by_team: dict[str, list[str]] = {}
    for team, emp in pairs:
        by_team.setdefault(team, []).append(emp)

    conn = get_db(app)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    teams_updated = 0
    people_rows = 0
    for team_name, names in by_team.items():
        conn.execute("INSERT OR IGNORE INTO teams (name, created_at) VALUES (?, ?)", (team_name, ts))
        row = conn.execute("SELECT id FROM teams WHERE name = ?", (team_name,)).fetchone()
        if not row:
            warnings.append(f"Could not resolve team id for {team_name!r}.")
            continue
        tid = int(row["id"])
        teams_updated += 1
        conn.execute("DELETE FROM team_roster WHERE team_id = ?", (tid,))
        deduped: list[str] = []
        seen: set[str] = set()
        for n in names:
            if n not in seen:
                seen.add(n)
                deduped.append(n)
        for i, emp in enumerate(deduped):
            conn.execute(
                "INSERT INTO team_roster (team_id, employee_name, sort_order) VALUES (?, ?, ?)",
                (tid, emp, i),
            )
            people_rows += 1
    conn.commit()
    conn.close()
    return teams_updated, people_rows, warnings


def ingest_team_roster_xlsx(app: Flask, data: bytes) -> tuple[int, int, list[str]]:
    """Parse first worksheet of an .xlsx workbook; columns TeamName + EmployeeName (or legacy headers)."""
    from openpyxl import load_workbook

    if not data:
        raise ValueError("Empty file.")
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as ex:  # noqa: BLE001
        raise ValueError("Could not read Excel file (.xlsx).") from ex
    rows: list[list[str]] = []
    try:
        ws = wb.active
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= 5000:
                break
            cells = [("" if c is None else str(c)).strip() for c in row]
            if not any(cells):
                continue
            rows.append(cells)
    finally:
        wb.close()
    if not rows:
        raise ValueError("No rows in workbook.")
    header = rows[0]
    pairs = _roster_table_rows_to_pairs(header, rows[1:])
    return ingest_team_roster_pairs(app, pairs)


def ingest_team_roster_csv(app: Flask, text: str) -> tuple[int, int, list[str]]:
    """
    Replace roster rows for each team present in the CSV (teams are created if missing).
    Expected columns include team + member name (TeamName / EmployeeName, or Team / Name, etc.).
    Returns (teams_updated, people_rows, warnings).
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Empty file.")
    sample = raw[:8192] if len(raw) > 8192 else raw
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(raw), dialect)
    rows = [r for r in reader if r and any((c or "").strip() for c in r)]
    if not rows:
        raise ValueError("No rows in CSV.")
    header = [(c or "").strip() for c in rows[0]]
    pairs = _roster_table_rows_to_pairs(header, rows[1:])
    if not pairs:
        raise ValueError("No member rows found below the header.")
    return ingest_team_roster_pairs(app, pairs)


def _safe_next_path(nxt: str | None) -> str | None:
    if not nxt:
        return None
    u = urlparse(nxt)
    if u.scheme or u.netloc:
        return None
    path = u.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return None
    return path + (f"?{u.query}" if u.query else "")


def _portal_session() -> dict | None:
    raw = session.get("portal_user")
    if not isinstance(raw, dict):
        return None
    return raw


def _portal_dod_default_json() -> str:
    return json.dumps([False] * len(PORTAL_DOD_CHECKLIST_LABELS))


def _portal_dod_parse(raw: str | None) -> list[bool]:
    n = len(PORTAL_DOD_CHECKLIST_LABELS)
    try:
        data = json.loads(raw or "[]")
        if not isinstance(data, list):
            return [False] * n
        return [bool(data[i]) if i < len(data) else False for i in range(n)]
    except Exception:
        return [False] * n


def _portal_dod_all_done(states: list[bool]) -> bool:
    n = len(PORTAL_DOD_CHECKLIST_LABELS)
    return len(states) == n and all(states)


def _working_weekdays_count(sd: date, ed: date) -> int:
    return sum(1 for d in _daterange_inclusive(sd, ed) if d.weekday() < 5)


def _portal_status_to_kanban(ps: str) -> str:
    x = (ps or "").strip().lower()
    if x == "progress":
        return "doing"
    if x == "done":
        return "done"
    return "backlog"


def _kanban_to_portal_status(col: str | None) -> str:
    c = _normalize_kanban_column(col)
    if c == "done":
        return "done"
    if c in ("doing", "do"):
        return "progress"
    return "todo"


def _migrate_scrum_sprint_item_area_v1(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("scrum_sprint_item_area_v1",)).fetchone():
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(scrum_sprint_item)")}
    if "area" not in cols:
        conn.execute("ALTER TABLE scrum_sprint_item ADD COLUMN area TEXT NOT NULL DEFAULT ''")
    conn.execute("INSERT INTO app_migrations (id) VALUES ('scrum_sprint_item_area_v1')")


def _migrate_employee_portal_v1(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("employee_portal_v1",)).fetchone():
        return
    cols_leave = {row[1] for row in conn.execute("PRAGMA table_info(leave_requests)")}
    if "submitted_email" not in cols_leave:
        conn.execute(
            "ALTER TABLE leave_requests ADD COLUMN submitted_email TEXT NOT NULL DEFAULT ''"
        )
    if "portal_leave_label" not in cols_leave:
        conn.execute(
            "ALTER TABLE leave_requests ADD COLUMN portal_leave_label TEXT NOT NULL DEFAULT ''"
        )
    cols_item = {row[1] for row in conn.execute("PRAGMA table_info(scrum_sprint_item)")}
    if "portal_dod_json" not in cols_item:
        conn.execute(
            "ALTER TABLE scrum_sprint_item ADD COLUMN portal_dod_json TEXT NOT NULL DEFAULT '[]'"
        )
    conn.execute("INSERT INTO app_migrations (id) VALUES ('employee_portal_v1')")


def _seed_portal_demo_scrum_items_if_requested(conn: sqlite3.Connection) -> None:
    """Optional demo stickies (3 per directory member) when PORTAL_SEED_DEMO_ITEMS=1."""
    flag = os.environ.get("PORTAL_SEED_DEMO_ITEMS", "").strip().lower()
    if flag not in ("1", "true", "yes"):
        return
    if conn.execute("SELECT 1 FROM app_migrations WHERE id = ?", ("portal_seed_demo_v1",)).fetchone():
        return
    team = conn.execute("SELECT id FROM teams ORDER BY id LIMIT 1").fetchone()
    if not team:
        return
    team_id = int(team["id"])
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    sp = conn.execute(
        "SELECT id FROM scrum_sprint WHERE team_id = ? AND name = ?",
        (team_id, "Portal demo seed"),
    ).fetchone()
    if sp:
        sid = int(sp["id"])
    else:
        cur = conn.execute(
            """
            INSERT INTO scrum_sprint (team_id, name, start_date, end_date, goal, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                team_id,
                "Portal demo seed",
                date.today().isoformat(),
                (date.today() + timedelta(days=14)).isoformat(),
                "Auto-seeded for Microsoft portal demo",
                ts,
                ts,
            ),
        )
        sid = int(cur.lastrowid)
    titles = ("Sample task A", "Sample task B", "Sample task C")
    for _em, _disp, roster in NOKIA_PORTAL_DIRECTORY:
        for idx, title in enumerate(titles):
            conn.execute(
                """
                INSERT INTO scrum_sprint_item
                (sprint_id, assignee, title, estimate_hours, status, notes, dod, done_artifacts,
                 sort_order, created_at, updated_at, kanban_column, task_kind, portal_dod_json)
                VALUES (?, ?, ?, 4.0, 'open', '', '', '[]', ?, ?, ?, 'backlog', 'ndy', ?)
                """,
                (sid, roster, title, idx, ts, ts, _portal_dod_default_json()),
            )
    conn.execute("INSERT INTO app_migrations (id) VALUES ('portal_seed_demo_v1')")


def _manager_password_configured(app: Flask) -> bool:
    return bool((app.config.get("MANAGER_DASHBOARD_PASSWORD") or "").strip())


def _manager_logged_in() -> bool:
    return bool(session.get("manager"))


def _check_manager_password(app: Flask, candidate: str) -> bool:
    expected = (app.config.get("MANAGER_DASHBOARD_PASSWORD") or "").strip()
    if not expected:
        return False
    try:
        return secrets.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
    except Exception:
        return False


def _lpo_sm_password_configured(app: Flask) -> bool:
    return bool((app.config.get("LPO_SM_DASHBOARD_PASSWORD") or "").strip())


def _check_lpo_sm_password(app: Flask, candidate: str) -> bool:
    expected = (app.config.get("LPO_SM_DASHBOARD_PASSWORD") or "").strip()
    if not expected:
        return False
    try:
        return secrets.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
    except Exception:
        return False


def _daterange_inclusive(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def _load_meet_leave_day_map(app: Flask, start_iso: str, end_iso: str) -> dict[tuple[int, str], str]:
    conn = get_db(app)
    rows = conn.execute(
        """
        SELECT leave_id, work_date, decision
        FROM meet_leave_day
        WHERE work_date >= ? AND work_date <= ?
        """,
        (start_iso, end_iso),
    ).fetchall()
    conn.close()
    return {(int(r["leave_id"]), r["work_date"]): str(r["decision"]) for r in rows}


def _overlapping_leaves_for_day(
    rows: list[sqlite3.Row],
    d: date,
    day_dec: dict[tuple[int, str], str],
) -> list[sqlite3.Row]:
    d_iso = d.isoformat()
    overlapping: list[sqlite3.Row] = []
    for row in rows:
        try:
            sd = date.fromisoformat(row["start_date"])
            ed = date.fromisoformat(row["end_date"])
        except ValueError:
            continue
        if not (sd <= d <= ed):
            continue
        lid = int(row["id"])
        if day_dec.get((lid, d_iso)) == "removed":
            continue
        overlapping.append(row)
    return overlapping


def _effective_leave_status(row: sqlite3.Row, d_iso: str, day_dec: dict[tuple[int, str], str]) -> str:
    if row["status"] == "pending" and day_dec.get((int(row["id"]), d_iso)) == "approved":
        return "approved"
    return row["status"]


def _is_effectively_pending(row: sqlite3.Row, d_iso: str, day_dec: dict[tuple[int, str], str]) -> bool:
    return row["status"] == "pending" and day_dec.get((int(row["id"]), d_iso)) != "approved"


def _maybe_promote_leave_after_meet_days(app: Flask, leave_id: int) -> None:
    """If every calendar day in the request span has meet_leave_day=approved, mark the row approved."""
    conn = get_db(app)
    row = conn.execute("SELECT * FROM leave_requests WHERE id = ?", (leave_id,)).fetchone()
    if not row or row["status"] != "pending":
        conn.close()
        return
    try:
        sd = date.fromisoformat(row["start_date"])
        ed = date.fromisoformat(row["end_date"])
    except ValueError:
        conn.close()
        return
    for d in _daterange_inclusive(sd, ed):
        iso = d.isoformat()
        r = conn.execute(
            "SELECT decision FROM meet_leave_day WHERE leave_id = ? AND work_date = ?",
            (leave_id, iso),
        ).fetchone()
        if not r or r["decision"] != "approved":
            conn.close()
            return
    conn.execute("UPDATE leave_requests SET status = 'approved' WHERE id = ?", (leave_id,))
    conn.commit()
    conn.close()


def _meet_validate_leave_in_window(
    app: Flask,
    leave_id_raw: str | None,
    work_date: str | None,
    anchor_raw: str | None,
    roster: Sequence[str],
) -> tuple[sqlite3.Row | None, date, str | None]:
    """Returns (leave_row, anchor, error_key)."""
    roster_t = tuple(roster)
    if not leave_id_raw or not work_date:
        return None, date.today(), "bad_input"
    try:
        leave_id = int(str(leave_id_raw).strip())
    except ValueError:
        return None, date.today(), "bad_input"
    anchor = _parse_meet_anchor(anchor_raw)
    try:
        wd = date.fromisoformat(work_date.strip()[:10])
    except ValueError:
        return None, anchor, "bad_date"
    d0, d4 = _meet_window(anchor)
    if not (d0 <= wd <= d4):
        return None, anchor, "out_of_window"
    conn = get_db(app)
    row = conn.execute("SELECT * FROM leave_requests WHERE id = ?", (leave_id,)).fetchone()
    conn.close()
    if not row:
        return None, anchor, "not_found"
    if row["employee_name"] not in roster_t:
        return None, anchor, "bad_emp"
    if row["status"] not in ("pending", "approved"):
        return None, anchor, "bad_status"
    try:
        sd = date.fromisoformat(row["start_date"])
        ed = date.fromisoformat(row["end_date"])
    except ValueError:
        return None, anchor, "bad_leave_dates"
    if not (sd <= wd <= ed):
        return None, anchor, "day_not_in_leave"
    return row, anchor, None


def build_month_context(app: Flask, year: int, month: int, roster: Sequence[str] | None = None) -> dict:
    roster_t = tuple(roster) if roster is not None else EMPLOYEES
    first = date(year, month, 1)
    _, last_day = monthrange(year, month)
    last = date(year, month, last_day)
    month_start_iso = first.isoformat()
    month_end_iso = last.isoformat()

    conn = get_db(app)
    leaves = conn.execute(
        """
        SELECT * FROM leave_requests
        WHERE start_date <= ? AND end_date >= ?
          AND status IN ('pending', 'approved')
        ORDER BY employee_name ASC, start_date ASC
        """,
        (month_end_iso, month_start_iso),
    ).fetchall()
    conn.close()

    day_dec = _load_meet_leave_day_map(app, month_start_iso, month_end_iso)

    by_emp: dict[str, list[sqlite3.Row]] = {e: [] for e in roster_t}
    for row in leaves:
        name = row["employee_name"]
        if name in by_emp:
            by_emp[name].append(row)

    reason_l = dict(LEAVE_REASONS)
    dur_l = dict(DURATION_CHOICES)

    days_meta: list[dict] = []
    for d in _daterange_inclusive(first, last):
        wd = d.weekday()  # Mon=0
        is_weekend = wd >= 5
        days_meta.append(
            {
                "date": d,
                "iso": d.isoformat(),
                "weekday": calendar.day_abbr[wd],
                "day": d.day,
                "is_weekend": is_weekend,
            }
        )

    grid_rows: list[dict] = []
    for emp in roster_t:
        cells: list[dict | None] = []
        for dm in days_meta:
            d = dm["date"]
            overlapping = _overlapping_leaves_for_day(by_emp[emp], d, day_dec)
            cell: dict | None = None
            if overlapping:
                d_iso = d.isoformat()
                pending_only = [r for r in overlapping if _is_effectively_pending(r, d_iso, day_dec)]
                pool = pending_only or overlapping
                row = max(pool, key=lambda r: int(r["id"]))
                eff = _effective_leave_status(row, d_iso, day_dec)
                code = leave_cell_code(row["reason"], row["duration_type"], eff)
                rlab = reason_l.get(row["reason"], row["reason"])
                dlab = dur_l.get(row["duration_type"], row["duration_type"])
                title = f"{rlab} · {dlab} · {eff}"
                css = cell_css_class(row["reason"], eff)
                cell = {"code": code, "title": title, "css": css, "status": eff}
            cells.append(cell)
        grid_rows.append({"employee": emp, "cells": cells})

    return {
        "year": year,
        "month": month,
        "month_label": first.strftime("%B %Y"),
        "days": days_meta,
        "grid_rows": grid_rows,
        "month_start": month_start_iso,
        "month_end": month_end_iso,
    }


def build_sprint_leave_tracker_context(
    conn: sqlite3.Connection,
    app: Flask,
    sd: date,
    ed: date,
    roster: Sequence[str],
) -> tuple[list[dict], list[dict]]:
    """
    Leave-tracker style grid for a sprint date window: each roster member × each day,
    same leave / meet-day rules as the monthly worksheet and sticky-board capacity strip.
    Returns (days_meta, grid_rows) where grid_rows items are {employee, cells} and each
    cell is None or {code, title, status, css} (code = PL/UL/SL/LL… for Excel / HTML).
    """
    roster_t = tuple(roster)
    if not roster_t:
        return [], []
    start_iso = sd.isoformat()
    end_iso = ed.isoformat()
    leaves = list(
        conn.execute(
            """
            SELECT * FROM leave_requests
            WHERE start_date <= ? AND end_date >= ?
              AND status IN ('pending', 'approved')
            ORDER BY employee_name ASC, start_date ASC
            """,
            (end_iso, start_iso),
        )
    )
    day_dec = _load_meet_leave_day_map(app, start_iso, end_iso)

    by_emp: dict[str, list[sqlite3.Row]] = {e: [] for e in roster_t}
    for row in leaves:
        name = row["employee_name"]
        if name in by_emp:
            by_emp[name].append(row)

    reason_l = dict(LEAVE_REASONS)
    dur_l = dict(DURATION_CHOICES)

    days_meta: list[dict] = []
    for d in _daterange_inclusive(sd, ed):
        wd = d.weekday()
        is_weekend = wd >= 5
        days_meta.append(
            {
                "date": d,
                "iso": d.isoformat(),
                "weekday": calendar.day_abbr[wd],
                "day": d.day,
                "is_weekend": is_weekend,
            }
        )

    grid_rows: list[dict] = []
    for emp in roster_t:
        cells: list[dict | None] = []
        for dm in days_meta:
            d = dm["date"]
            overlapping = _overlapping_leaves_for_day(by_emp[emp], d, day_dec)
            cell: dict | None = None
            if overlapping:
                d_iso = d.isoformat()
                pending_only = [r for r in overlapping if _is_effectively_pending(r, d_iso, day_dec)]
                pool = pending_only or overlapping
                row = max(pool, key=lambda r: int(r["id"]))
                eff = _effective_leave_status(row, d_iso, day_dec)
                code = leave_cell_code(row["reason"], row["duration_type"], eff)
                rlab = reason_l.get(row["reason"], row["reason"])
                dlab = dur_l.get(row["duration_type"], row["duration_type"])
                title = f"{rlab} · {dlab} · {eff}"
                css = cell_css_class(row["reason"], eff)
                cell = {"code": code, "title": title, "status": eff, "css": css}
            cells.append(cell)
        grid_rows.append({"employee": emp, "cells": cells})
    return days_meta, grid_rows


def build_kanban_leave_worksheet_context(
    app: Flask,
    sprint_id: int,
    sprint_start_iso: str,
    sprint_end_iso: str,
    assignee: str,
) -> dict:
    """Leave strip + capacity for one assignee over sprint dates (same cell rules as manager worksheet)."""
    sd = date.fromisoformat(str(sprint_start_iso).strip()[:10])
    ed = date.fromisoformat(str(sprint_end_iso).strip()[:10])
    start_iso = sd.isoformat()
    end_iso = ed.isoformat()

    conn = get_db(app)
    assigned_row = conn.execute(
        """
        SELECT COALESCE(SUM(estimate_hours), 0) AS s
        FROM scrum_sprint_item
        WHERE sprint_id = ? AND assignee = ?
        """,
        (int(sprint_id), assignee),
    ).fetchone()
    assigned = float(assigned_row["s"] or 0)
    leaves = list(
        conn.execute(
            """
            SELECT * FROM leave_requests
            WHERE employee_name = ?
              AND start_date <= ? AND end_date >= ?
              AND status IN ('pending', 'approved')
            ORDER BY id ASC
            """,
            (assignee, end_iso, start_iso),
        )
    )
    hours_rows = list(
        conn.execute(
            """
            SELECT date(a.created_at) AS d, COALESCE(SUM(a.committed_hours), 0) AS h
            FROM scrum_item_activity a
            INNER JOIN scrum_sprint_item i ON i.id = a.item_id
            WHERE i.sprint_id = ? AND i.assignee = ?
              AND date(a.created_at) >= date(?)
              AND date(a.created_at) <= date(?)
            GROUP BY date(a.created_at)
            """,
            (int(sprint_id), assignee, start_iso, end_iso),
        )
    )
    conn.close()
    hours_by_iso: dict[str, float] = {}
    for r in hours_rows:
        raw_d = r["d"]
        d_key = str(raw_d)[:10] if raw_d is not None else ""
        if d_key:
            hours_by_iso[d_key] = float(r["h"] or 0.0)

    day_dec = _load_meet_leave_day_map(app, start_iso, end_iso)
    reason_l = dict(LEAVE_REASONS)
    dur_l = dict(DURATION_CHOICES)

    days_meta: list[dict] = []
    cells: list[dict | None] = []
    hours_logged: list[str] = []
    gross = 0.0
    leave_debit_total = 0.0
    available_sum = 0.0

    for d in _daterange_inclusive(sd, ed):
        wd = d.weekday()
        is_weekend = wd >= 5
        d_iso = d.isoformat()
        days_meta.append(
            {
                "date": d,
                "iso": d_iso,
                "weekday": calendar.day_abbr[wd],
                "day": d.day,
                "is_weekend": is_weekend,
            }
        )
        base = SCRUM_KANBAN_WEEKDAY_HOURS if wd < 5 else 0.0
        gross += base
        overlapping = _overlapping_leaves_for_day(leaves, d, day_dec)
        debit = 0.0
        cell: dict | None = None
        if overlapping:
            pending_only = [r for r in overlapping if _is_effectively_pending(r, d_iso, day_dec)]
            pool = pending_only or overlapping
            row = max(pool, key=lambda r: int(r["id"]))
            eff = _effective_leave_status(row, d_iso, day_dec)
            code = leave_cell_code(row["reason"], row["duration_type"], eff)
            rlab = reason_l.get(row["reason"], row["reason"])
            dlab = dur_l.get(row["duration_type"], row["duration_type"])
            title = f"{rlab} · {dlab} · {eff}"
            css = cell_css_class(row["reason"], eff)
            cell = {"code": code, "title": title, "css": css, "status": eff}
            if base > 0 and eff in ("pending", "approved"):
                dur = (row["duration_type"] or "full").strip()
                if dur in ("half_am", "half_pm"):
                    debit = min(base, SCRUM_KANBAN_WEEKDAY_HOURS / 2.0)
                else:
                    debit = min(base, SCRUM_KANBAN_WEEKDAY_HOURS)
        leave_debit_total += debit
        available_sum += max(0.0, base - debit)
        cells.append(cell)
        h_day = float(hours_by_iso.get(d_iso, 0.0))
        hours_logged.append(f"{h_day:.1f}h")

    stretch = assigned > available_sum + SCRUM_HOUR_EPS
    scale_max = max(assigned, available_sum, SCRUM_HOUR_EPS)
    pct_available_bar = round(100.0 * available_sum / scale_max, 1) if scale_max > 0 else 0.0
    pct_assigned_within = round(100.0 * min(assigned, available_sum) / scale_max, 1) if scale_max > 0 else 0.0
    pct_assigned_stretch = (
        round(100.0 * max(0.0, assigned - available_sum) / scale_max, 1) if scale_max > 0 else 0.0
    )

    return {
        "kb_leave_days": days_meta,
        "kb_leave_cells": cells,
        "kb_leave_hours_logged": hours_logged,
        "kb_assignee": assignee,
        "kb_leave_range_label": f"{start_iso} → {end_iso}",
        "kb_capacity_weekday_gross_hours": round(gross, 1),
        "kb_capacity_leave_hours": round(leave_debit_total, 1),
        "kb_capacity_available_hours": round(available_sum, 1),
        "kb_capacity_assigned_hours": round(assigned, 1),
        "kb_capacity_stretch": stretch,
        "kb_capacity_scale_max": round(scale_max, 1),
        "kb_capacity_pct_available_bar": pct_available_bar,
        "kb_capacity_pct_assigned_within_bar": pct_assigned_within,
        "kb_capacity_pct_assigned_stretch_bar": pct_assigned_stretch,
    }


def _available_hours_for_assignee_sprint_window(app: Flask, assignee: str, sd: date, ed: date) -> float:
    """Mon–Fri gross SCRUM_KANBAN_WEEKDAY_HOURS per day minus pending/approved leave (same rules as sticky-board capacity strip)."""
    start_iso = sd.isoformat()
    end_iso = ed.isoformat()
    conn = get_db(app)
    leaves = list(
        conn.execute(
            """
            SELECT * FROM leave_requests
            WHERE employee_name = ?
              AND start_date <= ? AND end_date >= ?
              AND status IN ('pending', 'approved')
            ORDER BY id ASC
            """,
            (assignee, end_iso, start_iso),
        )
    )
    conn.close()
    day_dec = _load_meet_leave_day_map(app, start_iso, end_iso)
    available_sum = 0.0
    for d in _daterange_inclusive(sd, ed):
        wd = d.weekday()
        base = SCRUM_KANBAN_WEEKDAY_HOURS if wd < 5 else 0.0
        d_iso = d.isoformat()
        overlapping = _overlapping_leaves_for_day(leaves, d, day_dec)
        debit = 0.0
        if overlapping:
            pending_only = [r for r in overlapping if _is_effectively_pending(r, d_iso, day_dec)]
            pool = pending_only or overlapping
            row = max(pool, key=lambda r: int(r["id"]))
            eff = _effective_leave_status(row, d_iso, day_dec)
            if base > 0 and eff in ("pending", "approved"):
                dur = (row["duration_type"] or "full").strip()
                if dur in ("half_am", "half_pm"):
                    debit = min(base, SCRUM_KANBAN_WEEKDAY_HOURS / 2.0)
                else:
                    debit = min(base, SCRUM_KANBAN_WEEKDAY_HOURS)
        available_sum += max(0.0, base - debit)
    return available_sum


def compute_team_sprint_capacity_leave_hours(app: Flask, roster: Sequence[str], sd: date, ed: date) -> float:
    """Total leave-adjusted capacity (h) for all roster names across sprint dates (weekends 0; leave days 0 or half)."""
    return sum(_available_hours_for_assignee_sprint_window(app, emp, sd, ed) for emp in roster)


def _parse_meet_anchor(raw: str | None) -> date:
    if not raw:
        return date.today()
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return date.today()


def _meet_window(anchor: date) -> tuple[date, date]:
    return anchor + timedelta(days=-1), anchor + timedelta(days=1)


def build_meet_context(app: Flask, anchor: date, roster: Sequence[str] | None = None) -> dict:
    """Three-day window: anchor−1 … anchor+1 (center column is the anchor day, highlighted)."""
    roster_t = tuple(roster) if roster is not None else EMPLOYEES
    dates_list = [anchor + timedelta(days=k) for k in (-1, 0, 1)]
    start_iso = dates_list[0].isoformat()
    end_iso = dates_list[-1].isoformat()
    conn = get_db(app)
    leaves = list(
        conn.execute(
            """
            SELECT * FROM leave_requests
            WHERE start_date <= ? AND end_date >= ?
              AND status IN ('pending', 'approved')
            ORDER BY id ASC
            """,
            (end_iso, start_iso),
        )
    )
    conn.close()

    day_dec = _load_meet_leave_day_map(app, start_iso, end_iso)

    by_emp: dict[str, list[sqlite3.Row]] = {e: [] for e in roster_t}
    for row in leaves:
        if row["employee_name"] in by_emp:
            by_emp[row["employee_name"]].append(row)

    reason_l = dict(LEAVE_REASONS)
    dur_l = dict(DURATION_CHOICES)
    today = date.today()
    days_out: list[dict] = []
    for d in dates_list:
        wd = d.weekday()
        days_out.append(
            {
                "date": d,
                "iso": d.isoformat(),
                "weekday": calendar.day_abbr[wd],
                "label": d.strftime("%a %d %b"),
                "is_anchor": d == anchor,
                "is_today": d == today,
                "is_weekend": wd >= 5,
            }
        )

    grid_rows: list[dict] = []
    for emp in roster_t:
        cells: list[dict] = []
        for d in dates_list:
            d_iso = d.isoformat()
            overlapping = _overlapping_leaves_for_day(by_emp[emp], d, day_dec)
            cell: dict = {
                "iso": d_iso,
                "has_leave": False,
                "code": "",
                "css": "",
                "title": "",
                "status": "",
                "leave_id": None,
                "is_anchor": d == anchor,
                "is_today": d == today,
                "is_weekend": d.weekday() >= 5,
            }
            if overlapping:
                pending_only = [r for r in overlapping if _is_effectively_pending(r, d_iso, day_dec)]
                pool = pending_only or overlapping
                row = max(pool, key=lambda r: int(r["id"]))
                eff = _effective_leave_status(row, d_iso, day_dec)
                code = leave_cell_code(row["reason"], row["duration_type"], eff)
                rlab = reason_l.get(row["reason"], row["reason"])
                dlab = dur_l.get(row["duration_type"], row["duration_type"])
                title = f"{rlab} · {dlab} · {eff}"
                css = cell_css_class(row["reason"], eff)
                cell.update(
                    {
                        "has_leave": True,
                        "code": code,
                        "css": css,
                        "title": title,
                        "status": eff,
                        "leave_id": int(row["id"]),
                    }
                )
            cells.append(cell)
        grid_rows.append({"employee": emp, "cells": cells})

    return {
        "anchor": anchor,
        "anchor_label": anchor.strftime("%A %d %B %Y"),
        "prev_anchor": (anchor - timedelta(days=1)).isoformat(),
        "next_anchor": (anchor + timedelta(days=1)).isoformat(),
        "days": days_out,
        "grid_rows": grid_rows,
        "window_start": start_iso,
        "window_end": end_iso,
    }


def _normalize_hub_mode(raw: str | None) -> str:
    m = (raw or "leave").strip().lower()
    return m if m in ("leave", "scrum") else "leave"


def _normalize_scrum_status(raw: str | None) -> str:
    s = (raw or "open").strip().lower()
    return s if s in {c[0] for c in SCRUM_STATUS_CHOICES} else "open"


def _parse_hours_field(raw: str | None) -> float:
    if raw is None:
        return 0.0
    t = str(raw).strip().replace(",", ".")
    if not t:
        return 0.0
    try:
        return max(0.0, min(9999.0, float(t)))
    except ValueError:
        return 0.0


def _parse_committed_hours_for_kanban_move(
    raw: object,
    *,
    from_col: str,
    to_col: str,
) -> float | None:
    """Hours logged on a column move. Do → In progress: blank / JSON null → NULL in DB (unset), not zero."""
    if from_col != "do" or to_col != "doing":
        return _parse_hours_field(str(raw if raw is not None else ""))
    if raw is None:
        return None
    if isinstance(raw, bool):
        return _parse_hours_field(str(raw))
    if isinstance(raw, (int, float)):
        return max(0.0, min(9999.0, float(raw)))
    t = str(raw).strip().replace(",", ".")
    if not t:
        return None
    try:
        return max(0.0, min(9999.0, float(t)))
    except ValueError:
        return 0.0


def _hours_close(a: float, b: float, eps: float = SCRUM_HOUR_EPS) -> bool:
    return abs(float(a) - float(b)) <= eps


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _working_weekdays(start: date, end: date) -> list[date]:
    """Mon–Fri dates inclusive between start and end (for ideal burndown)."""
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _parse_optional_int(raw: str | int | None) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    try:
        v = int(str(raw).strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _sprint_row_for_team(conn: sqlite3.Connection, sprint_id: int, team_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM scrum_sprint WHERE id = ? AND team_id = ?", (int(sprint_id), int(team_id))
    ).fetchone()


def _sprint_row_is_closed(row: sqlite3.Row | None) -> bool:
    if row is None:
        return False
    if "is_closed" not in row.keys():
        return False
    try:
        return int(row["is_closed"] or 0) != 0
    except (TypeError, ValueError):
        return False


def _sprint_is_closed_by_id(conn: sqlite3.Connection, sprint_id: int) -> bool:
    r = conn.execute("SELECT is_closed FROM scrum_sprint WHERE id = ?", (int(sprint_id),)).fetchone()
    return _sprint_row_is_closed(r)


def _sprint_name_exists_for_team(conn: sqlite3.Connection, team_id: int, name: str) -> bool:
    """True if this team already has a sprint with the same name (case-insensitive, trimmed)."""
    n = (name or "").strip()
    if not n:
        return False
    return (
        conn.execute(
            """
            SELECT 1 FROM scrum_sprint
            WHERE team_id = ? AND lower(trim(name)) = lower(trim(?))
            LIMIT 1
            """,
            (int(team_id), n),
        ).fetchone()
        is not None
    )


def _sprint_name_taken_by_other(
    conn: sqlite3.Connection, team_id: int, name: str, exclude_sprint_id: int
) -> bool:
    """True if another sprint on this team (not exclude_sprint_id) already uses this name."""
    n = (name or "").strip()
    if not n:
        return False
    return (
        conn.execute(
            """
            SELECT 1 FROM scrum_sprint
            WHERE team_id = ? AND id != ? AND lower(trim(name)) = lower(trim(?))
            LIMIT 1
            """,
            (int(team_id), int(exclude_sprint_id), n),
        ).fetchone()
        is not None
    )


def _pick_default_sprint_id(conn: sqlite3.Connection, team_id: int, preferred: int | None) -> int | None:
    if preferred is not None:
        row = conn.execute(
            "SELECT id FROM scrum_sprint WHERE id = ? AND team_id = ?",
            (int(preferred), int(team_id)),
        ).fetchone()
        if row:
            return int(row["id"])
    today = date.today().isoformat()
    row = conn.execute(
        """
        SELECT id FROM scrum_sprint
        WHERE team_id = ? AND start_date <= ? AND end_date >= ?
        ORDER BY start_date DESC
        LIMIT 1
        """,
        (int(team_id), today, today),
    ).fetchone()
    if row:
        return int(row["id"])
    row = conn.execute(
        """
        SELECT id FROM scrum_sprint
        WHERE team_id = ?
        ORDER BY end_date DESC, id DESC
        LIMIT 1
        """,
        (int(team_id),),
    ).fetchone()
    return int(row["id"]) if row else None


def _normalize_kanban_column(raw: str | None) -> str:
    c = (raw or "backlog").strip().lower()
    return c if c in SCRUM_KANBAN_COLUMNS else "backlog"


def _normalize_hex_color(raw: str | None, fallback: str = "#64748b") -> str:
    s = (raw or "").strip()
    if _SCRUM_HEX_COLOR.match(s):
        return s.lower()
    return fallback


def _slug_task_kind_code(label: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")[:20]
    if not base:
        base = "kind"
    return f"{base}-{secrets.token_hex(3)}"


def _ensure_team_task_kinds(conn: sqlite3.Connection, team_id: int) -> None:
    for code, label, color, so in SCRUM_BUILTIN_TASK_KIND_ROWS:
        conn.execute(
            """
            INSERT OR IGNORE INTO scrum_team_task_kind (team_id, code, label, color_hex, sort_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(team_id), code, label, color, so),
        )


def _list_team_task_kinds(conn: sqlite3.Connection, team_id: int) -> list[dict]:
    _ensure_team_task_kinds(conn, team_id)
    codes = tuple(sorted(SCRUM_TASK_KIND_CODES))
    ph = ",".join("?" * len(codes))
    return [
        {"code": r["code"], "label": r["label"], "color_hex": r["color_hex"]}
        for r in conn.execute(
            f"""
            SELECT code, label, color_hex FROM scrum_team_task_kind
            WHERE team_id = ? AND code IN ({ph})
            ORDER BY sort_order ASC, code ASC
            """,
            (int(team_id), *codes),
        )
    ]


def _resolve_task_kind_code(conn: sqlite3.Connection, team_id: int, raw: str | None) -> str:
    """Return a task kind code, or '' when the type is cleared (no kind)."""
    if raw is None:
        return ""
    k = raw.strip().lower()
    if not k or k in ("none", "-", "__none__"):
        return ""
    if k in SCRUM_LEGACY_TASK_KIND_MAP:
        k = SCRUM_LEGACY_TASK_KIND_MAP[k]
    row = conn.execute(
        "SELECT code FROM scrum_team_task_kind WHERE team_id = ? AND code = ?",
        (int(team_id), k),
    ).fetchone()
    if row:
        return str(row["code"])
    return "ndy"


def _coerce_sprint_item_task_kind(conn: sqlite3.Connection, team_id: int, raw: str | None) -> str:
    """Normalize to a built-in task kind code (for persistence and dropdowns)."""
    c = _resolve_task_kind_code(conn, team_id, raw)
    if c in SCRUM_TASK_KIND_CODES:
        return c
    return "ndy"


def _insert_team_task_kind(conn: sqlite3.Connection, team_id: int, label: str, color_hex: str) -> str:
    _ensure_team_task_kinds(conn, team_id)
    safe_label = (label or "").strip()[:120] or "Custom kind"
    hx = _normalize_hex_color(color_hex)
    code = _slug_task_kind_code(safe_label)
    mx = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM scrum_team_task_kind WHERE team_id = ?",
        (int(team_id),),
    ).fetchone()
    sort_order = int(mx["n"] if mx is not None else 0)
    conn.execute(
        """
        INSERT INTO scrum_team_task_kind (team_id, code, label, color_hex, sort_order)
        VALUES (?, ?, ?, ?, ?)
        """,
        (int(team_id), code, safe_label, hx, sort_order),
    )
    return code


def _task_kind_color_for_item(
    conn: sqlite3.Connection, team_id: int, task_kind_code: str, custom_color: str | None
) -> str:
    if not (task_kind_code or "").strip():
        return "#64748b"
    code = _resolve_task_kind_code(conn, team_id, task_kind_code)
    if not code:
        return "#64748b"
    row = conn.execute(
        "SELECT color_hex FROM scrum_team_task_kind WHERE team_id = ? AND code = ?",
        (int(team_id), code),
    ).fetchone()
    if row and (row["color_hex"] or "").strip():
        return _normalize_hex_color(str(row["color_hex"]))
    return "#64748b"


def _status_for_kanban_column(col: str) -> str:
    c = _normalize_kanban_column(col)
    if c == "done":
        return "done"
    if c == "doing":
        return "doing"
    return "open"


def _carry_forward_do_doing_to_new_sprint(
    conn: sqlite3.Connection,
    team_id: int,
    new_sprint_id: int,
    new_start: date,
    ts: str,
) -> int:
    """Copy Do and Doing stickies from the latest prior sprint (ends before new_start) into this sprint's backlog.
    New estimate = original estimate minus total committed hours logged on that sticky (floored at 0).
    """
    prev = conn.execute(
        """
        SELECT id FROM scrum_sprint
        WHERE team_id = ? AND id != ? AND end_date < ?
        ORDER BY end_date DESC, id DESC
        LIMIT 1
        """,
        (team_id, new_sprint_id, new_start.isoformat()),
    ).fetchone()
    if not prev:
        return 0
    prev_id = int(prev["id"])
    rows = list(
        conn.execute(
            """
            SELECT id, assignee, title, estimate_hours, notes, dod, task_kind, sticky_color_hex, done_artifacts, sort_order, area
            FROM scrum_sprint_item
            WHERE sprint_id = ? AND lower(trim(kanban_column)) IN ('do', 'doing')
            ORDER BY assignee ASC, sort_order ASC, id ASC
            """,
            (prev_id,),
        )
    )
    if not rows:
        return 0
    st = _status_for_kanban_column("backlog")
    last_so: dict[str, int] = {}
    copied = 0
    for r in rows:
        emp = (r["assignee"] or "").strip()
        last_so[emp] = last_so.get(emp, -1) + 1
        so = last_so[emp]
        title = (r["title"] or "").strip()
        old_iid = int(r["id"])
        log_row = conn.execute(
            "SELECT COALESCE(SUM(committed_hours), 0) AS h FROM scrum_item_activity WHERE item_id = ?",
            (old_iid,),
        ).fetchone()
        logged = float(log_row["h"] or 0)
        raw_est = float(r["estimate_hours"] or 0)
        est = max(0.0, round(raw_est - logged, 2))
        notes = (r["notes"] or "").strip()[:2000]
        dod = (r["dod"] or "").strip()[:4000]
        tkind = (r["task_kind"] or "").strip() or "task"
        sticky_raw = (r["sticky_color_hex"] or "").strip() if "sticky_color_hex" in r.keys() else ""
        sticky_hex = sticky_raw if sticky_raw and _SCRUM_HEX_COLOR.match(sticky_raw) else None
        da_raw = (r["done_artifacts"] if "done_artifacts" in r.keys() else None) or "[]"
        da_str = str(da_raw).strip()[:8000] or "[]"
        area_s = str(r["area"] or "").strip()[:SCRUM_STICKY_AREA_MAX_LEN]
        conn.execute(
            """
            INSERT INTO scrum_sprint_item
            (sprint_id, assignee, title, estimate_hours, status, notes, dod, sort_order, created_at, updated_at, kanban_column, task_kind, sticky_color_hex, done_artifacts, area)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'backlog', ?, ?, ?, ?)
            """,
            (new_sprint_id, emp, title, est, st, notes, dod, so, ts, ts, tkind, sticky_hex, da_str, area_s),
        )
        copied += 1
    return copied


def _activity_calendar_day(raw_ts: str | None) -> date | None:
    if not raw_ts:
        return None
    s = str(raw_ts).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def build_sprint_burndown_chart_context(
    conn: sqlite3.Connection, sprint_id: int, start_iso: str, end_iso: str
) -> dict:
    """
    Ideal vs remaining-effort burndown for the whole sprint (all members).
    Remaining = sum per open sticky of max(0, estimate - cumulative Burnt hours) until Done.
    """
    empty = {
        "burndown_has_chart": False,
        "burndown_message": "Add stickies with estimates to see a burndown.",
        "burndown_ideal_d": "",
        "burndown_actual_d": "",
        "burndown_area_d": "",
        "burndown_y_ticks": [],
        "burndown_x_labels": [],
        "burndown_svg_w": 520,
        "burndown_svg_h": 228,
        "burndown_total_hours": 0.0,
        "burndown_remaining_hours": 0.0,
        "burndown_axis_label": "",
        "burndown_y_max": 0.0,
        "burndown_last_day_label": "—",
        "burndown_actual_dots": [],
    }
    try:
        sd = date.fromisoformat(str(start_iso)[:10])
        ed = date.fromisoformat(str(end_iso)[:10])
    except ValueError:
        return empty
    if ed < sd:
        sd, ed = ed, sd
    days: list[date] = list(_daterange_inclusive(sd, ed))
    n = len(days)
    if n < 1:
        return empty

    items = list(
        conn.execute(
            """
            SELECT id, estimate_hours, kanban_column, updated_at
            FROM scrum_sprint_item
            WHERE sprint_id = ?
            """,
            (int(sprint_id),),
        )
    )
    if not items:
        return empty

    ids = [int(r["id"]) for r in items]
    ph = ",".join("?" * len(ids))

    first_done: dict[int, date] = {}
    for r in conn.execute(
        f"""
        SELECT item_id, MIN(created_at) AS ts
        FROM scrum_item_activity
        WHERE item_id IN ({ph}) AND lower(trim(to_column)) = 'done'
        GROUP BY item_id
        """,
        ids,
    ):
        d = _activity_calendar_day(str(r["ts"] or ""))
        if d:
            first_done[int(r["item_id"])] = d

    acts: dict[int, list[tuple[date, float]]] = {i: [] for i in ids}
    for r in conn.execute(
        f"""
        SELECT item_id, committed_hours, created_at
        FROM scrum_item_activity
        WHERE item_id IN ({ph})
        """,
        ids,
    ):
        iid = int(r["item_id"])
        d = _activity_calendar_day(str(r["created_at"] or ""))
        if d is None:
            continue
        h = float(r["committed_hours"] or 0)
        if abs(h) <= SCRUM_HOUR_EPS:
            continue
        acts[iid].append((d, h))
    for iid in acts:
        acts[iid].sort(key=lambda t: t[0])

    def cum_burnt_through(iid: int, d: date) -> float:
        t = 0.0
        for ad, h in acts.get(iid, []):
            if ad <= d:
                t += h
        return t

    def remaining_on_day(d: date) -> float:
        rem = 0.0
        for it in items:
            iid = int(it["id"])
            est = float(it["estimate_hours"] or 0)
            col = _normalize_kanban_column(it["kanban_column"] if it["kanban_column"] else None)
            fd = first_done.get(iid)
            if fd is None and col == "done":
                fd = _activity_calendar_day(str(it["updated_at"] or "")) or ed
            if fd is not None and d >= fd:
                continue
            burnt = cum_burnt_through(iid, d)
            rem += max(0.0, est - burnt)
        return rem

    day0 = days[0]
    remaining_start = remaining_on_day(day0)
    if remaining_start <= SCRUM_HOUR_EPS:
        remaining_start = sum(
            float(it["estimate_hours"] or 0)
            for it in items
            if _normalize_kanban_column(it["kanban_column"] if it["kanban_column"] else None) != "done"
        )
    if remaining_start <= SCRUM_HOUR_EPS:
        remaining_start = sum(float(it["estimate_hours"] or 0) for it in items)
    if remaining_start <= SCRUM_HOUR_EPS:
        return empty

    total_scope = float(remaining_start)
    today = date.today()
    last_actual_day = min(ed, today) if today >= sd else None

    ideal_vals: list[float] = []
    for i in range(n):
        if n <= 1:
            ideal_vals.append(0.0)
        else:
            ideal_vals.append(total_scope * (n - 1 - i) / (n - 1))

    actual_vals: list[float | None] = []
    for d in days:
        if last_actual_day is None or d > last_actual_day:
            actual_vals.append(None)
        else:
            actual_vals.append(remaining_on_day(d))

    actual_numeric = [float(x) for x in actual_vals if x is not None]
    y_max = max(max(ideal_vals), max(actual_numeric) if actual_numeric else 0.0, total_scope, 1.0) * 1.08
    y_max = max(y_max, 1.0)

    W, H = 520, 228
    pad_l, pad_r, pad_t, pad_b = 50, 14, 26, 34
    iw = W - pad_l - pad_r
    ih = H - pad_t - pad_b
    base_y = pad_t + ih

    def x_at(i: int) -> float:
        if n <= 1:
            return pad_l + iw / 2
        return pad_l + i * iw / (n - 1)

    def y_at(val: float) -> float:
        v = max(0.0, min(val, y_max))
        return pad_t + (y_max - v) / y_max * ih

    ideal_pts: list[tuple[float, float]] = [(x_at(i), y_at(ideal_vals[i])) for i in range(n)]
    ideal_d = f"M {ideal_pts[0][0]:.1f},{ideal_pts[0][1]:.1f}"
    if len(ideal_pts) > 1:
        ideal_d += "".join(f" L {x:.1f},{y:.1f}" for x, y in ideal_pts[1:])

    actual_pts: list[tuple[float, float]] = []
    for i, d in enumerate(days):
        av = actual_vals[i]
        if av is None:
            break
        actual_pts.append((x_at(i), y_at(av)))
    actual_d = ""
    if len(actual_pts) >= 2:
        actual_d = f"M {actual_pts[0][0]:.1f},{actual_pts[0][1]:.1f}"
        actual_d += "".join(f" L {x:.1f},{y:.1f}" for x, y in actual_pts[1:])
    elif len(actual_pts) == 1:
        actual_d = f"M {actual_pts[0][0]:.1f},{actual_pts[0][1]:.1f}"

    area_d = ""
    if len(actual_pts) >= 2:
        ax0, _y0 = actual_pts[0]
        ax1, _y1 = actual_pts[-1]
        area_d = f"M {ax0:.1f},{base_y:.1f}"
        for x, y in actual_pts:
            area_d += f" L {x:.1f},{y:.1f}"
        area_d += f" L {ax1:.1f},{base_y:.1f} Z"
    elif len(actual_pts) == 1:
        ax, ay = actual_pts[0]
        area_d = f"M {ax:.1f},{base_y:.1f} L {ax:.1f},{ay:.1f} L {ax + 0.5:.1f},{base_y:.1f} Z"

    y_tick_vals = [0.0, y_max * 0.25, y_max * 0.5, y_max * 0.75, y_max]
    y_ticks_out: list[dict[str, float | str]] = []
    for tv in y_tick_vals:
        y_ticks_out.append({"y": y_at(tv), "label": f"{tv:.0f}h" if tv >= 10 else f"{tv:.1f}h"})

    x_labels: list[dict[str, float | str]] = []
    for idx in sorted({0, n // 2, n - 1}):
        if idx < 0 or idx >= n:
            continue
        d = days[idx]
        x_labels.append({"x": x_at(idx), "label": f"{calendar.month_abbr[d.month]} {d.day}"})

    current_remaining = float(actual_numeric[-1]) if actual_numeric else remaining_on_day(min(ed, max(today, sd)))

    actual_dots = [{"x": round(x, 1), "y": round(y, 1)} for x, y in actual_pts]

    return {
        "burndown_has_chart": True,
        "burndown_message": "",
        "burndown_total_hours": round(total_scope, 1),
        "burndown_remaining_hours": round(current_remaining, 1),
        "burndown_axis_label": "Hours (remaining work)",
        "burndown_svg_w": W,
        "burndown_svg_h": H,
        "burndown_ideal_d": ideal_d,
        "burndown_actual_d": actual_d,
        "burndown_area_d": area_d,
        "burndown_actual_dots": actual_dots,
        "burndown_y_ticks": y_ticks_out,
        "burndown_x_labels": x_labels,
        "burndown_y_max": round(y_max, 1),
        "burndown_last_day_label": f"{calendar.month_abbr[last_actual_day.month]} {last_actual_day.day}"
        if last_actual_day
        else "—",
    }


def build_sprint_task_kind_stack_chart_context(
    conn: sqlite3.Connection, team_id: int, sprint_id: int
) -> dict:
    """
    Vertical stacked bars per task type: burnt within estimate, burnt beyond estimate, remaining estimate.
    Same default SVG size as the sprint burndown chart for a matched pair in the UI.
    """
    W, H = 520, 228
    empty: dict = {
        "kind_stack_has_chart": False,
        "kind_stack_message": "No sticky estimates by type yet.",
        "kind_stack_svg_w": W,
        "kind_stack_svg_h": H,
        "kind_stack_y_ticks": [],
        "kind_stack_y_max": 0.0,
        "kind_stack_bars": [],
        "kind_stack_axis_label": "Hours (by task type)",
        "kind_stack_total_est": 0.0,
        "kind_stack_total_burnt": 0.0,
    }
    _ensure_team_task_kinds(conn, team_id)
    kind_meta: dict[str, dict[str, str]] = {}
    for r in conn.execute(
        "SELECT code, label, color_hex FROM scrum_team_task_kind WHERE team_id = ?",
        (int(team_id),),
    ):
        kind_meta[str(r["code"])] = {
            "label": str(r["label"] or r["code"]),
            "color_hex": str(r["color_hex"] or "#94a3b8"),
        }

    kind_est = {code: 0.0 for code in SCRUM_TASK_KIND_CODES}
    kind_com = {code: 0.0 for code in SCRUM_TASK_KIND_CODES}
    for r in conn.execute(
        """
        SELECT task_kind, SUM(estimate_hours) AS h
        FROM scrum_sprint_item
        WHERE sprint_id = ?
        GROUP BY task_kind
        """,
        (int(sprint_id),),
    ).fetchall():
        code = _resolve_task_kind_code(conn, team_id, str(r["task_kind"] or ""))
        if code in kind_est:
            kind_est[code] += float(r["h"] or 0)
    for r in conn.execute(
        """
        SELECT i.task_kind, SUM(a.committed_hours) AS h
        FROM scrum_item_activity a
        JOIN scrum_sprint_item i ON i.id = a.item_id
        WHERE i.sprint_id = ?
        GROUP BY i.task_kind
        """,
        (int(sprint_id),),
    ).fetchall():
        code = _resolve_task_kind_code(conn, team_id, str(r["task_kind"] or ""))
        if code in kind_com:
            kind_com[code] += float(r["h"] or 0)

    ordered = SPRINT_TEAM_KIND_STACK_ORDER
    any_h = any(
        kind_est[c] > SCRUM_HOUR_EPS or kind_com[c] > SCRUM_HOUR_EPS for c in ordered
    )
    if not any_h:
        return empty

    tot_est = sum(kind_est[c] for c in ordered)
    tot_burnt = sum(kind_com[c] for c in ordered)
    y_max = max(max(kind_est[c], kind_com[c]) for c in ordered)
    y_max = max(y_max, 1.0) * 1.08

    pad_l, pad_r, pad_t, pad_b = 46, 12, 22, 34
    iw = W - pad_l - pad_r
    ih = H - pad_t - pad_b
    base_y = pad_t + ih
    nbar = len(ordered)
    gap = max(6.0, iw * 0.018)
    bar_w = (iw - gap * (nbar - 1)) / nbar

    def height_for(hrs: float) -> float:
        return max(0.0, hrs / y_max) * ih

    y_tick_vals = [0.0, y_max * 0.25, y_max * 0.5, y_max * 0.75, y_max]
    y_ticks_out: list[dict[str, float | str]] = []
    for tv in y_tick_vals:
        v = max(0.0, min(tv, y_max))
        yy = pad_t + (y_max - v) / y_max * ih
        y_ticks_out.append({"y": yy, "label": f"{tv:.0f}h" if tv >= 10 else f"{tv:.1f}h"})

    bars: list[dict] = []
    for i, code in enumerate(ordered):
        est = float(kind_est.get(code, 0.0))
        burnt = float(kind_com.get(code, 0.0))
        bi = min(est, burnt)
        over = max(0.0, burnt - est)
        rem = max(0.0, est - burnt)
        x = pad_l + i * (bar_w + gap)
        meta = kind_meta.get(code, {"label": code.upper(), "color_hex": "#94a3b8"})
        label = str(meta["label"])
        short = label
        if code == "process_tools":
            short = "P&T"
        elif len(label) > 8:
            short = label[:7] + "…"

        segs: list[dict[str, float | str]] = []
        yc = base_y
        h1 = height_for(bi)
        if h1 > 0.08:
            segs.append(
                {
                    "x": round(x, 1),
                    "y": round(yc - h1, 1),
                    "w": round(bar_w, 1),
                    "h": round(h1, 1),
                    "fill": "#fb7185",
                    "kind": "burnt",
                }
            )
            yc -= h1
        h2 = height_for(over)
        if h2 > 0.08:
            segs.append(
                {
                    "x": round(x, 1),
                    "y": round(yc - h2, 1),
                    "w": round(bar_w, 1),
                    "h": round(h2, 1),
                    "fill": "#f59e0b",
                    "kind": "over",
                }
            )
            yc -= h2
        h3 = height_for(rem)
        if h3 > 0.08:
            segs.append(
                {
                    "x": round(x, 1),
                    "y": round(yc - h3, 1),
                    "w": round(bar_w, 1),
                    "h": round(h3, 1),
                    "fill": "#38bdf8",
                    "kind": "remaining",
                }
            )

        bars.append(
            {
                "code": code,
                "label": label,
                "label_short": short,
                "x": round(x, 1),
                "bar_w": round(bar_w, 1),
                "cx": round(x + bar_w / 2, 1),
                "est": round(est, 1),
                "burnt": round(burnt, 1),
                "title": f"{label}: {est:.1f}h estimated, {burnt:.1f}h burnt (this sprint)",
                "segments": segs,
            }
        )

    return {
        "kind_stack_has_chart": True,
        "kind_stack_message": "",
        "kind_stack_svg_w": W,
        "kind_stack_svg_h": H,
        "kind_stack_y_ticks": y_ticks_out,
        "kind_stack_y_max": round(y_max, 1),
        "kind_stack_bars": bars,
        "kind_stack_axis_label": "Hours (est. vs burnt by type)",
        "kind_stack_total_est": round(tot_est, 1),
        "kind_stack_total_burnt": round(tot_burnt, 1),
    }


def _sanitize_xlsx_base_filename(name: str) -> str:
    s = (name or "").strip() or "sprint"
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", s)
    s = re.sub(r"\s+", "_", s).strip("._")
    return (s[:96] if s else "sprint")


def build_sprint_export_xlsx_bytes(
    conn: sqlite3.Connection,
    team_id: int,
    sprint_id: int,
    *,
    app: Flask | None = None,
    manager_roster: Sequence[str] | None = None,
) -> tuple[bytes | None, str]:
    """
    Build a multi-sheet .xlsx: Summary (metrics A:B; leave tracker column D; planned capacity %;
    free capacity hours; member goals; task kinds), PNG snapshot tab, SprintStatus (stickies +
    day-wise hours like FB2610), Activity log (one row per sticky, day-wise activity), Daily tasks
    Details (per-member metrics with team + day columns; compact sticky rows; daily task rows),
    and Appriciation (one row per appreciation; columns C, D, G, H only — author, comment, title, assignee).
    On failure returns (None, flash_message). On success returns (bytes, download_filename).
    """
    try:
        from collections import defaultdict

        from openpyxl import Workbook
        from openpyxl.comments import Comment
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return None, "Excel export requires openpyxl (install dependencies)."

    sprint = conn.execute(
        "SELECT * FROM scrum_sprint WHERE id = ? AND team_id = ?", (int(sprint_id), int(team_id))
    ).fetchone()
    if not sprint:
        return None, "Sprint not found."

    team_row = conn.execute("SELECT name FROM teams WHERE id = ?", (int(team_id),)).fetchone()
    team_name = str(team_row["name"]) if team_row else ""

    items = list(
        conn.execute(
            """
            SELECT
              i.id,
              i.assignee,
              i.title,
              i.area,
              i.estimate_hours,
              i.status,
              i.kanban_column,
              i.task_kind,
              COALESCE(k.label, i.task_kind) AS task_kind_label,
              i.sticky_color_hex,
              i.notes,
              i.dod,
              i.done_artifacts,
              i.sort_order,
              i.created_at,
              i.updated_at,
              COALESCE(
                (SELECT SUM(committed_hours) FROM scrum_item_activity WHERE item_id = i.id),
                0
              ) AS total_burnt_hours
            FROM scrum_sprint_item i
            LEFT JOIN scrum_team_task_kind k ON k.team_id = ? AND k.code = i.task_kind
            WHERE i.sprint_id = ?
            ORDER BY i.assignee COLLATE NOCASE, i.sort_order, i.id
            """,
            (int(team_id), int(sprint_id)),
        )
    )

    activity = list(
        conn.execute(
            """
            SELECT
              a.id AS activity_id,
              a.item_id,
              i.title AS sticky_title,
              i.assignee,
              a.created_at,
              a.committed_hours,
              a.from_column,
              a.to_column,
              a.body AS note
            FROM scrum_item_activity a
            JOIN scrum_sprint_item i ON i.id = a.item_id
            WHERE i.sprint_id = ?
            ORDER BY a.created_at ASC, a.id ASC
            """,
            (int(sprint_id),),
        )
    )

    appreciation_export_rows = list(
        conn.execute(
            """
            SELECT
              a.author AS appreciation_author,
              a.comment AS appreciation_comment,
              i.title,
              i.assignee
            FROM scrum_item_appreciation a
            JOIN scrum_sprint_item i ON i.id = a.item_id
            WHERE i.sprint_id = ?
            ORDER BY a.created_at ASC, a.id ASC
            """,
            (int(sprint_id),),
        )
    )

    try:
        sd_date_export = date.fromisoformat(str(sprint["start_date"])[:10])
        ed_date_export = date.fromisoformat(str(sprint["end_date"])[:10])
    except ValueError:
        sd_date_export = ed_date_export = date.today()
    sprint_calendar_dates: list[date] = []
    _walk = sd_date_export
    while _walk <= ed_date_export:
        sprint_calendar_dates.append(_walk)
        _walk += timedelta(days=1)
    sprint_day_set = frozenset(sprint_calendar_dates)

    def _activity_day(ts: object) -> date | None:
        if ts is None:
            return None
        s = str(ts).strip()
        if len(s) < 10 or s[4] != "-" or s[7] != "-":
            return None
        try:
            d = date.fromisoformat(s[:10])
        except ValueError:
            return None
        return d if d in sprint_day_set else None

    item_day_hours: dict[int, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    item_day_snips: dict[int, dict[date, list[str]]] = defaultdict(lambda: defaultdict(list))
    assignee_day_hours: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for ar in activity:
        ad = _activity_day(ar["created_at"])
        if ad is None:
            continue
        iid = int(ar["item_id"])
        h = float(ar["committed_hours"] or 0)
        item_day_hours[iid][ad] += h
        em = str(ar["assignee"] or "").strip()
        if em:
            assignee_day_hours[em][ad] += h
        fc = str(ar["from_column"] or "").strip()
        tc = str(ar["to_column"] or "").strip()
        body = str(ar["note"] or "").strip().replace("\n", " ")
        if len(body) > 100:
            body = body[:97] + "…"
        snip = f"+{h:.1f}h {fc}→{tc}" + (f": {body}" if body else "")
        item_day_snips[iid][ad].append(snip)

    member_goals = list(
        conn.execute(
            """
            SELECT employee_name, goal, updated_at
            FROM scrum_sprint_member_goal
            WHERE sprint_id = ?
            ORDER BY employee_name COLLATE NOCASE
            """,
            (int(sprint_id),),
        )
    )

    daily_rows = list(
        conn.execute(
            """
            SELECT
              d.id,
              d.work_date,
              d.assignee,
              d.title,
              d.planned_hours,
              d.committed_hours,
              d.actual_hours,
              d.status,
              d.notes,
              d.sort_order,
              d.created_at,
              d.updated_at,
              d.sprint_item_id,
              COALESCE(i.title, '') AS linked_sticky_title
            FROM scrum_daily_task d
            LEFT JOIN scrum_sprint_item i
              ON i.id = d.sprint_item_id AND i.sprint_id = d.sprint_id
            WHERE d.sprint_id = ?
            ORDER BY d.work_date, d.assignee COLLATE NOCASE, d.sort_order, d.id
            """,
            (int(sprint_id),),
        )
    )

    roster_xlsx: list[str]
    if manager_roster:
        roster_xlsx = [str(x).strip() for x in manager_roster if str(x).strip()]
    else:
        roster_xlsx = [
            str(row["employee_name"])
            for row in conn.execute(
                """
                SELECT employee_name FROM team_roster
                WHERE team_id = ?
                ORDER BY sort_order, employee_name COLLATE NOCASE
                """,
                (int(team_id),),
            )
        ]
    if not roster_xlsx:
        roster_xlsx = sorted(
            {str(r["assignee"] or "").strip() for r in items if (r["assignee"] or "").strip()},
            key=str.casefold,
        )
    member_overview = _sprint_team_overview_rows(app, conn, team_id, sprint_id, roster_xlsx)

    task_kinds = list(
        conn.execute(
            """
            SELECT code, label, color_hex, sort_order
            FROM scrum_team_task_kind
            WHERE team_id = ?
            ORDER BY sort_order, id ASC
            """,
            (int(team_id),),
        )
    )

    tot_est = sum(float(r["estimate_hours"] or 0) for r in items)
    tot_burnt = sum(float(r["total_burnt_hours"] or 0) for r in items)
    done_n = sum(
        1
        for r in items
        if _normalize_kanban_column(r["kanban_column"] if r["kanban_column"] else None) == "done"
    )
    open_n = len(items) - done_n

    col_counts: dict[str, int] = {}
    for r in items:
        kc = _normalize_kanban_column(r["kanban_column"] if r["kanban_column"] else None) or "unknown"
        col_counts[kc] = col_counts.get(kc, 0) + 1

    raw_cap = sprint["team_capacity_hours"] if "team_capacity_hours" in sprint.keys() else None
    total_sprint_capacity_h: float | None = float(raw_cap) if raw_cap is not None else None
    if total_sprint_capacity_h is None and app is not None and roster_xlsx:
        try:
            sd_cap = date.fromisoformat(str(sprint["start_date"])[:10])
            ed_cap = date.fromisoformat(str(sprint["end_date"])[:10])
            total_sprint_capacity_h = round(
                compute_team_sprint_capacity_leave_hours(app, roster_xlsx, sd_cap, ed_cap), 2
            )
        except ValueError:
            total_sprint_capacity_h = None

    sd_iso_sprint = str(sprint["start_date"])[:10]
    ed_iso_sprint = str(sprint["end_date"])[:10]
    bd_ctx_export = build_sprint_burndown_chart_context(conn, sprint_id, sd_iso_sprint, ed_iso_sprint)
    png_sprint_snapshot: bytes | None = None
    try:
        from sprint_hub_snapshot_png import build_sprint_hub_snapshot_png_bytes

        png_sprint_snapshot = build_sprint_hub_snapshot_png_bytes(
            team_name=team_name,
            sprint_name=str(sprint["name"]),
            sprint_start=sd_iso_sprint,
            sprint_end=ed_iso_sprint,
            capacity_h=total_sprint_capacity_h,
            bd_ctx=bd_ctx_export,
            member_rows=member_overview,
        )
    except Exception:
        png_sprint_snapshot = None

    wb = Workbook()
    hdr_fill = PatternFill("solid", fgColor="0F172A")
    hdr_font = Font(color="F8FAFC", bold=True, size=11)
    sub_font = Font(bold=True, size=10, color="1E293B")
    title_font = Font(color="F8FAFC", bold=True, size=14)
    title_fill = PatternFill("solid", fgColor="1E3A5F")
    subtitle_font = Font(size=11, color="334155", italic=True)
    thin = Side(style="thin", color="94A3B8")
    hdr_border = Border(left=thin, right=thin, top=thin, bottom=thin)
    grid_border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")
    top = Alignment(vertical="top")

    def style_header_row(ws, row_idx: int, ncols: int) -> None:
        for c in range(1, ncols + 1):
            cell = ws.cell(row=row_idx, column=c)
            cell.fill = hdr_fill
            cell.font = hdr_font
            cell.border = hdr_border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    def autofit(ws: object, max_w: float = 52.0) -> None:
        for col in ws.columns:
            letter = get_column_letter(col[0].column)
            maxlen = 10
            for cell in col:
                if cell.value is None:
                    continue
                maxlen = max(maxlen, min(240, len(str(cell.value))))
            ws.column_dimensions[letter].width = min(max_w, max(10.0, maxlen * 1.05 + 1.0))

    def apply_data_grid(ws: object, min_row: int, max_row: int, ncols: int) -> None:
        if max_row < min_row:
            return
        for r in range(min_row, max_row + 1):
            for c in range(1, ncols + 1):
                ws.cell(row=r, column=c).border = grid_border

    def apply_data_grid_region(ws: object, min_row: int, max_row: int, min_col: int, max_col: int) -> None:
        if max_row < min_row or max_col < min_col:
            return
        for r in range(min_row, max_row + 1):
            for c in range(min_col, max_col + 1):
                ws.cell(row=r, column=c).border = grid_border

    # --- Summary ---
    ws0 = wb.active
    ws0.title = "Summary"
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sd_s = str(sprint["start_date"])[:10]
    ed_s = str(sprint["end_date"])[:10]
    cap_display = f"{total_sprint_capacity_h:.2f}" if total_sprint_capacity_h is not None else "—"

    pairs: list[tuple[str, str]] = [
        ("Sprint name", str(sprint["name"])),
        ("Total Sprint Capacity (h)", cap_display),
        ("Start date", sd_s),
        ("End date", ed_s),
        ("Team", team_name),
        ("Sprint ID", str(int(sprint["id"]))),
        ("Exported at", exported_at),
        ("", ""),
        ("Stickies (count)", str(len(items))),
        ("Done stickies", str(done_n)),
        ("Open / not-done stickies", str(open_n)),
        ("Sum of estimates (h)", f"{tot_est:.2f}"),
        ("Sum of Burnt hours (h)", f"{tot_burnt:.2f}"),
    ]
    if total_sprint_capacity_h is not None and float(total_sprint_capacity_h) > SCRUM_HOUR_EPS:
        capv = float(total_sprint_capacity_h)
        pairs.append(("Planned capacity (sum estimates ÷ sprint capacity, %)", f"{100.0 * tot_est / capv:.1f}%"))
        pairs.append(("Free sprint capacity (sprint capacity − sum estimates, h)", f"{capv - tot_est:.2f}"))
    else:
        pairs.append(("Planned capacity (sum estimates ÷ sprint capacity, %)", "—"))
        pairs.append(("Free sprint capacity (sprint capacity − sum estimates, h)", "—"))
    for col_name, n in sorted(col_counts.items()):
        pairs.append((f"Stickies in column: {col_name}", str(n)))
    while 4 + len(pairs) - 1 > 22 and len(pairs) > 8:
        tail = pairs[-1][0] if pairs else ""
        if isinstance(tail, str) and tail.startswith("Stickies in column:"):
            pairs.pop()
        else:
            break

    ws0.merge_cells(start_row=1, start_column=1, end_row=1, end_column=2)
    ws0.row_dimensions[1].height = 30
    ban = ws0.cell(row=1, column=1, value=f"Sprint export — {str(sprint['name'])}")
    ban.font = title_font
    ban.fill = title_fill
    ban.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws0.merge_cells(start_row=2, start_column=1, end_row=2, end_column=2)
    sub = ws0.cell(row=2, column=1, value=f"{team_name} · {sd_s} → {ed_s}")
    sub.font = subtitle_font
    sub.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    summary_start_row = 4
    for i, (k, v) in enumerate(pairs, start=summary_start_row):
        ws0.cell(row=i, column=1, value=k).font = sub_font if k else Font()
        c2 = ws0.cell(row=i, column=2, value=v)
        c2.alignment = wrap
    ws0.column_dimensions["A"].width = 42
    ws0.column_dimensions["B"].width = 72
    last_summary = summary_start_row + len(pairs) - 1
    apply_data_grid(ws0, summary_start_row, last_summary, 2)

    # Leave tracker grid in columns D onward, row 1+ (alongside A:B sprint metrics).
    weekend_fill_lv = PatternFill("solid", fgColor="312E81")
    last_leave_row = last_summary
    try:
        sdd = date.fromisoformat(sd_s)
        edd = date.fromisoformat(ed_s)
    except ValueError:
        sdd = edd = date.today()
    leave_first_col = 4  # Column D
    weekday_row_lv = 1
    date_row_lv = 2
    member_start_lv = 3
    if app is not None and roster_xlsx:
        days_lv, grid_lv = build_sprint_leave_tracker_context(conn, app, sdd, edd, roster_xlsx)
        if days_lv and grid_lv:
            ncols_lv = len(days_lv) + 1
            last_col_lv = leave_first_col + ncols_lv - 1
            corner = ws0.cell(row=weekday_row_lv, column=leave_first_col, value="")
            corner.border = grid_border
            corner.fill = hdr_fill
            for idx, dm in enumerate(days_lv):
                col = leave_first_col + 1 + idx
                ccell = ws0.cell(row=weekday_row_lv, column=col, value=str(dm["weekday"]).upper())
                ccell.font = hdr_font
                ccell.fill = hdr_fill if not dm["is_weekend"] else weekend_fill_lv
                ccell.border = hdr_border
                ccell.alignment = Alignment(horizontal="center", vertical="center")
            name_hdr = ws0.cell(row=date_row_lv, column=leave_first_col, value="NAME")
            name_hdr.font = hdr_font
            name_hdr.fill = hdr_fill
            name_hdr.border = hdr_border
            name_hdr.alignment = Alignment(horizontal="center", vertical="center")
            for idx, dm in enumerate(days_lv):
                col = leave_first_col + 1 + idx
                ccell = ws0.cell(row=date_row_lv, column=col, value=dm["date"].strftime("%m/%d"))
                ccell.font = hdr_font
                ccell.fill = hdr_fill if not dm["is_weekend"] else weekend_fill_lv
                ccell.border = hdr_border
                ccell.alignment = Alignment(horizontal="center", vertical="center")
            mr = member_start_lv
            max_lv_rows = max(0, 22 - member_start_lv + 1)
            for grow in grid_lv[:max_lv_rows]:
                ws0.cell(row=mr, column=leave_first_col, value=grow["employee"]).alignment = wrap
                ws0.cell(row=mr, column=leave_first_col).border = grid_border
                for idx, cel in enumerate(grow["cells"]):
                    dm = days_lv[idx]
                    col = leave_first_col + 1 + idx
                    ccell = ws0.cell(row=mr, column=col)
                    ccell.border = grid_border
                    if dm["is_weekend"]:
                        ccell.fill = weekend_fill_lv
                    if cel:
                        ccell.value = cel["code"]
                        ccell.alignment = Alignment(horizontal="center", vertical="center")
                        if cel.get("title"):
                            ccell.comment = Comment(str(cel["title"])[:1000], "Leave")
                    else:
                        ccell.value = ""
                mr += 1
            last_leave_row = mr - 1
            apply_data_grid_region(ws0, weekday_row_lv, last_leave_row, leave_first_col, last_col_lv)
            ws0.column_dimensions["D"].width = max(float(ws0.column_dimensions["D"].width or 10), 28)
            for idx in range(len(days_lv)):
                letter = get_column_letter(leave_first_col + 1 + idx)
                ws0.column_dimensions[letter].width = 5.5

    # Member goals & task kinds (only if they fit on or before row 22; charts start at row 23 on Summary).
    last_mg = max(last_summary, last_leave_row)
    r = last_mg + 2
    if r <= 20 and member_goals:
        ws0.cell(row=r, column=1, value="Member sprint goals").font = sub_font
        r += 1
        mg_hdr_row = r
        mg_h = ["Member", "Sprint goal / notes", "Last updated (timestamp)"]
        for j, h in enumerate(mg_h, start=1):
            ws0.cell(row=r, column=j, value=h)
        style_header_row(ws0, mg_hdr_row, len(mg_h))
        r = mg_hdr_row + 1
        max_goal_rows = max(0, 22 - r)
        for mg in member_goals[:max_goal_rows]:
            ws0.cell(row=r, column=1, value=str(mg["employee_name"] or "")).alignment = top
            ws0.cell(row=r, column=2, value=str(mg["goal"] or "")).alignment = wrap
            ws0.cell(row=r, column=3, value=str(mg["updated_at"] or "")).alignment = top
            r += 1
        last_mg = r - 1
        apply_data_grid(ws0, mg_hdr_row, max(mg_hdr_row, last_mg), len(mg_h))

    last_tk = last_mg
    r = last_mg + 2
    if r <= 19 and task_kinds:
        ws0.cell(row=r, column=1, value="Task kinds (team reference)").font = sub_font
        r += 1
        tk_hdr_row = r
        tk_h = ["Code", "Label", "Color (hex)", "Sort order"]
        for j, h in enumerate(tk_h, start=1):
            ws0.cell(row=r, column=j, value=h)
        style_header_row(ws0, tk_hdr_row, len(tk_h))
        r = tk_hdr_row + 1
        max_tk = max(0, 22 - r)
        for tk in task_kinds[:max_tk]:
            ws0.cell(row=r, column=1, value=str(tk["code"] or "")).alignment = top
            ws0.cell(row=r, column=2, value=str(tk["label"] or "")).alignment = wrap
            ws0.cell(row=r, column=3, value=str(tk["color_hex"] or "")).alignment = top
            ws0.cell(row=r, column=4, value=int(tk["sort_order"] or 0)).alignment = top
            r += 1
        last_tk = r - 1
        apply_data_grid(ws0, tk_hdr_row, max(tk_hdr_row, last_tk), len(tk_h))

    ws0.column_dimensions["C"].width = max(float(ws0.column_dimensions["C"].width or 12), 22)
    ws0.column_dimensions["D"].width = max(float(ws0.column_dimensions["D"].width or 10), 14)

    if ws0.max_row > 22:
        ws0.delete_rows(23, ws0.max_row - 22)

    # --- Hidden chart source + single merged chart on Summary (row 23+) ---
    try:
        from openpyxl.chart import BarChart, Reference
        from openpyxl.chart.label import DataLabelList

        ws_cd = wb.create_sheet("_chart_data")
        ws_cd.sheet_state = "hidden"

        def _lbl(code: str) -> str:
            return {
                "ndy": "NDY",
                "fsy": "FSY",
                "code": "CODE",
                "improvement": "Improvement",
                "process_tools": "P&T",
            }.get(code, code)

        cr = 1
        ws_cd.cell(row=cr, column=1, value="Sprint export — chart backing (hidden)")
        cr += 1
        ws_cd.cell(row=cr, column=1, value="Merged: Total sprint + members — est/burnt by kind (h)")
        cr += 1

        k_est, k_brn = _export_roll_kind_est_burnt(items)
        hrow = cr
        ws_cd.cell(row=hrow, column=1, value="Name")
        ncol = 2
        for code in _SUMMARY_EXPORT_KIND_CHART_CODES:
            lab = _lbl(code)
            ws_cd.cell(row=hrow, column=ncol, value=f"{lab} burnt (h)")
            ncol += 1
            ws_cd.cell(row=hrow, column=ncol, value=f"{lab} est (h)")
            ncol += 1
        last_col = ncol - 1
        cr = hrow + 1
        d0 = cr

        def _write_kind_burnt_est_row(name: str, est_d: dict[str, float], brn_d: dict[str, float]) -> None:
            nonlocal cr
            ws_cd.cell(row=cr, column=1, value=name)
            cc = 2
            for code in _SUMMARY_EXPORT_KIND_CHART_CODES:
                ws_cd.cell(row=cr, column=cc, value=round(brn_d.get(code, 0.0), 2))
                cc += 1
                ws_cd.cell(row=cr, column=cc, value=round(est_d.get(code, 0.0), 2))
                cc += 1
            cr += 1

        _write_kind_burnt_est_row("Total sprint", k_est, k_brn)

        max_member_rows = 12
        shown = 0
        for emp in roster_xlsx:
            if shown >= max_member_rows:
                break
            if not any(str(it["assignee"] or "").strip() == emp.strip() for it in items):
                continue
            me_est, me_brn = _export_roll_kind_est_burnt_assignee(items, emp)
            _write_kind_burnt_est_row(str(emp).strip(), me_est, me_brn)
            shown += 1

        d1 = cr - 1
        if d1 >= d0:
            ch = BarChart()
            ch.type = "bar"
            ch.grouping = "clustered"
            ch.title = "Sprint + members — burnt vs est by task kind (h); includes Total sprint"
            ch.x_axis.title = "Hours"
            ch.add_data(
                Reference(ws_cd, min_col=2, min_row=hrow, max_row=d1, max_col=last_col),
                titles_from_data=True,
            )
            ch.set_categories(Reference(ws_cd, min_col=1, min_row=d0, max_row=d1, max_col=1))
            ch.legend.position = "b"
            # Size in cm — generous plot so category names, 10 clustered series, and labels stay readable.
            ncat = d1 - d0 + 1
            ch.height = min(64.0, max(20.0, 16.0 + 1.35 * float(ncat)))
            ch.width = 52
            ch.gapWidth = 90
            try:
                ch.dataLabels = DataLabelList()
                ch.dataLabels.showVal = True
            except Exception:
                pass
            ws0.add_chart(ch, "A23")
    except Exception:
        pass

    # Sprint hub snapshot as PNG on a tab named after the sprint (second sheet).
    if png_sprint_snapshot:
        try:
            from openpyxl.drawing.image import Image as XLImage
            from sprint_hub_snapshot_png import sanitize_excel_sheet_title

            sheet_title = sanitize_excel_sheet_title(str(sprint["name"]))
            reserved = {
                "Summary",
                "SprintStatus",
                "Activity log",
                "Daily tasks Details",
                "Appriciation",
                "_chart_data",
            }
            if sheet_title in reserved:
                sheet_title = (sheet_title[:18] + " hub")[:31]
            if sheet_title in wb.sheetnames:
                sheet_title = (sheet_title[:20] + "_")[:31]
            ws_snap = wb.create_sheet(sheet_title, 1)
            xl_img = XLImage(io.BytesIO(png_sprint_snapshot))
            tw = 920
            if getattr(xl_img, "width", 0) and xl_img.width > 0:
                sc = tw / float(xl_img.width)
                xl_img.width = int(xl_img.width * sc)
                xl_img.height = int(xl_img.height * sc)
            ws_snap.add_image(xl_img, "A1")
        except Exception:
            pass

    # --- Sprint status (FB2610-style: sticky metadata + day-wise logged hours) ---
    ws1 = wb.create_sheet("SprintStatus")
    fb_base_headers = [
        "TEAM",
        "Sticky ID",
        "Title",
        "Task type",
        "Kanban",
        "Owner",
        "Estimate (h)",
        "Burnt total (h)",
        "Burn %",
        "DoD (summary)",
    ]
    sticky_headers = fb_base_headers + [d.isoformat() for d in sprint_calendar_dates]
    nstatcols = len(sticky_headers)
    for j, h in enumerate(sticky_headers, start=1):
        ws1.cell(row=1, column=j, value=h)
    style_header_row(ws1, 1, nstatcols)
    for ri, r in enumerate(items, start=2):
        est = float(r["estimate_hours"] or 0)
        burnt = float(r["total_burnt_hours"] or 0)
        bpct = round(100.0 * burnt / est, 1) if est > SCRUM_HOUR_EPS else ""
        dod = str(r["dod"] or "")
        if len(dod) > 800:
            dod = dod[:797] + "…"
        iid = int(r["id"])
        base_vals: list[object] = [
            team_name,
            iid,
            str(r["title"] or ""),
            str(r["task_kind_label"] or r["task_kind"] or ""),
            str(r["kanban_column"] or ""),
            str(r["assignee"] or ""),
            est,
            burnt,
            bpct,
            dod,
        ]
        for j, val in enumerate(base_vals, start=1):
            cell = ws1.cell(row=ri, column=j, value=val)
            cell.alignment = wrap
            if j in (7, 8):
                cell.number_format = "0.00"
            if j == 9 and val != "":
                cell.number_format = "0.0"
        for j, d in enumerate(sprint_calendar_dates, start=len(base_vals) + 1):
            hv = float(item_day_hours.get(iid, {}).get(d, 0.0))
            ccell = ws1.cell(row=ri, column=j, value=round(hv, 2) if hv > SCRUM_HOUR_EPS else None)
            if hv > SCRUM_HOUR_EPS:
                ccell.number_format = "0.00"
            snips = list(item_day_snips.get(iid, {}).get(d, []))
            if snips:
                ccell.comment = Comment("\n".join(snips)[:999], "scrum")
    ws1.freeze_panes = "A2"
    autofit(ws1)
    apply_data_grid(ws1, 1, max(1, len(items) + 1), nstatcols)

    # --- Activity log (one row per sticky; day-wise hours + comment text) ---
    ws2 = wb.create_sheet("Activity log")
    act_base_headers = ["TEAM", "Sticky ID", "Sticky title", "Owner"]
    act_headers = act_base_headers + [d.isoformat() for d in sprint_calendar_dates]
    nactcols = len(act_headers)
    for j, h in enumerate(act_headers, start=1):
        ws2.cell(row=1, column=j, value=h)
    style_header_row(ws2, 1, nactcols)
    for ri, it in enumerate(items, start=2):
        iid = int(it["id"])
        row0 = [
            team_name,
            iid,
            str(it["title"] or ""),
            str(it["assignee"] or ""),
        ]
        for j, val in enumerate(row0, start=1):
            cell = ws2.cell(row=ri, column=j, value=val)
            cell.alignment = wrap
        for j, d in enumerate(sprint_calendar_dates, start=len(row0) + 1):
            hv = float(item_day_hours.get(iid, {}).get(d, 0.0))
            ccell = ws2.cell(row=ri, column=j, value=round(hv, 2) if hv > SCRUM_HOUR_EPS else None)
            if hv > SCRUM_HOUR_EPS:
                ccell.number_format = "0.00"
            snips = list(item_day_snips.get(iid, {}).get(d, []))
            if snips:
                ccell.comment = Comment("\n".join(snips)[:999], "scrum")
    ws2.freeze_panes = "A2"
    autofit(ws2)
    apply_data_grid(ws2, 1, max(1, len(items) + 1), nactcols)

    # --- Daily tasks Details: sprint team view per member + all stickies + scrum_daily_task rows ---
    def _xlsx_pct_disp(v: float | None) -> str:
        if v is None:
            return "—"
        return f"{float(v):.1f}"

    ws3 = wb.create_sheet("Daily tasks Details")
    rz = 1
    tcell = ws3.cell(row=rz, column=1, value="Daily tasks Details — Sprint team view, stickies, and daily task records")
    tcell.font = Font(bold=True, size=12, color="1E293B")
    rz += 2

    ws3.cell(row=rz, column=1, value="Per-member sprint overview (same metrics as Sprint team page)").font = sub_font
    rz += 1
    memb_headers = (
        ["Team", "Member"]
        + [
            "Sprint burnt %",
            "NDY % burnt (by type)",
            "FSY % burnt (by type)",
            "CODE % burnt (by type)",
            "NDY est (h)",
            "NDY burnt (h)",
            "FSY est (h)",
            "FSY burnt (h)",
            "CODE est (h)",
            "CODE burnt (h)",
            "Improvement % burnt (by type)",
            "Process&Tools % burnt (by type)",
            "Improvement est (h)",
            "Improvement burnt (h)",
            "Process&Tools est (h)",
            "Process&Tools burnt (h)",
            "Queue backlog (B)",
            "Queue do (D)",
            "Queue in progress (Dg)",
            "Queue done (Dn)",
            "Total est all stickies (h)",
            "Total logged (h)",
            "In progress titles (preview)",
        ]
        + [d.strftime("%m/%d") for d in sprint_calendar_dates]
    )
    mem_hdr_row = rz
    for jc, h in enumerate(memb_headers, start=1):
        ws3.cell(row=rz, column=jc, value=h)
    style_header_row(ws3, mem_hdr_row, len(memb_headers))
    rz += 1
    freeze_member_row = rz
    for m in member_overview:
        cnt = m["counts"]
        emp = str(m["name"] or "").strip()
        day_vals = [round(float(assignee_day_hours.get(emp, {}).get(d, 0.0)), 2) for d in sprint_calendar_dates]
        row_mv = [
            team_name,
            emp,
            _xlsx_pct_disp(m.get("progress_pct")),
            _xlsx_pct_disp(m.get("burn_kind_ndy_pct")),
            _xlsx_pct_disp(m.get("burn_kind_fsy_pct")),
            _xlsx_pct_disp(m.get("burn_kind_code_pct")),
            float(m["burn_kind_ndy_est"]),
            float(m["burn_kind_ndy_com"]),
            float(m["burn_kind_fsy_est"]),
            float(m["burn_kind_fsy_com"]),
            float(m["burn_kind_code_est"]),
            float(m["burn_kind_code_com"]),
            _xlsx_pct_disp(m.get("burn_kind_improvement_pct")),
            _xlsx_pct_disp(m.get("burn_kind_process_tools_pct")),
            float(m["burn_kind_improvement_est"]),
            float(m["burn_kind_improvement_com"]),
            float(m["burn_kind_process_tools_est"]),
            float(m["burn_kind_process_tools_com"]),
            int(cnt.get("backlog", 0)),
            int(cnt.get("do", 0)),
            int(cnt.get("doing", 0)),
            int(cnt.get("done", 0)),
            float(m["est_total_hours"]),
            float(m["committed_total_hours"]),
            "; ".join(m.get("doing_preview") or []),
        ] + day_vals
        for jc, val in enumerate(row_mv, start=1):
            cell = ws3.cell(row=rz, column=jc, value=val)
            cell.alignment = wrap
            if isinstance(val, float):
                cell.number_format = "0.00"
        rz += 1
    last_mem = rz - 1
    apply_data_grid(ws3, mem_hdr_row, max(mem_hdr_row, last_mem), len(memb_headers))

    rz += 1
    ws3.cell(row=rz, column=1, value="Every sticky — compact (no duplicate columns vs SprintStatus)").font = sub_font
    rz += 1
    sticky_detail_headers = [
        "Team",
        "Sticky ID",
        "Title",
        "Kanban",
        "Est (h)",
        "Burnt (h)",
        "Burn %",
    ]
    st_hdr_row = rz
    for jc, h in enumerate(sticky_detail_headers, start=1):
        ws3.cell(row=rz, column=jc, value=h)
    style_header_row(ws3, st_hdr_row, len(sticky_detail_headers))
    rz += 1
    for it in items:
        est = float(it["estimate_hours"] or 0)
        burnt = float(it["total_burnt_hours"] or 0)
        sburn = round(100.0 * burnt / est, 1) if est > SCRUM_HOUR_EPS else None
        row_st = [
            team_name,
            int(it["id"]),
            str(it["title"] or ""),
            str(it["kanban_column"] or ""),
            est,
            burnt,
            sburn if sburn is not None else "",
        ]
        for jc, val in enumerate(row_st, start=1):
            cell = ws3.cell(row=rz, column=jc, value=val)
            cell.alignment = wrap
            if jc in (5, 6):
                cell.number_format = "0.00"
            if jc == 7 and val != "":
                cell.number_format = "0.0"
        rz += 1
    last_st = rz - 1
    apply_data_grid(ws3, st_hdr_row, max(st_hdr_row, last_st), len(sticky_detail_headers))

    rz += 1
    ws3.cell(row=rz, column=1, value="Daily task rows (scrum_daily_task table)").font = sub_font
    rz += 1
    daily_headers = [
        "Team",
        "Row ID",
        "Work date",
        "Assignee",
        "Title",
        "Planned (h)",
        "Committed (h)",
        "Actual (h)",
        "Status",
        "Notes",
        "Sort order",
        "Linked sticky ID",
        "Linked sticky title",
        "Created at (timestamp)",
        "Last updated (timestamp)",
    ]
    daily_hdr_row = rz
    for jc, h in enumerate(daily_headers, start=1):
        ws3.cell(row=rz, column=jc, value=h)
    style_header_row(ws3, daily_hdr_row, len(daily_headers))
    rz += 1
    for dr in daily_rows:
        row_d = [
            team_name,
            int(dr["id"]),
            str(dr["work_date"] or ""),
            str(dr["assignee"] or ""),
            str(dr["title"] or ""),
            float(dr["planned_hours"] or 0),
            float(dr["committed_hours"] or 0),
            float(dr["actual_hours"] or 0),
            str(dr["status"] or ""),
            str(dr["notes"] or ""),
            int(dr["sort_order"] or 0),
            int(dr["sprint_item_id"]) if dr["sprint_item_id"] is not None else "",
            str(dr["linked_sticky_title"] or ""),
            str(dr["created_at"] or ""),
            str(dr["updated_at"] or ""),
        ]
        for jc, val in enumerate(row_d, start=1):
            cell = ws3.cell(row=rz, column=jc, value=val)
            cell.alignment = wrap
            if jc in (6, 7, 8):
                cell.number_format = "0.00"
        rz += 1
    last_daily = rz - 1
    apply_data_grid(ws3, daily_hdr_row, max(daily_hdr_row, last_daily), len(daily_headers))

    ws3.freeze_panes = f"A{freeze_member_row}"
    autofit(ws3)

    # Daily tasks Details: allow many rows (day columns + roster); trim only extreme overflow.
    if ws3.max_row > 1200:
        n_del3 = ws3.max_row - 1200
        if n_del3 > 0:
            ws3.delete_rows(1201, n_del3)

    # --- Appriciation: author, comment, title, assignee only (columns C, D, G, H) ---
    ws4 = wb.create_sheet("Appriciation")
    r4 = 1
    ws4.merge_cells(start_row=r4, start_column=3, end_row=r4, end_column=8)
    t4 = ws4.cell(
        row=r4,
        column=3,
        value="Appriciation — one row per manager comment (author, comment, title, assignee)",
    )
    t4.font = Font(bold=True, size=12, color="1E293B")
    t4.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    r4 += 2
    ap_hdr_row = r4
    ap_cols = (
        (3, "Appriciation author"),
        (4, "Appriciation comment"),
        (7, "Title"),
        (8, "Assignee"),
    )
    for col_idx, label in ap_cols:
        c = ws4.cell(row=r4, column=col_idx, value=label)
        c.fill = hdr_fill
        c.font = hdr_font
        c.border = hdr_border
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    r4 += 1
    first_data_row = r4

    def _ap_short_text(val: object, cap: int = 12000) -> str:
        s = str(val if val is not None else "")
        if len(s) > cap:
            return s[: cap - 1] + "…"
        return s

    if not appreciation_export_rows:
        c0 = ws4.cell(row=r4, column=3, value="(No appreciation comments for this sprint.)")
        c0.alignment = wrap
        last_ap = r4
    else:
        for ar in appreciation_export_rows:
            ws4.cell(row=r4, column=3, value=str(ar["appreciation_author"] or "")).alignment = wrap
            ws4.cell(row=r4, column=4, value=_ap_short_text(ar["appreciation_comment"])).alignment = wrap
            ws4.cell(row=r4, column=7, value=str(ar["title"] or "")).alignment = wrap
            ws4.cell(row=r4, column=8, value=str(ar["assignee"] or "")).alignment = wrap
            r4 += 1
        last_ap = r4 - 1

    apply_data_grid_region(ws4, ap_hdr_row, max(ap_hdr_row, last_ap), 3, 4)
    apply_data_grid_region(ws4, ap_hdr_row, max(ap_hdr_row, last_ap), 7, 8)
    ws4.freeze_panes = f"C{first_data_row}"
    for letter, w in (("C", 18.0), ("D", 48.0), ("G", 36.0), ("H", 28.0)):
        ws4.column_dimensions[letter].width = w

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    base = _sanitize_xlsx_base_filename(str(sprint["name"]))
    fname = f"{base}_sprint_{int(sprint_id)}.xlsx"
    return bio.getvalue(), fname


def _sprint_item_completion(conn: sqlite3.Connection, sprint_id: int) -> dict:
    rows = list(
        conn.execute(
            """
            SELECT estimate_hours, status, kanban_column
            FROM scrum_sprint_item WHERE sprint_id = ?
            """,
            (sprint_id,),
        )
    )
    if not rows:
        return {"item_count": 0, "done_count": 0, "pct_done": 0.0, "open_estimate": 0.0}
    n = len(rows)
    done = 0
    open_est = 0.0
    for r in rows:
        col = _normalize_kanban_column(r["kanban_column"] if "kanban_column" in r.keys() else None)
        st = _normalize_scrum_status(r["status"])
        is_done = col == "done" or st == "done"
        if is_done:
            done += 1
        else:
            open_est += float(r["estimate_hours"] or 0)
    return {
        "item_count": n,
        "done_count": done,
        "pct_done": round(100.0 * done / n, 1) if n else 0.0,
        "open_estimate": round(open_est, 2),
    }


def _dashboard_sprint_summaries(conn: sqlite3.Connection, team_id: int, limit: int = 8) -> list[dict]:
    rows = list(
        conn.execute(
            """
            SELECT id, name, start_date, end_date, goal
            FROM scrum_sprint
            WHERE team_id = ?
            ORDER BY end_date DESC, id DESC
            LIMIT ?
            """,
            (int(team_id), int(limit)),
        )
    )
    today = date.today().isoformat()
    out: list[dict] = []
    for r in rows:
        sid = int(r["id"])
        comp = _sprint_item_completion(conn, sid)
        logged = conn.execute(
            """
            SELECT COALESCE(SUM(a.committed_hours), 0) AS h
            FROM scrum_item_activity a
            JOIN scrum_sprint_item i ON i.id = a.item_id
            WHERE i.sprint_id = ?
            """,
            (sid,),
        ).fetchone()
        h = float(logged["h"] or 0) if logged else 0.0
        s0, s1 = str(r["start_date"])[:10], str(r["end_date"])[:10]
        in_window = s0 <= today <= s1
        out.append(
            {
                "id": sid,
                "name": r["name"],
                "start": s0,
                "end": s1,
                "goal": (r["goal"] or "").strip()[:200],
                "pct_done": comp["pct_done"],
                "done_count": comp["done_count"],
                "item_count": comp["item_count"],
                "logged_hours": round(h, 1),
                "in_sprint_window": in_window,
            }
        )
    return out


def _parse_activity_created_at_utc(raw: object) -> datetime | None:
    """Parse scrum_item_activity.created_at (ISO, usually …Z) to aware UTC datetime."""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _last_notes_for_assignee(conn: sqlite3.Connection, sprint_id: int, assignee: str, limit: int = 3) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.body, a.committed_hours, a.from_column, a.to_column, a.created_at, i.title
        FROM scrum_item_activity a
        JOIN scrum_sprint_item i ON i.id = a.item_id
        WHERE i.sprint_id = ? AND i.assignee = ?
          AND (
            trim(a.body) != ''
            OR IFNULL(ABS(a.committed_hours), 0) > 0.00001
            OR lower(trim(COALESCE(a.from_column, ''))) != lower(trim(COALESCE(a.to_column, '')))
          )
        ORDER BY a.created_at DESC
        LIMIT ?
        """,
        (int(sprint_id), assignee, int(limit)),
    ).fetchall()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    out: list[dict] = []
    for r in rows:
        created = _parse_activity_created_at_utc(r["created_at"])
        recent_24h = bool(created is not None and created >= cutoff)
        out.append(
            {
                "body": (r["body"] or "").strip(),
                "committed_hours": float(r["committed_hours"] or 0),
                "from_column": r["from_column"],
                "to_column": r["to_column"],
                "created_at": r["created_at"],
                "title": (r["title"] or "").strip(),
                "recent_24h": recent_24h,
            }
        )
    return out


TEAM_TRACKER_TEAM_BUNDLE_FORMAT = "team_tracker_team_bundle_v1"

_BUNDLE_INSERT_TABLES = frozenset(
    {
        "scrum_team_task_kind",
        "scrum_sprint",
        "scrum_sprint_item",
        "scrum_item_activity",
        "scrum_sprint_member_goal",
        "scrum_daily_task",
        "scrum_item_appreciation",
        "scrum_portal_proposal",
    }
)


def _sqlite_table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _row_to_bundle_dict(r: sqlite3.Row) -> dict[str, object]:
    out: dict[str, object] = {}
    for k in r.keys():
        v = r[k]
        if isinstance(v, bytes):
            out[k] = v.decode("utf-8", errors="replace")
        else:
            out[k] = v
    return out


def _pragma_insert_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if table not in _BUNDLE_INSERT_TABLES:
        raise ValueError(f"Unsupported bundle table {table!r}")
    return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _insert_bundle_row(
    conn: sqlite3.Connection, table: str, row: dict[str, object], overrides: dict[str, object]
) -> int:
    merged: dict[str, object] = {k: v for k, v in row.items() if k != "id"}
    merged.update(overrides)
    pragma_order = _pragma_insert_columns(conn, table)
    cols = [c for c in pragma_order if c != "id" and c in merged]
    if not cols:
        raise ValueError(f"No insertable columns for {table}")
    vals = [merged[c] for c in cols]
    ph = ",".join("?" * len(cols))
    cur = conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})", vals)
    return int(cur.lastrowid)


def build_manager_team_bundle_dict(conn: sqlite3.Connection, team_id: int) -> dict[str, object]:
    """Full Scrum-related snapshot for one team (JSON-serializable)."""
    team_row = conn.execute("SELECT * FROM teams WHERE id = ?", (int(team_id),)).fetchone()
    if not team_row:
        raise ValueError("Team not found")
    tid = int(team_id)
    sprints = list(
        conn.execute("SELECT * FROM scrum_sprint WHERE team_id = ? ORDER BY id", (tid,)).fetchall()
    )
    sid_list = [int(s["id"]) for s in sprints]
    items: list[sqlite3.Row] = []
    if sid_list:
        ph = ",".join("?" * len(sid_list))
        items = list(
            conn.execute(
                f"SELECT * FROM scrum_sprint_item WHERE sprint_id IN ({ph}) ORDER BY id",
                sid_list,
            ).fetchall()
        )
    iid_list = [int(x["id"]) for x in items]
    activity: list[sqlite3.Row] = []
    appreciation: list[sqlite3.Row] = []
    if iid_list:
        ph = ",".join("?" * len(iid_list))
        activity = list(
            conn.execute(
                f"SELECT * FROM scrum_item_activity WHERE item_id IN ({ph}) ORDER BY id",
                iid_list,
            ).fetchall()
        )
        if _sqlite_table_exists(conn, "scrum_item_appreciation"):
            appreciation = list(
                conn.execute(
                    f"SELECT * FROM scrum_item_appreciation WHERE item_id IN ({ph}) ORDER BY id",
                    iid_list,
                ).fetchall()
            )
    member_goals: list[sqlite3.Row] = []
    if sid_list:
        ph = ",".join("?" * len(sid_list))
        member_goals = list(
            conn.execute(
                f"SELECT * FROM scrum_sprint_member_goal WHERE sprint_id IN ({ph})",
                sid_list,
            ).fetchall()
        )
    daily = list(conn.execute("SELECT * FROM scrum_daily_task WHERE team_id = ? ORDER BY id", (tid,)).fetchall())
    roster = list(
        conn.execute(
            "SELECT * FROM team_roster WHERE team_id = ? ORDER BY sort_order, employee_name COLLATE NOCASE",
            (tid,),
        ).fetchall()
    )
    kinds: list[sqlite3.Row] = []
    if _sqlite_table_exists(conn, "scrum_team_task_kind"):
        kinds = list(
            conn.execute(
                "SELECT * FROM scrum_team_task_kind WHERE team_id = ? ORDER BY sort_order, id",
                (tid,),
            ).fetchall()
        )
    proposals: list[sqlite3.Row] = []
    if _sqlite_table_exists(conn, "scrum_portal_proposal"):
        proposals = list(
            conn.execute("SELECT * FROM scrum_portal_proposal WHERE team_id = ? ORDER BY id", (tid,)).fetchall()
        )
    return {
        "format": TEAM_TRACKER_TEAM_BUNDLE_FORMAT,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "team": _row_to_bundle_dict(team_row),
        "team_roster": [_row_to_bundle_dict(r) for r in roster],
        "scrum_team_task_kind": [_row_to_bundle_dict(r) for r in kinds],
        "scrum_sprint": [_row_to_bundle_dict(r) for r in sprints],
        "scrum_sprint_item": [_row_to_bundle_dict(r) for r in items],
        "scrum_item_activity": [_row_to_bundle_dict(r) for r in activity],
        "scrum_sprint_member_goal": [_row_to_bundle_dict(r) for r in member_goals],
        "scrum_daily_task": [_row_to_bundle_dict(r) for r in daily],
        "scrum_item_appreciation": [_row_to_bundle_dict(r) for r in appreciation],
        "scrum_portal_proposal": [_row_to_bundle_dict(r) for r in proposals],
    }


def import_manager_team_bundle(conn: sqlite3.Connection, team_id: int, bundle: dict[str, object]) -> None:
    """Replace this team's Scrum roster + data with the contents of a bundle (same format as export)."""
    if bundle.get("format") != TEAM_TRACKER_TEAM_BUNDLE_FORMAT:
        raise ValueError("Unrecognized bundle format (expected team_tracker_team_bundle_v1).")
    tid = int(team_id)
    if not conn.execute("SELECT 1 FROM teams WHERE id = ?", (tid,)).fetchone():
        raise ValueError("Team not found.")

    if _sqlite_table_exists(conn, "scrum_portal_proposal"):
        conn.execute("DELETE FROM scrum_portal_proposal WHERE team_id = ?", (tid,))
    conn.execute("DELETE FROM scrum_daily_task WHERE team_id = ?", (tid,))
    conn.execute("DELETE FROM scrum_sprint WHERE team_id = ?", (tid,))
    if _sqlite_table_exists(conn, "scrum_team_task_kind"):
        conn.execute("DELETE FROM scrum_team_task_kind WHERE team_id = ?", (tid,))
    conn.execute("DELETE FROM team_roster WHERE team_id = ?", (tid,))

    team_meta = bundle.get("team") or {}
    hub = str(team_meta.get("hub_mode") or "leave").strip()
    if hub not in ("leave", "scrum"):
        hub = "leave"
    conn.execute("UPDATE teams SET hub_mode = ? WHERE id = ?", (hub, tid))

    for row in bundle.get("team_roster") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("employee_name") or "").strip()
        if not name:
            continue
        so = int(row.get("sort_order") or 0)
        conn.execute(
            "INSERT INTO team_roster (team_id, employee_name, sort_order) VALUES (?, ?, ?)",
            (tid, name, so),
        )

    if _sqlite_table_exists(conn, "scrum_team_task_kind"):
        for row in bundle.get("scrum_team_task_kind") or []:
            if not isinstance(row, dict):
                continue
            _insert_bundle_row(conn, "scrum_team_task_kind", row, {"team_id": tid})

    sprint_map: dict[int, int] = {}
    for sp in sorted(bundle.get("scrum_sprint") or [], key=lambda x: int(x["id"])):
        if not isinstance(sp, dict):
            continue
        old_sid = int(sp["id"])
        new_sid = _insert_bundle_row(conn, "scrum_sprint", sp, {"team_id": tid})
        sprint_map[old_sid] = new_sid

    item_map: dict[int, int] = {}
    for it in sorted(bundle.get("scrum_sprint_item") or [], key=lambda x: int(x["id"])):
        if not isinstance(it, dict):
            continue
        old_iid = int(it["id"])
        old_sp = int(it["sprint_id"])
        new_sp = sprint_map.get(old_sp)
        if new_sp is None:
            continue
        new_iid = _insert_bundle_row(conn, "scrum_sprint_item", it, {"sprint_id": new_sp})
        item_map[old_iid] = new_iid

    for row in bundle.get("scrum_item_activity") or []:
        if not isinstance(row, dict):
            continue
        old_item = int(row["item_id"])
        new_item = item_map.get(old_item)
        if new_item is None:
            continue
        _insert_bundle_row(conn, "scrum_item_activity", row, {"item_id": new_item})

    for row in bundle.get("scrum_sprint_member_goal") or []:
        if not isinstance(row, dict):
            continue
        old_sp = int(row["sprint_id"])
        new_sp = sprint_map.get(old_sp)
        if new_sp is None:
            continue
        _insert_bundle_row(conn, "scrum_sprint_member_goal", row, {"sprint_id": new_sp})

    if _sqlite_table_exists(conn, "scrum_item_appreciation"):
        for row in bundle.get("scrum_item_appreciation") or []:
            if not isinstance(row, dict):
                continue
            old_item = int(row["item_id"])
            new_item = item_map.get(old_item)
            if new_item is None:
                continue
            _insert_bundle_row(conn, "scrum_item_appreciation", row, {"item_id": new_item})

    if _sqlite_table_exists(conn, "scrum_portal_proposal"):
        for row in bundle.get("scrum_portal_proposal") or []:
            if not isinstance(row, dict):
                continue
            old_sp = int(row["sprint_id"])
            new_sp = sprint_map.get(old_sp)
            if new_sp is None:
                continue
            ov: dict[str, object] = {"team_id": tid, "sprint_id": new_sp}
            raw_item = row.get("item_id")
            if raw_item is not None:
                old_item = int(raw_item)
                ov["item_id"] = item_map.get(old_item)
            _insert_bundle_row(conn, "scrum_portal_proposal", row, ov)

    for row in bundle.get("scrum_daily_task") or []:
        if not isinstance(row, dict):
            continue
        ov: dict[str, object] = {"team_id": tid}
        raw_sp = row.get("sprint_id")
        if raw_sp is not None:
            ov["sprint_id"] = sprint_map.get(int(raw_sp))
        raw_it = row.get("sprint_item_id")
        if raw_it is not None:
            ov["sprint_item_id"] = item_map.get(int(raw_it))
        _insert_bundle_row(conn, "scrum_daily_task", row, ov)


def _sprint_team_overview_rows(
    app: Flask | None, conn: sqlite3.Connection, team_id: int, sprint_id: int, roster: Sequence[str]
) -> list[dict]:
    _ensure_team_task_kinds(conn, team_id)
    srow = conn.execute(
        "SELECT start_date, end_date FROM scrum_sprint WHERE id = ? AND team_id = ?",
        (int(sprint_id), int(team_id)),
    ).fetchone()
    if not srow:
        return []
    try:
        sd = date.fromisoformat(str(srow["start_date"])[:10])
        ed = date.fromisoformat(str(srow["end_date"])[:10])
    except ValueError:
        sd = ed = date.today()
    out: list[dict] = []
    for emp in roster:
        counts = {c: 0 for c in SCRUM_KANBAN_COLUMNS}
        rows = conn.execute(
            """
            SELECT kanban_column, status FROM scrum_sprint_item
            WHERE sprint_id = ? AND assignee = ?
            """,
            (int(sprint_id), emp),
        ).fetchall()
        for r in rows:
            col = _normalize_kanban_column(r["kanban_column"] if "kanban_column" in r.keys() else None)
            if _normalize_scrum_status(r["status"]) == "done" and col != "done":
                col = "done"
            counts[col] = counts.get(col, 0) + 1
        done_n = counts["done"]
        doing_titles = [
            t[0]
            for t in conn.execute(
                """
                SELECT title FROM scrum_sprint_item
                WHERE sprint_id = ? AND assignee = ? AND lower(trim(kanban_column)) = 'doing'
                ORDER BY sort_order, id LIMIT 4
                """,
                (int(sprint_id), emp),
            ).fetchall()
        ]
        notes = _last_notes_for_assignee(conn, sprint_id, emp, 4)
        est_row = conn.execute(
            """
            SELECT COALESCE(SUM(estimate_hours), 0) AS h
            FROM scrum_sprint_item
            WHERE sprint_id = ? AND assignee = ?
            """,
            (int(sprint_id), emp),
        ).fetchone()
        com_row = conn.execute(
            """
            SELECT COALESCE(SUM(a.committed_hours), 0) AS h
            FROM scrum_item_activity a
            JOIN scrum_sprint_item i ON i.id = a.item_id
            WHERE i.sprint_id = ? AND i.assignee = ?
            """,
            (int(sprint_id), emp),
        ).fetchone()
        est_total = float(est_row["h"] or 0) if est_row else 0.0
        committed_total = float(com_row["h"] or 0) if com_row else 0.0
        if app is not None:
            capacity_available = round(_available_hours_for_assignee_sprint_window(app, emp, sd, ed), 2)
        else:
            capacity_available = round(
                sum(
                    SCRUM_KANBAN_WEEKDAY_HOURS
                    for d in _daterange_inclusive(sd, ed)
                    if d.weekday() < 5
                ),
                2,
            )
        mx = max(est_total, committed_total, capacity_available, 0.01)
        stretch = max(0.0, committed_total - est_total)
        pct_capacity = round(100.0 * capacity_available / mx, 2)
        pct_est = round(100.0 * est_total / mx, 2)
        reg = min(committed_total, est_total)
        pct_committed_regular = round(100.0 * reg / mx, 2)
        pct_stretch = round(100.0 * stretch / mx, 2)
        task_count = sum(int(counts[c]) for c in SCRUM_KANBAN_COLUMNS)
        progress_pct: float | None
        if task_count > 0 and est_total > SCRUM_HOUR_EPS:
            progress_pct = round(100.0 * committed_total / est_total, 1)
        else:
            progress_pct = None
        kind_est = {code: 0.0 for code in SCRUM_TASK_KIND_CODES}
        kind_com = {code: 0.0 for code in SCRUM_TASK_KIND_CODES}
        for r in conn.execute(
            """
            SELECT task_kind, SUM(estimate_hours) AS h
            FROM scrum_sprint_item
            WHERE sprint_id = ? AND assignee = ?
            GROUP BY task_kind
            """,
            (int(sprint_id), emp),
        ).fetchall():
            code = _resolve_task_kind_code(conn, team_id, str(r["task_kind"] or ""))
            if code in kind_est:
                kind_est[code] += float(r["h"] or 0)
        for r in conn.execute(
            """
            SELECT i.task_kind, SUM(a.committed_hours) AS h
            FROM scrum_item_activity a
            JOIN scrum_sprint_item i ON i.id = a.item_id
            WHERE i.sprint_id = ? AND i.assignee = ?
            GROUP BY i.task_kind
            """,
            (int(sprint_id), emp),
        ).fetchall():
            code = _resolve_task_kind_code(conn, team_id, str(r["task_kind"] or ""))
            if code in kind_com:
                kind_com[code] += float(r["h"] or 0)

        def _kind_burn_pct(est: float, com: float) -> float | None:
            if est > SCRUM_HOUR_EPS:
                return round(100.0 * com / est, 1)
            return None

        burn_ndy_pct = _kind_burn_pct(kind_est["ndy"], kind_com["ndy"])
        burn_fsy_pct = _kind_burn_pct(kind_est["fsy"], kind_com["fsy"])
        burn_code_pct = _kind_burn_pct(kind_est["code"], kind_com["code"])
        burn_improvement_pct = _kind_burn_pct(kind_est["improvement"], kind_com["improvement"])
        burn_process_tools_pct = _kind_burn_pct(kind_est["process_tools"], kind_com["process_tools"])
        any_kind_est = any(kind_est[c] > SCRUM_HOUR_EPS for c in SCRUM_TASK_KIND_CODES)
        show_kind_burn_breakdown = bool(
            progress_pct is not None
            and progress_pct < SCRUM_TEAM_KIND_BURN_BREAKDOWN_BELOW_PCT
            and any_kind_est
        )
        collapsed_kind_lines: list[dict[str, object]] = []
        _kind_lbl = {
            "ndy": "NDY",
            "fsy": "FSY",
            "code": "CODE",
            "process_tools": "Process&Tools",
            "improvement": "Improvement",
        }
        for _code in SPRINT_TEAM_KIND_STACK_ORDER:
            est_k = float(kind_est[_code])
            if est_k <= SCRUM_HOUR_EPS:
                continue
            com_k = float(kind_com[_code])
            collapsed_kind_lines.append(
                {
                    "label": _kind_lbl.get(_code, _code.upper()),
                    "pct": _kind_burn_pct(est_k, com_k),
                    "com": round(com_k, 2),
                    "est": round(est_k, 2),
                }
            )
            if len(collapsed_kind_lines) >= 3:
                break
        out.append(
            {
                "name": emp,
                "counts": counts,
                "done_total": done_n,
                "doing_preview": doing_titles,
                "last_notes": notes,
                "est_total_hours": round(est_total, 2),
                "committed_total_hours": round(committed_total, 2),
                "capacity_available_hours": capacity_available,
                "stretch_load_hours": round(stretch, 2),
                "effort_scale_max": round(mx, 2),
                "pct_capacity_bar": pct_capacity,
                "pct_est_bar": pct_est,
                "pct_committed_regular_bar": pct_committed_regular,
                "pct_stretch_bar": pct_stretch,
                "task_count": task_count,
                "progress_pct": progress_pct,
                "burn_kind_ndy_pct": burn_ndy_pct,
                "burn_kind_fsy_pct": burn_fsy_pct,
                "burn_kind_code_pct": burn_code_pct,
                "burn_kind_improvement_pct": burn_improvement_pct,
                "burn_kind_process_tools_pct": burn_process_tools_pct,
                "burn_kind_ndy_est": round(kind_est["ndy"], 2),
                "burn_kind_ndy_com": round(kind_com["ndy"], 2),
                "burn_kind_fsy_est": round(kind_est["fsy"], 2),
                "burn_kind_fsy_com": round(kind_com["fsy"], 2),
                "burn_kind_code_est": round(kind_est["code"], 2),
                "burn_kind_code_com": round(kind_com["code"], 2),
                "burn_kind_improvement_est": round(kind_est["improvement"], 2),
                "burn_kind_improvement_com": round(kind_com["improvement"], 2),
                "burn_kind_process_tools_est": round(kind_est["process_tools"], 2),
                "burn_kind_process_tools_com": round(kind_com["process_tools"], 2),
                "show_kind_burn_breakdown": show_kind_burn_breakdown,
                "collapsed_kind_lines": collapsed_kind_lines,
            }
        )
    return out


def _load_kanban_cards(conn: sqlite3.Connection, sprint_id: int, assignee: str, team_id: int) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {c: [] for c in SCRUM_KANBAN_COLUMNS}
    _ensure_team_task_kinds(conn, team_id)
    for r in conn.execute(
        """
        SELECT i.id, i.title, i.estimate_hours, i.status, i.notes, i.dod, i.done_artifacts, i.sort_order, i.kanban_column, i.task_kind,
               i.sticky_color_hex, i.area, k.label AS kind_label, k.color_hex AS kind_color_hex
        FROM scrum_sprint_item i
        LEFT JOIN scrum_team_task_kind k ON k.team_id = ? AND k.code = i.task_kind
        WHERE i.sprint_id = ? AND i.assignee = ?
        ORDER BY i.sort_order ASC, i.id ASC
        """,
        (int(team_id), int(sprint_id), assignee),
    ):
        col = _normalize_kanban_column(r["kanban_column"] if "kanban_column" in r.keys() else None)
        if _normalize_scrum_status(r["status"]) == "done" and col != "done":
            col = "done"
        raw_kind = (r["task_kind"] or "").strip()
        code = _coerce_sprint_item_task_kind(conn, team_id, raw_kind or None)
        sticky_hex = (r["sticky_color_hex"] or "").strip() if "sticky_color_hex" in r.keys() else ""
        kind_color = (r["kind_color_hex"] or "").strip() if "kind_color_hex" in r.keys() else ""
        if sticky_hex and _SCRUM_HEX_COLOR.match(sticky_hex):
            kind_color = _normalize_hex_color(sticky_hex)
        elif not kind_color:
            kind_color = _task_kind_color_for_item(conn, team_id, code, None)
        kind_label = (r["kind_label"] or "").strip() if "kind_label" in r.keys() else ""
        if not kind_label:
            kind_label = code.upper()
        da_list = _parse_done_artifacts_db(
            str(r["done_artifacts"] or "[]") if "done_artifacts" in r.keys() else "[]"
        )
        buckets[col].append(
            {
                "id": int(r["id"]),
                "title": (r["title"] or "").strip(),
                "estimate_hours": float(r["estimate_hours"] or 0),
                "notes": (r["notes"] or "").strip(),
                "dod": (r["dod"] or "").strip() if "dod" in r.keys() else "",
                "done_artifacts": da_list,
                "done_artifacts_lines": _done_artifacts_to_lines(da_list),
                "task_kind": code,
                "kind_label": kind_label,
                "kind_color_hex": kind_color,
                "area": (str(r["area"] or "").strip()[:SCRUM_STICKY_AREA_MAX_LEN]),
                "sticky_color_hex_raw": _normalize_hex_color(sticky_hex)
                if sticky_hex and _SCRUM_HEX_COLOR.match(sticky_hex)
                else "",
                "column": col,
            }
        )
    _enrich_doing_kanban_cards(conn, buckets["doing"])
    _enrich_kanban_appreciation(conn, buckets)
    return buckets


def _enrich_kanban_appreciation(conn: sqlite3.Connection, buckets: dict[str, list[dict]]) -> None:
    """Attach appreciation_count and appreciation_preview (latest) for Well Done comments."""
    all_cards: list[dict] = [c for col in buckets.values() for c in col]
    for c in all_cards:
        c["appreciation_count"] = 0
        c["appreciation_preview"] = ""
        c["appreciation_messages"] = []
    if not all_cards:
        return
    ids = sorted({int(c["id"]) for c in all_cards})
    if not ids:
        return
    ph = ",".join("?" * len(ids))
    counts = {
        int(r["item_id"]): int(r["c"] or 0)
        for r in conn.execute(
            f"SELECT item_id, COUNT(*) AS c FROM scrum_item_appreciation WHERE item_id IN ({ph}) GROUP BY item_id",
            ids,
        )
    }
    previews: dict[int, str] = {}
    for r in conn.execute(
        f"""
        SELECT a.item_id, a.comment, a.author
        FROM scrum_item_appreciation a
        INNER JOIN (
            SELECT item_id, MAX(id) AS mid FROM scrum_item_appreciation WHERE item_id IN ({ph}) GROUP BY item_id
        ) t ON a.id = t.mid
        """,
        ids,
    ):
        iid = int(r["item_id"])
        body = (str(r["comment"] or "")).strip()
        auth = (str(r["author"] or "")).strip()
        line = f"{auth}: {body}" if auth else body
        previews[iid] = line[:500] if line else "Well done"
    # Fetch all appreciation messages for portal display
    all_messages: dict[int, list[dict]] = {i: [] for i in ids}
    for r in conn.execute(
        f"""
        SELECT item_id, author, comment, created_at
        FROM scrum_item_appreciation
        WHERE item_id IN ({ph})
        ORDER BY created_at ASC, id ASC
        """,
        ids,
    ):
        iid2 = int(r["item_id"])
        if iid2 in all_messages:
            all_messages[iid2].append({
                "author": (str(r["author"] or "")).strip(),
                "comment": (str(r["comment"] or "")).strip(),
                "created_at_display": _format_activity_ts(str(r["created_at"] or "")),
            })
    for c in all_cards:
        iid = int(c["id"])
        n = counts.get(iid, 0)
        c["appreciation_count"] = n
        if n:
            c["appreciation_preview"] = previews.get(iid, "Appreciation")
        c["appreciation_messages"] = all_messages.get(iid, [])


def _format_activity_ts(raw: str) -> str:
    s = (raw or "").strip().replace("+00:00", "Z")
    if not s:
        return ""
    if "T" in s:
        date_part, rest = s.split("T", 1)
        time_part = rest.replace("Z", "").split(".")[0][:8]
        if len(time_part) >= 5:
            time_part = time_part[:5]
        return f"{date_part} {time_part} UTC"
    return s[:19]


def _strip_appended_standup_lines_from_notes(notes: str) -> str:
    """Remove legacy lines appended by older stand-up saves (started with [YYYY-MM-DD])."""
    lines = (notes or "").split("\n")
    kept: list[str] = []
    for line in lines:
        s = line.strip()
        if len(s) >= 12 and s.startswith("[") and s[1:11].count("-") == 2 and s[11] == "]":
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _enrich_doing_kanban_cards(conn: sqlite3.Connection, doing_cards: list[dict]) -> None:
    if not doing_cards:
        return
    ids = [int(c["id"]) for c in doing_cards]
    ph = ",".join("?" * len(ids))
    sums = {
        int(r["item_id"]): float(r["h"] or 0)
        for r in conn.execute(
            f"SELECT item_id, COALESCE(SUM(committed_hours), 0) AS h FROM scrum_item_activity WHERE item_id IN ({ph}) GROUP BY item_id",
            ids,
        )
    }
    standups: dict[int, list[dict]] = {i: [] for i in ids}
    for r in conn.execute(
        f"""
        SELECT id, item_id, body, committed_hours, created_at
        FROM scrum_item_activity
        WHERE item_id IN ({ph}) AND lower(trim(from_column)) = 'doing' AND lower(trim(to_column)) = 'doing'
        ORDER BY created_at ASC, id ASC
        """,
        ids,
    ):
        iid = int(r["item_id"])
        if iid not in standups:
            continue
        standups[iid].append(
            {
                "id": r["id"],
                "body": (r["body"] or "").strip(),
                "committed_hours": float(r["committed_hours"] or 0),
                "created_at": str(r["created_at"] or ""),
                "created_at_display": _format_activity_ts(str(r["created_at"] or "")),
            }
        )
    for c in doing_cards:
        iid = int(c["id"])
        est = float(c.get("estimate_hours") or 0)
        committed = float(sums.get(iid, 0.0))
        mx = max(est, committed, 0.01)
        stretch = max(0.0, committed - est)
        pct_est = round(100.0 * est / mx, 2)
        reg = min(committed, est)
        pct_committed_regular = round(100.0 * reg / mx, 2)
        pct_stretch = round(100.0 * stretch / mx, 2)
        burn_pct: float | None
        if est > SCRUM_HOUR_EPS:
            burn_pct = round(100.0 * committed / est, 1)
        else:
            burn_pct = None
        c["committed_logged_hours"] = round(committed, 2)
        c["burn_pct"] = burn_pct
        c["effort_scale_max"] = round(mx, 2)
        c["pct_est_bar"] = pct_est
        c["pct_committed_regular_bar"] = pct_committed_regular
        c["pct_stretch_bar"] = pct_stretch
        c["stretch_load_hours"] = round(stretch, 2)
        c["standup_updates"] = standups.get(iid, [])
        c["notes_display"] = _strip_appended_standup_lines_from_notes(str(c.get("notes") or ""))


def _item_belongs_to_sprint_team(
    conn: sqlite3.Connection, item_id: int, sprint_id: int, team_id: int, assignee: str
) -> bool:
    row = conn.execute(
        """
        SELECT i.id FROM scrum_sprint_item i
        JOIN scrum_sprint s ON s.id = i.sprint_id
        WHERE i.id = ? AND i.sprint_id = ? AND s.team_id = ? AND i.assignee = ?
        """,
        (int(item_id), int(sprint_id), int(team_id), assignee),
    ).fetchone()
    return row is not None


def _sprint_team_id(conn: sqlite3.Connection, sprint_id: int) -> int | None:
    row = conn.execute("SELECT team_id FROM scrum_sprint WHERE id = ?", (int(sprint_id),)).fetchone()
    if not row:
        return None
    return int(row["team_id"])


SCRUM_PORTAL_PROPOSAL_ACTIONS: frozenset[str] = frozenset(
    {
        "item_move",
        "item_note",
        "item_add",
        "item_update_do",
        "item_update_done",
        "item_delete",
        "portal_checklist",
    }
)


def _approved_portal_activity_note(note: str) -> str:
    b = (note or "").strip()
    p = "[approved employee change] "
    if b.startswith(p):
        return b[:2000]
    return (p + b)[:2000]


def _insert_scrum_portal_proposal(
    conn: sqlite3.Connection,
    team_id: int,
    sprint_id: int,
    item_id: int | None,
    action: str,
    proposer_roster_name: str,
    payload: dict,
) -> None:
    if action not in SCRUM_PORTAL_PROPOSAL_ACTIONS:
        raise ValueError("bad proposal action")
    ts = _utc_stamp()
    cursor = conn.execute(
        """
        INSERT INTO scrum_portal_proposal
        (team_id, sprint_id, item_id, proposer_roster_name, action, payload_json, status, created_at, resolved_at, resolution_note)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL, '')
        """,
        (
            int(team_id),
            int(sprint_id),
            int(item_id) if item_id is not None else None,
            proposer_roster_name,
            action,
            json.dumps(payload),
            ts,
        ),
    )
    
    # Auto-approve logic: give employees full access
    proposal_id = cursor.lastrowid
    prop = conn.execute("SELECT * FROM scrum_portal_proposal WHERE id = ?", (proposal_id,)).fetchone()
    if prop:
        ok, err = _apply_scrum_portal_proposal_core(conn, prop, int(team_id))
        if ok:
            conn.execute(
                "UPDATE scrum_portal_proposal SET status = 'approved', resolved_at = ?, resolution_note = 'Auto-approved for employee full access' WHERE id = ?",
                (_utc_stamp(), proposal_id)
            )


def _count_pending_scrum_portal_proposals(conn: sqlite3.Connection, team_id: int, sprint_id: int | None = None) -> int:
    if sprint_id is None:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM scrum_portal_proposal WHERE team_id = ? AND status = 'pending'",
            (int(team_id),),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM scrum_portal_proposal WHERE team_id = ? AND sprint_id = ? AND status = 'pending'",
            (int(team_id), int(sprint_id)),
        ).fetchone()
    return int(row["c"]) if row else 0


def _count_pending_scrum_portal_proposals_for_proposer(conn: sqlite3.Connection, roster_name: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM scrum_portal_proposal WHERE proposer_roster_name = ? AND status = 'pending'",
        (roster_name,),
    ).fetchone()
    return int(row["c"]) if row else 0


def _portal_proposal_summary_line(action: str, payload: dict, item_title: str | None) -> str:
    t = (item_title or "").strip() or "(item)"
    if action == "item_move":
        col = _normalize_kanban_column(payload.get("to_column"))
        return f"Move «{t}» → {col}"
    if action == "item_note":
        return f"Stand-up / hours on «{t}»"
    if action == "item_add":
        title = (payload.get("title") or "").strip() or "New sticky"
        return f"Add sticky in Do: «{title}»"
    if action == "item_update_do":
        return f"Edit plan (Do) for «{t}»"
    if action == "item_update_done":
        return f"Edit Done details for «{t}»"
    if action == "item_delete":
        return f"Delete «{t}»"
    if action == "portal_checklist":
        return f"My sprint checklist update for «{t}»"
    return action


def _kanban_column_public_label(col: str | None) -> str:
    c = _normalize_kanban_column(col)
    return {"backlog": "To do", "do": "Do", "doing": "In progress", "done": "Done"}.get(c, c or "—")


def _pending_portal_proposals_grouped_for_sprint(
    conn: sqlite3.Connection, team_id: int, sprint_id: int
) -> dict[str, list[dict]]:
    """Pending portal proposals for this sprint, grouped by employee roster name (for team overview)."""
    action_labels = {
        "item_move": "Column move",
        "item_note": "Stand-up / hours",
        "item_add": "New sticky",
        "item_update_do": "Edit plan (Do)",
        "item_update_done": "Edit Done",
        "item_delete": "Delete sticky",
        "portal_checklist": "Status & DoD",
    }
    rows = list(
        conn.execute(
            """
            SELECT p.id, p.item_id, p.proposer_roster_name, p.action, p.payload_json, p.created_at,
              (SELECT title FROM scrum_sprint_item WHERE id = p.item_id) AS item_title
            FROM scrum_portal_proposal p
            WHERE p.team_id = ? AND p.sprint_id = ? AND p.status = 'pending'
            ORDER BY p.created_at ASC
            """,
            (int(team_id), int(sprint_id)),
        )
    )
    by: dict[str, list[dict]] = {}
    for r in rows:
        emp = (r["proposer_roster_name"] or "").strip()
        if not emp:
            continue
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        it = None
        if "item_title" in r.keys() and r["item_title"] is not None:
            it = (str(r["item_title"]) or "").strip() or None
        action = str(r["action"] or "")
        summary = _portal_proposal_summary_line(action, payload, it)
        card: dict = {
            "id": int(r["id"]),
            "created_at": str(r["created_at"] or ""),
            "created_at_short": (str(r["created_at"] or "")[:16].replace("T", " ")),
            "action": action,
            "action_label": action_labels.get(action, action),
            "summary": summary,
            "item_title": it or ("(new sticky)" if action == "item_add" else "(item)"),
            "dod_rows": None,
            "dod_all_met": None,
            "dod_checked_count": None,
            "note_preview": None,
            "target_column_label": None,
            "hours_hint": None,
            "estimate_hint": None,
            "from_column_label": None,
            "last_sync_body": None,
            "last_sync_at_short": None,
            "last_sync_hours": None,
            "artifact_items": [],
            "verification_urls": [],
            "proposed_standup_note": None,
        }
        da_raw = payload.get("done_artifacts_json")
        if isinstance(da_raw, str) and da_raw.strip():
            card["artifact_items"] = _parse_done_artifacts_db(da_raw)
        card["verification_urls"] = _normalize_verification_urls_mixed(payload.get("verification_urls"))
        if action == "portal_checklist":
            dod_json = (payload.get("portal_dod_json") or "[]").strip()
            states = _portal_dod_parse(dod_json)
            card["dod_rows"] = [
                {
                    "label": PORTAL_DOD_CHECKLIST_LABELS[i],
                    "checked": bool(states[i]) if i < len(states) else False,
                }
                for i in range(len(PORTAL_DOD_CHECKLIST_LABELS))
            ]
            card["dod_all_met"] = _portal_dod_all_done(states)
            card["dod_checked_count"] = sum(1 for x in card["dod_rows"] if x.get("checked"))
            card["target_column_label"] = _kanban_column_public_label(str(payload.get("kanban_column")))
        if action == "item_move":
            card["target_column_label"] = _kanban_column_public_label(str(payload.get("to_column")))
            card["note_preview"] = (payload.get("note") or "").strip()[:160]
            raw_ch = payload.get("committed_hours")
            ch: float | None
            try:
                ch = float(raw_ch) if raw_ch is not None and raw_ch != "" else None
            except (TypeError, ValueError):
                ch = 0.0
            if ch is not None and abs(ch) > SCRUM_HOUR_EPS:
                card["hours_hint"] = f"+{ch:g}h on this move"
        if action == "item_note":
            note_full = (payload.get("note") or "").strip()
            card["proposed_standup_note"] = note_full
            card["note_preview"] = note_full[:160]
            try:
                ch = float(payload.get("committed_hours", 0) or 0)
            except (TypeError, ValueError):
                ch = 0.0
            if abs(ch) > SCRUM_HOUR_EPS:
                card["hours_hint"] = f"+{ch:g}h"
        if action == "item_add":
            card["note_preview"] = (payload.get("title") or "").strip()[:160]
            try:
                est = float(payload.get("estimate_hours", 0) or 0)
            except (TypeError, ValueError):
                est = 0.0
            if est > SCRUM_HOUR_EPS:
                card["estimate_hint"] = f"{est:g}h estimate"
        if action in ("item_update_do", "item_update_done"):
            card["note_preview"] = (payload.get("notes") or "").strip()[:120]
        raw_item = r["item_id"]
        item_id_int = int(raw_item) if raw_item is not None else None
        if item_id_int is not None:
            crow = conn.execute(
                "SELECT kanban_column FROM scrum_sprint_item WHERE id = ? AND sprint_id = ?",
                (item_id_int, int(sprint_id)),
            ).fetchone()
            if crow:
                card["from_column_label"] = _kanban_column_public_label(str(crow["kanban_column"]))
            act = conn.execute(
                """
                SELECT body, created_at, committed_hours
                FROM scrum_item_activity
                WHERE item_id = ? AND from_column = 'doing' AND to_column = 'doing'
                ORDER BY id DESC LIMIT 1
                """,
                (item_id_int,),
            ).fetchone()
            if act:
                card["last_sync_body"] = (str(act["body"] or "")).strip()
                card["last_sync_at_short"] = (str(act["created_at"] or "")[:16].replace("T", " "))
                try:
                    lh = float(act["committed_hours"] or 0)
                except (TypeError, ValueError):
                    lh = 0.0
                if abs(lh) > SCRUM_HOUR_EPS:
                    card["last_sync_hours"] = lh
        by.setdefault(emp, []).append(card)
    return by


def _redirect_after_portal_proposal(next_raw: str | None):
    n = (next_raw or "").strip()
    if n.startswith("/") and not n.startswith("//") and ".." not in n and "\n" not in n and "\r" not in n:
        return redirect(n)
    return redirect(url_for("scrum_portal_proposals"))


def _apply_scrum_portal_proposal_core(
    conn: sqlite3.Connection, proposal: sqlite3.Row, manager_team_id: int
) -> tuple[bool, str | None]:
    if int(proposal["team_id"]) != int(manager_team_id):
        return False, "team"
    if (proposal["status"] or "").strip().lower() != "pending":
        return False, "not_pending"
    action = (proposal["action"] or "").strip()
    if action not in SCRUM_PORTAL_PROPOSAL_ACTIONS:
        return False, "bad_action"
    sprint_id = int(proposal["sprint_id"])
    team_id = int(proposal["team_id"])
    if _sprint_is_closed_by_id(conn, sprint_id):
        return False, "sprint_closed"
    proposer = (proposal["proposer_roster_name"] or "").strip()
    try:
        payload = json.loads(proposal["payload_json"] or "{}")
    except json.JSONDecodeError:
        return False, "bad_payload"
    raw_item_id = proposal["item_id"]
    item_id: int | None = int(raw_item_id) if raw_item_id is not None else None
    ts = _utc_stamp()

    if action == "item_move":
        if item_id is None:
            return False, "missing_item"
        if not _item_belongs_to_sprint_team(conn, item_id, sprint_id, team_id, proposer):
            return False, "forbidden"
        to_col = _normalize_kanban_column(payload.get("to_column"))
        note_raw = (payload.get("note") or "").strip()[:2000]
        body = _approved_portal_activity_note(note_raw)
        row = conn.execute(
            "SELECT kanban_column FROM scrum_sprint_item WHERE id = ? AND sprint_id = ?",
            (item_id, sprint_id),
        ).fetchone()
        if not row:
            return False, "item_gone"
        from_col = _normalize_kanban_column(row["kanban_column"] if row else None)
        if from_col == to_col:
            return True, None
        st = _status_for_kanban_column(to_col)
        if from_col == "do" and to_col == "doing":
            conn.execute("DELETE FROM scrum_item_activity WHERE item_id = ?", (item_id,))
            ch = _parse_committed_hours_for_kanban_move(
                payload.get("committed_hours"), from_col=from_col, to_col=to_col
            )
        elif from_col == "doing" and to_col == "do":
            conn.execute("DELETE FROM scrum_item_activity WHERE item_id = ?", (item_id,))
            ch = 0.0
        else:
            ch = _parse_committed_hours_for_kanban_move(
                payload.get("committed_hours"), from_col=from_col, to_col=to_col
            )
        artifacts_json: str | None = None
        if to_col == "done":
            raw_art = payload.get("done_artifacts_json")
            if raw_art is not None and isinstance(raw_art, str):
                artifacts_json = raw_art
            else:
                artifacts_json = "[]"
        if to_col == "done" and artifacts_json is not None:
            conn.execute(
                """
                UPDATE scrum_sprint_item
                SET kanban_column = ?, status = ?, updated_at = ?, done_artifacts = ?
                WHERE id = ? AND sprint_id = ?
                """,
                (to_col, st, ts, artifacts_json, item_id, sprint_id),
            )
        else:
            conn.execute(
                """
                UPDATE scrum_sprint_item
                SET kanban_column = ?, status = ?, updated_at = ?
                WHERE id = ? AND sprint_id = ?
                """,
                (to_col, st, ts, item_id, sprint_id),
            )
        conn.execute(
            """
            INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id, body, ch, from_col, to_col, ts),
        )
        return True, None

    if action == "item_note":
        if item_id is None:
            return False, "missing_item"
        if not _item_belongs_to_sprint_team(conn, item_id, sprint_id, team_id, proposer):
            return False, "forbidden"
        row = conn.execute(
            "SELECT kanban_column FROM scrum_sprint_item WHERE id = ? AND sprint_id = ?",
            (item_id, sprint_id),
        ).fetchone()
        if not row or _normalize_kanban_column(row["kanban_column"]) != "doing":
            return False, "only_doing"
        note_raw = (payload.get("note") or "").strip()[:2000]
        ch = _parse_hours_field(str(payload.get("committed_hours", "0")))
        if not note_raw and ch <= SCRUM_HOUR_EPS:
            return False, "missing"
        body = _approved_portal_activity_note(note_raw)
        conn.execute(
            "UPDATE scrum_sprint_item SET updated_at = ? WHERE id = ?",
            (ts, item_id),
        )
        conn.execute(
            """
            INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
            VALUES (?, ?, ?, 'doing', 'doing', ?)
            """,
            (item_id, body, ch, ts),
        )
        return True, None

    if action == "item_activity_update":
        if item_id is None:
            return False, "missing_item"
        if not _item_belongs_to_sprint_team(conn, item_id, sprint_id, team_id, proposer):
            return False, "forbidden"
        activity_id = payload.get("activity_id")
        if not activity_id:
            return False, "missing_activity_id"
        note_raw = (payload.get("note") or "").strip()[:2000]
        ch = _parse_hours_field(str(payload.get("committed_hours", "0")))
        body = _approved_portal_activity_note(note_raw)
        conn.execute(
            "UPDATE scrum_item_activity SET body = ?, committed_hours = ? WHERE id = ? AND item_id = ?",
            (body, ch, activity_id, item_id),
        )
        conn.execute(
            "UPDATE scrum_sprint_item SET updated_at = ? WHERE id = ?",
            (ts, item_id),
        )
        return True, None

    if action == "item_add":
        if not _sprint_row_for_team(conn, sprint_id, team_id):
            return False, "sprint_gone"
        title = (payload.get("title") or "").strip()[:500]
        notes = (payload.get("notes") or "").strip()[:2000]
        dod = (payload.get("dod") or "").strip()[:4000]
        est = _parse_hours_field(str(payload.get("estimate_hours", "0")))
        raw_tc = (payload.get("task_kind_code") or "ndy").strip().lower()[:64]
        if raw_tc in SCRUM_LEGACY_TASK_KIND_MAP:
            raw_tc = SCRUM_LEGACY_TASK_KIND_MAP[raw_tc]
        tcode = raw_tc if raw_tc in SCRUM_TASK_KIND_CODES else "ndy"
        if not title or est <= SCRUM_HOUR_EPS:
            return False, "missing"
        area = (payload.get("area") or "").strip()[:SCRUM_STICKY_AREA_MAX_LEN]
        _ensure_team_task_kinds(conn, team_id)
        st = _status_for_kanban_column("do")
        mx_row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM scrum_sprint_item WHERE sprint_id = ?",
            (sprint_id,),
        ).fetchone()
        mx = int(mx_row["n"] if mx_row is not None else 0)
        conn.execute(
            """
            INSERT INTO scrum_sprint_item
            (sprint_id, assignee, title, estimate_hours, status, notes, dod, sort_order, created_at, updated_at, kanban_column, task_kind, sticky_color_hex, area)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'do', ?, ?, ?)
            """,
            (sprint_id, proposer, title, est, st, notes, dod, mx, ts, ts, tcode, None, area),
        )
        return True, None

    if action == "item_update_do":
        if item_id is None:
            return False, "missing_item"
        row = conn.execute(
            "SELECT assignee, kanban_column, sticky_color_hex FROM scrum_sprint_item WHERE id = ? AND sprint_id = ?",
            (item_id, sprint_id),
        ).fetchone()
        if not row or (row["assignee"] or "").strip() != proposer:
            return False, "forbidden"
        cur_col = _normalize_kanban_column(row["kanban_column"] if "kanban_column" in row.keys() else None)
        if cur_col != "do":
            return False, "wrong_column"
        title = (payload.get("title") or "").strip()[:500]
        if not title:
            return False, "missing"
        est = _parse_hours_field(str(payload.get("estimate_hours", "0")))
        if est <= SCRUM_HOUR_EPS:
            return False, "estimate_required"
        tkind = _coerce_sprint_item_task_kind(conn, team_id, (payload.get("task_kind") or "ndy").strip()[:64])
        notes = (payload.get("notes") or "").strip()[:2000]
        dod = (payload.get("dod") or "").strip()[:4000]
        area = (payload.get("area") or "").strip()[:SCRUM_STICKY_AREA_MAX_LEN]
        sticky_hex = None
        st = _status_for_kanban_column(cur_col)
        conn.execute(
            """
            UPDATE scrum_sprint_item SET
                title = ?, estimate_hours = ?, task_kind = ?, notes = ?, dod = ?, status = ?, updated_at = ?, sticky_color_hex = ?, area = ?
            WHERE id = ? AND sprint_id = ?
            """,
            (title, est, tkind, notes, dod, st, ts, sticky_hex, area, item_id, sprint_id),
        )
        return True, None

    if action == "item_update_done":
        if item_id is None:
            return False, "missing_item"
        row = conn.execute(
            "SELECT assignee, kanban_column FROM scrum_sprint_item WHERE id = ? AND sprint_id = ?",
            (item_id, sprint_id),
        ).fetchone()
        if not row or (row["assignee"] or "").strip() != proposer:
            return False, "forbidden"
        if _normalize_kanban_column(row["kanban_column"] if "kanban_column" in row.keys() else None) != "done":
            return False, "wrong_column"
        notes = (payload.get("notes") or "").strip()[:2000]
        da_json = (payload.get("done_artifacts_json") or "[]").strip()
        if not da_json.startswith("["):
            da_json = "[]"
        conn.execute(
            """
            UPDATE scrum_sprint_item SET notes = ?, done_artifacts = ?, updated_at = ?
            WHERE id = ? AND sprint_id = ?
            """,
            (notes, da_json, ts, item_id, sprint_id),
        )
        return True, None

    if action == "item_delete":
        if item_id is None:
            return False, "missing_item"
        ar = conn.execute(
            "SELECT assignee FROM scrum_sprint_item WHERE id = ? AND sprint_id = ?",
            (item_id, sprint_id),
        ).fetchone()
        if not ar or (ar["assignee"] or "").strip() != proposer:
            return False, "forbidden"
        conn.execute("DELETE FROM scrum_sprint_item WHERE id = ? AND sprint_id = ?", (item_id, sprint_id))
        return True, None

    if action == "portal_checklist":
        if item_id is None:
            return False, "missing_item"
        chk = conn.execute(
            "SELECT id FROM scrum_sprint_item WHERE id = ? AND assignee = ?",
            (item_id, proposer),
        ).fetchone()
        if not chk:
            return False, "forbidden"
        col = _normalize_kanban_column(payload.get("kanban_column"))
        st = (payload.get("item_status") or "").strip()[:32]
        dod_json = (payload.get("portal_dod_json") or "[]").strip()[:8000]
        conn.execute(
            """
            UPDATE scrum_sprint_item
            SET kanban_column = ?, status = ?, portal_dod_json = ?, updated_at = ?
            WHERE id = ? AND assignee = ?
            """,
            (col, st, dod_json, ts, item_id, proposer),
        )
        return True, None

    return False, "bad_action"


def _csrf_api_ok(app: Flask) -> bool:
    if not app.config.get("WTF_CSRF_ENABLED", True):
        return True
    try:
        token = request.form.get("csrf_token")
        if not token and request.is_json:
            token = (request.get_json(silent=True) or {}).get("csrf_token")
        if not token:
            token = request.headers.get("X-CSRFToken")
        validate_csrf(token)
        return True
    except Exception:
        return False


def _fill_empty_env_from_dotenv_file(path: Path) -> None:
    """Apply selected .env values when the process env has missing or blank entries (employee auth only).

    Does not touch keys like MANAGER_DASHBOARD_PASSWORD so tests can force an empty password with monkeypatch.
    """
    if not path.is_file():
        return
    fill_keys = frozenset(
        {
            "MICROSOFT_OAUTH_CLIENT_ID",
            "MICROSOFT_OAUTH_CLIENT_SECRET",
            "MICROSOFT_OAUTH_TENANT_ID",
            "PORTAL_OTP_SMTP_HOST",
            "PORTAL_OTP_SMTP_PORT",
            "PORTAL_OTP_SMTP_USER",
            "PORTAL_OTP_SMTP_PASSWORD",
            "PORTAL_OTP_FROM",
            "PORTAL_OTP_FROM_NAME",
            "PORTAL_OTP_USE_TLS",
            "PORTAL_OTP_TTL_MINUTES",
            "PORTAL_OTP_DEV_CONSOLE",
            "TEAM_TRACKER_PRODUCTION",
        }
    )
    for k, v in (dotenv_values(path) or {}).items():
        if not k or k not in fill_keys or v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        cur = os.environ.get(k)
        if cur is None or (isinstance(cur, str) and cur.strip() == ""):
            os.environ[k] = s


def create_app() -> Flask:
    _app_root = Path(__file__).resolve().parent
    load_dotenv(_app_root / ".env", override=False)
    load_dotenv(override=False)
    _fill_empty_env_from_dotenv_file(_app_root / ".env")

    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or "dev-only-change-with-FLASK_SECRET_KEY"
    app.config["DB_PATH"] = os.environ.get("TEAM_TRACKER_DB_PATH", str(DB_DEFAULT))
    # Default "team" when unset (local use). Set MANAGER_DASHBOARD_PASSWORD in .env for production.
    _mgr_env = os.environ.get("MANAGER_DASHBOARD_PASSWORD")
    app.config["MANAGER_DASHBOARD_PASSWORD"] = ("team" if _mgr_env is None else _mgr_env).strip()
    app.config["PRIMARY_OWNER_MANAGER_EMAIL"] = (os.environ.get("PRIMARY_OWNER_MANAGER_EMAIL") or "").strip()
    app.config["LPO_SM_DASHBOARD_PASSWORD"] = (os.environ.get("LPO_SM_DASHBOARD_PASSWORD") or "").strip()
    app.config["WTF_CSRF_ENABLED"] = os.environ.get("WTF_CSRF_ENABLED", "true").lower() in (
        "1",
        "true",
        "yes",
    )
    ms_id = (os.environ.get("MICROSOFT_OAUTH_CLIENT_ID") or "").strip()
    app.config["MICROSOFT_OAUTH_CLIENT_ID"] = ms_id
    app.config["MICROSOFT_OAUTH_CLIENT_SECRET"] = (os.environ.get("MICROSOFT_OAUTH_CLIENT_SECRET") or "").strip()
    app.config["MICROSOFT_OAUTH_TENANT_ID"] = (
        (os.environ.get("MICROSOFT_OAUTH_TENANT_ID") or "organizations").strip() or "organizations"
    )

    app.config["PORTAL_OTP_SMTP_HOST"] = (os.environ.get("PORTAL_OTP_SMTP_HOST") or "").strip()
    app.config["PORTAL_OTP_SMTP_PORT"] = int((os.environ.get("PORTAL_OTP_SMTP_PORT") or "587").strip() or "587")
    app.config["PORTAL_OTP_SMTP_USER"] = (os.environ.get("PORTAL_OTP_SMTP_USER") or "").strip()
    app.config["PORTAL_OTP_SMTP_PASSWORD"] = (os.environ.get("PORTAL_OTP_SMTP_PASSWORD") or "").strip()
    app.config["PORTAL_OTP_FROM"] = (os.environ.get("PORTAL_OTP_FROM") or "").strip()
    app.config["PORTAL_OTP_FROM_NAME"] = (os.environ.get("PORTAL_OTP_FROM_NAME") or "TEAM MANAGEMENT PORTAL").strip()
    app.config["PORTAL_OTP_USE_TLS"] = os.environ.get("PORTAL_OTP_USE_TLS", "true").lower() in (
        "1",
        "true",
        "yes",
    )
    app.config["PORTAL_OTP_TTL_MINUTES"] = int((os.environ.get("PORTAL_OTP_TTL_MINUTES") or "15").strip() or "15")
    _production = os.environ.get("TEAM_TRACKER_PRODUCTION", "").strip().lower() in ("1", "true", "yes")
    _smtp_ready_env = bool(
        (app.config.get("PORTAL_OTP_SMTP_HOST") or "").strip()
        and (app.config.get("PORTAL_OTP_FROM") or "").strip()
    )
    _raw_dev_console = (os.environ.get("PORTAL_OTP_DEV_CONSOLE") or "").strip().lower()
    if _raw_dev_console in ("0", "false", "no", "off"):
        app.config["PORTAL_OTP_DEV_CONSOLE"] = False
    elif _raw_dev_console in ("1", "true", "yes"):
        app.config["PORTAL_OTP_DEV_CONSOLE"] = True
    else:
        app.config["PORTAL_OTP_DEV_CONSOLE"] = not _production and not ms_id and not _smtp_ready_env
    if (
        app.config["PORTAL_OTP_DEV_CONSOLE"]
        and _raw_dev_console == ""
        and not _production
        and not ms_id
        and not _smtp_ready_env
    ):
        _log.warning(
            "Employee email OTP dev console is auto-enabled (no Microsoft OAuth, no SMTP, TEAM_TRACKER_PRODUCTION unset). "
            "Set TEAM_TRACKER_PRODUCTION=1 and configure MICROSOFT_OAUTH_* or PORTAL_OTP_SMTP_* before production; "
            "or set PORTAL_OTP_DEV_CONSOLE=0 to hide employee sign-in until configured."
        )

    CSRFProtect(app)

    oauth_ms = None
    if ms_id:
        from authlib.integrations.flask_client import OAuth

        oauth_ms = OAuth(app)
        tenant = app.config["MICROSOFT_OAUTH_TENANT_ID"]
        meta = f"https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration"
        oauth_ms.register(
            name="microsoft",
            client_id=ms_id,
            client_secret=app.config["MICROSOFT_OAUTH_CLIENT_SECRET"],
            server_metadata_url=meta,
            client_kwargs={"scope": "openid profile email offline_access User.Read"},
        )

    @app.route("/auth/microsoft/start")
    def portal_ms_start():
        if not oauth_ms:
            if _portal_otp_mail_configured(app):
                flash("Microsoft OAuth is not configured. Use email sign-in below, or set MICROSOFT_OAUTH_*.", "error")
            else:
                flash(
                    "Employee sign-in is not configured. Set MICROSOFT_OAUTH_CLIENT_ID and "
                    "MICROSOFT_OAUTH_CLIENT_SECRET for Microsoft, or set PORTAL_OTP_SMTP_HOST and PORTAL_OTP_FROM "
                    "for email codes. For local development, leave TEAM_TRACKER_PRODUCTION unset (dev OTP auto-enables) "
                    "or set PORTAL_OTP_DEV_CONSOLE=1. In production set TEAM_TRACKER_PRODUCTION=1 and real auth. See README.",
                    "error",
                )
            return redirect(url_for("home"))
        redirect_uri = url_for("portal_ms_callback", _external=True)
        return oauth_ms.microsoft.authorize_redirect(redirect_uri)

    @app.route("/auth/microsoft/callback")
    def portal_ms_callback():
        if not oauth_ms:
            return redirect(url_for("home"))
        try:
            token = oauth_ms.microsoft.authorize_access_token()
        except Exception:
            flash("Microsoft sign-in did not complete. Try again.", "error")
            return redirect(url_for("home"))
        access = (token or {}).get("access_token")
        if not access:
            flash("Microsoft sign-in failed (no access token).", "error")
            return redirect(url_for("home"))
        try:
            gr = requests.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {access}"},
                timeout=20,
            )
        except requests.RequestException:
            flash("Could not reach Microsoft to read your profile.", "error")
            return redirect(url_for("home"))
        if gr.status_code != 200:
            flash("Could not read your Microsoft profile.", "error")
            return redirect(url_for("home"))
        me = gr.json()
        mail = (me.get("mail") or me.get("userPrincipalName") or "").strip()
        if not mail:
            flash("No email address returned from Microsoft.", "error")
            return redirect(url_for("home"))
        low = normalize_email(mail)
        if not low.endswith("@nokia.com"):
            flash("Access restricted to Nokia corporate accounts.", "error")
            return redirect(url_for("home"))
        hit = lookup_portal_directory(low)
        if not hit:
            flash("Your account is not registered in this team. Contact your admin.", "error")
            return redirect(url_for("home"))
        session["portal_user"] = {
            "email": low,
            "name": hit["display_name"],
            "roster_name": hit["roster_name"],
            "role": "employee",
            "auth": "microsoft",
        }
        session.permanent = True
        flash(f"Welcome, {hit['display_name']}.", "success")
        return redirect(url_for("portal_dashboard"))

    @app.post("/auth/logout")
    def portal_logout():
        if not _csrf_form_ok():
            flash("Invalid security token.", "error")
            return redirect(url_for("home"))
        session.pop("portal_user", None)
        session.pop("portal_otp_email", None)
        session.pop("portal_otp_fails", None)
        flash("Signed out.", "success")
        return redirect(url_for("home"))

    @app.post("/auth/email-otp/send")
    def portal_email_otp_send():
        if not _portal_otp_mail_configured(app):
            flash("Email sign-in is not configured on this server.", "error")
            return redirect(url_for("home"))
        if not _csrf_form_ok():
            flash("Invalid security token.", "error")
            return redirect(url_for("home"))
        sess_em = session.get("portal_otp_email")
        if sess_em and isinstance(sess_em, str):
            em = normalize_email(sess_em)
        else:
            raw_email = (request.form.get("email") or "").strip()
            em = normalize_email(raw_email)
        if not em:
            flash("Enter your Nokia email address.", "error")
            return redirect(url_for("home"))
        hit = lookup_portal_directory(em)
        if not hit:
            flash(
                "If that address is registered for this team, a sign-in code will arrive shortly. "
                "Otherwise no email was sent.",
                "success",
            )
            return redirect(url_for("home"))

        now = datetime.now(timezone.utc)
        since = (now - timedelta(hours=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        ip = client_ip()
        conn = get_db(app)
        n_email = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM portal_email_otp WHERE email = ? AND created_at >= ?",
                (em, since),
            ).fetchone()["c"]
        )
        nip = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM portal_email_otp WHERE client_ip = ? AND created_at >= ?",
                (ip, since),
            ).fetchone()["c"]
        )
        if n_email >= 5:
            conn.close()
            flash("Too many code requests for this address in the last hour. Try again later.", "error")
            return redirect(url_for("home"))
        if nip >= 40:
            conn.close()
            flash("Too many code requests from this network. Try again later.", "error")
            return redirect(url_for("home"))

        code = f"{secrets.randbelow(900000) + 100000:06d}"
        ttl = max(5, min(int(app.config.get("PORTAL_OTP_TTL_MINUTES") or 15), 120))
        exp = now + timedelta(minutes=ttl)
        exp_iso = exp.isoformat(timespec="seconds").replace("+00:00", "Z")
        cre_iso = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        digest = _portal_otp_code_hmac(app, code)
        conn.execute("DELETE FROM portal_email_otp WHERE email = ?", (em,))
        conn.execute(
            """
            INSERT INTO portal_email_otp (email, code_hmac, expires_at, created_at, client_ip)
            VALUES (?, ?, ?, ?, ?)
            """,
            (em, digest, exp_iso, cre_iso, ip),
        )
        conn.commit()
        conn.close()
        if _portal_otp_smtp_ready(app):
            try:
                send_portal_otp_smtp(app, em, code)
            except Exception as exc:
                _log.warning("portal OTP email failed: %s", exc)
                conn2 = get_db(app)
                conn2.execute("DELETE FROM portal_email_otp WHERE email = ?", (em,))
                conn2.commit()
                conn2.close()
                flash("Could not send the email. Check SMTP settings or try again later.", "error")
                return redirect(url_for("home"))
        elif app.config.get("PORTAL_OTP_DEV_CONSOLE"):
            _log.warning(
                "PORTAL_OTP_DEV_CONSOLE: one-time sign-in for %s — code=%s (local dev only; never in production)",
                em,
                code,
            )
            flash(
                f"Development mode: your code is {code}. It is also in the server log. "
                "Unset PORTAL_OTP_DEV_CONSOLE for production.",
                "success",
            )
        else:
            conn2 = get_db(app)
            conn2.execute("DELETE FROM portal_email_otp WHERE email = ?", (em,))
            conn2.commit()
            conn2.close()
            flash("Email delivery is not configured.", "error")
            return redirect(url_for("home"))

        session["portal_otp_email"] = em
        session.pop("portal_otp_fails", None)
        flash("Check your inbox for a 6-digit sign-in code.", "success")
        return redirect(url_for("home"))

    @app.post("/auth/email-otp/verify")
    def portal_email_otp_verify():
        if not _portal_otp_mail_configured(app):
            flash("Email sign-in is not configured on this server.", "error")
            return redirect(url_for("home"))
        if not _csrf_form_ok():
            flash("Invalid security token.", "error")
            return redirect(url_for("home"))
        em = session.get("portal_otp_email")
        if not em or not isinstance(em, str):
            flash("Request a sign-in code first.", "error")
            return redirect(url_for("home"))
        code = (request.form.get("code") or "").replace(" ", "").strip()
        if len(code) != 6 or not code.isdigit():
            flash("Enter the 6-digit code from your email.", "error")
            return redirect(url_for("home"))

        conn = get_db(app)
        row = conn.execute(
            "SELECT id, code_hmac, expires_at FROM portal_email_otp WHERE email = ? ORDER BY id DESC LIMIT 1",
            (em,),
        ).fetchone()
        if not row:
            conn.close()
            flash("No active code. Request a new one.", "error")
            return redirect(url_for("home"))

        exp_raw = str(row["expires_at"] or "")
        try:
            exp_dt = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
        except ValueError:
            conn.execute("DELETE FROM portal_email_otp WHERE id = ?", (int(row["id"]),))
            conn.commit()
            conn.close()
            flash("Invalid challenge state. Request a new code.", "error")
            return redirect(url_for("home"))

        if datetime.now(timezone.utc) > exp_dt:
            conn.execute("DELETE FROM portal_email_otp WHERE id = ?", (int(row["id"]),))
            conn.commit()
            conn.close()
            flash("That code has expired. Request a new one.", "error")
            return redirect(url_for("home"))

        digest = _portal_otp_code_hmac(app, code)
        if not hmac.compare_digest(digest, str(row["code_hmac"] or "")):
            fails = int(session.get("portal_otp_fails") or 0) + 1
            session["portal_otp_fails"] = fails
            conn.close()
            if fails >= 8:
                conn2 = get_db(app)
                conn2.execute("DELETE FROM portal_email_otp WHERE email = ?", (em,))
                conn2.commit()
                conn2.close()
                session.pop("portal_otp_fails", None)
                flash("Too many wrong attempts. Request a new code.", "error")
            else:
                flash("Incorrect code. Try again.", "error")
            return redirect(url_for("home"))

        hit = lookup_portal_directory(em)
        if not hit:
            conn.execute("DELETE FROM portal_email_otp WHERE id = ?", (int(row["id"]),))
            conn.commit()
            conn.close()
            session.pop("portal_otp_email", None)
            flash("Your email is no longer in the team directory.", "error")
            return redirect(url_for("home"))

        conn.execute("DELETE FROM portal_email_otp WHERE id = ?", (int(row["id"]),))
        conn.commit()
        conn.close()
        session.pop("portal_otp_email", None)
        session.pop("portal_otp_fails", None)
        session["portal_user"] = {
            "email": hit["email"],
            "name": hit["display_name"],
            "roster_name": hit["roster_name"],
            "role": "employee",
            "auth": "email_otp",
        }
        session.permanent = True
        flash(f"Welcome, {hit['display_name']}.", "success")
        return redirect(url_for("portal_dashboard"))

    @app.route("/auth/email-otp/cancel")
    def portal_email_otp_cancel():
        session.pop("portal_otp_email", None)
        session.pop("portal_otp_fails", None)
        flash("Cancelled email sign-in.", "info")
        return redirect(url_for("login_page"))

    @app.route("/portal")
    def portal_dashboard():
        pu = _portal_session()
        if not pu:
            return redirect(url_for("home"))
        return render_template("portal_dashboard.html", hide_nav=True)

    @app.route("/portal/leave/apply", methods=["GET", "POST"])
    def portal_leave_apply():
        pu = _portal_session()
        if not pu:
            return redirect(url_for("home"))
        today_iso = date.today().isoformat()
        if request.method == "POST":
            if not _csrf_form_ok():
                flash("Invalid security token.", "error")
                return redirect(url_for("portal_leave_apply"))
            kind = (request.form.get("leave_kind") or "").strip()
            allowed = {c for c, _ in PORTAL_LEAVE_FORM_CHOICES}
            if kind not in allowed:
                flash("Select a valid leave type.", "error")
                return render_template(
                    "portal_leave_apply.html",
                    hide_nav=True,
                    leave_choices=PORTAL_LEAVE_FORM_CHOICES,
                    form=request.form,
                    default_start=today_iso,
                    default_end=today_iso,
                )
            start_date = (request.form.get("start_date") or "").strip()
            end_date = (request.form.get("end_date") or "").strip()
            reason = (request.form.get("reason") or "").strip()[:300]
            errors: list[str] = []
            if not start_date:
                errors.append("Start date is required.")
            if not end_date:
                errors.append("End date is required.")
            if not reason:
                errors.append("Reason is required.")
            sd: date | None = None
            ed: date | None = None
            if not errors:
                try:
                    sd = date.fromisoformat(start_date[:10])
                    ed = date.fromisoformat(end_date[:10])
                except ValueError:
                    errors.append("Invalid date format.")
            if not errors and sd and ed:
                if ed < sd:
                    errors.append("End date cannot be before start date.")
                elif sd.weekday() >= 5 or ed.weekday() >= 5:
                    errors.append("Leave cannot start or end on a weekend — choose Monday through Friday.")
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template(
                    "portal_leave_apply.html",
                    hide_nav=True,
                    leave_choices=PORTAL_LEAVE_FORM_CHOICES,
                    form=request.form,
                    default_start=today_iso,
                    default_end=today_iso,
                )
            assert sd is not None and ed is not None
            duration_type = "multi" if ed > sd else "full"
            label = dict(PORTAL_LEAVE_FORM_CHOICES).get(kind, kind)
            created = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            ip = client_ip()
            conn = get_db(app)
            conn.execute(
                """
                INSERT INTO leave_requests
                (employee_name, reason, description, start_date, end_date, duration_type, status,
                 created_at, submitted_ip, submitted_email, portal_leave_label)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    pu["roster_name"],
                    kind,
                    reason,
                    start_date[:10],
                    end_date[:10],
                    duration_type,
                    created,
                    ip,
                    pu["email"],
                    label,
                ),
            )
            conn.commit()
            conn.close()
            wd = _working_weekdays_count(sd, ed)
            if any(d.weekday() >= 5 for d in _daterange_inclusive(sd, ed)):
                flash(
                    f"Leave request submitted successfully. Your range includes weekend days; "
                    f"working weekdays in this span: {wd}.",
                    "success",
                )
            else:
                flash("Leave request submitted successfully.", "success")
            return redirect(url_for("portal_dashboard"))

        return render_template(
            "portal_leave_apply.html",
            hide_nav=True,
            leave_choices=PORTAL_LEAVE_FORM_CHOICES,
            form={},
            default_start=today_iso,
            default_end=today_iso,
        )

    @app.route("/portal/leave/history")
    def portal_leave_history():
        pu = _portal_session()
        if not pu:
            return redirect(url_for("home"))
        conn = get_db(app)
        rows = list(
            conn.execute(
                """
                SELECT * FROM leave_requests
                WHERE employee_name = ?
                ORDER BY created_at DESC
                LIMIT 200
                """,
                (pu["roster_name"],),
            )
        )
        conn.close()
        base_reasons = dict(LEAVE_REASONS)
        display_rows: list[dict] = []
        for r in rows:
            try:
                sd = date.fromisoformat(str(r["start_date"])[:10])
                ed = date.fromisoformat(str(r["end_date"])[:10])
                wd = _working_weekdays_count(sd, ed)
            except ValueError:
                wd = 0
            plab = (r["portal_leave_label"] if "portal_leave_label" in r.keys() else "") or ""
            leave_type = plab.strip() if plab.strip() else base_reasons.get(r["reason"], r["reason"])
            display_rows.append({"raw": r, "working_days": wd, "leave_type": leave_type})
        return render_template(
            "portal_leave_history.html",
            hide_nav=True,
            display_rows=display_rows,
        )

    @app.route("/portal/my-sprint/board", methods=["GET"])
    def portal_my_sprint_kanban():
        """Employee entry point: open the sticky-note Kanban for their latest sprint with items."""
        pu = _portal_session()
        if not pu:
            return redirect(url_for("home"))
        conn = get_db(app)
        row = conn.execute(
            """
            SELECT i.sprint_id
            FROM scrum_sprint_item i
            JOIN scrum_sprint s ON s.id = i.sprint_id
            WHERE i.assignee = ?
            ORDER BY s.start_date DESC, i.id DESC
            LIMIT 1
            """,
            (pu["roster_name"],),
        ).fetchone()
        conn.close()
        if not row:
            flash("No sprint items are assigned to you yet.", "info")
            return redirect(url_for("portal_dashboard"))
        return redirect(url_for("portal_scrum_kanban_board", sprint_id=int(row["sprint_id"])))

    @app.route("/portal/sprint", methods=["GET", "POST"])
    def portal_sprint():
        pu = _portal_session()
        if not pu:
            return redirect(url_for("home"))
        if request.method == "GET":
            return redirect(url_for("portal_my_sprint_kanban"))
        if not _csrf_form_ok():
            flash("Invalid security token.", "error")
            return redirect(url_for("portal_my_sprint_kanban"))
        item_id = _parse_optional_int(request.form.get("item_id"))
        if not item_id:
            flash("Missing item.", "error")
            return redirect(url_for("portal_my_sprint_kanban"))
        status = (request.form.get("portal_status") or "").strip().lower()
        if status not in ("todo", "progress", "done"):
            flash("Invalid status.", "error")
            return redirect(url_for("portal_my_sprint_kanban"))
        dod_flags: list[bool] = []
        for i in range(len(PORTAL_DOD_CHECKLIST_LABELS)):
            dod_flags.append((request.form.get(f"dod_{i}") or "").strip() == "1")
        dod_json = json.dumps(dod_flags)
        col = _portal_status_to_kanban(status)
        st = _status_for_kanban_column(col)
        conn = get_db(app)
        row = conn.execute(
            """
            SELECT i.id, i.sprint_id, s.team_id
            FROM scrum_sprint_item i
            JOIN scrum_sprint s ON s.id = i.sprint_id
            WHERE i.id = ? AND i.assignee = ?
            """,
            (int(item_id), pu["roster_name"]),
        ).fetchone()
        if not row:
            conn.close()
            flash("That work item was not found or is not assigned to you.", "error")
            return redirect(url_for("portal_my_sprint_kanban"))
        pl = {"kanban_column": col, "item_status": st, "portal_dod_json": dod_json}
        _insert_scrum_portal_proposal(
            conn,
            int(row["team_id"]),
            int(row["sprint_id"]),
            int(item_id),
            "portal_checklist",
            pu["roster_name"],
            pl,
        )
        conn.commit()
        sid = int(row["sprint_id"])
        conn.close()
        flash("Change submitted for manager approval.", "success")
        return redirect(url_for("portal_scrum_kanban_board", sprint_id=sid))

    @app.route("/portal/sprint/<int:sprint_id>/board", methods=["GET"])
    def portal_scrum_kanban_board(sprint_id: int):
        pu = _portal_session()
        if not pu:
            return redirect(url_for("home"))
        roster_name = (pu.get("roster_name") or "").strip()
        if not roster_name:
            flash("Your profile is missing a roster name.", "error")
            return redirect(url_for("portal_dashboard"))
        conn = get_db(app)
        team_id = _sprint_team_id(conn, sprint_id)
        if team_id is None:
            conn.close()
            flash("Sprint not found.", "error")
            return redirect(url_for("portal_my_sprint_kanban"))
        sprint = conn.execute(
            "SELECT * FROM scrum_sprint WHERE id = ? AND team_id = ?", (int(sprint_id), team_id)
        ).fetchone()
        if not sprint:
            conn.close()
            flash("Sprint not found.", "error")
            return redirect(url_for("portal_my_sprint_kanban"))
        cards_by_col = _load_kanban_cards(conn, int(sprint_id), roster_name, team_id)
        task_kinds_rows = _list_team_task_kinds(conn, team_id)
        conn.close()
        kb_leave_ctx = build_kanban_leave_worksheet_context(
            app,
            int(sprint_id),
            str(sprint["start_date"])[:10],
            str(sprint["end_date"])[:10],
            roster_name,
        )
        return render_template(
            "scrum_kanban.html",
            hide_nav=True,
            sprint_id=int(sprint_id),
            sprint_name=sprint["name"],
            assignee=roster_name,
            columns=SCRUM_KANBAN_COLUMNS,
            cards_by_col=cards_by_col,
            task_kinds_rows=task_kinds_rows,
            kb_back_url=url_for("portal_dashboard"),
            kb_back_label="Dashboard",
            kb_api_urls={
                "item_move": url_for("portal_scrum_api_item_move"),
                "item_note": url_for("portal_scrum_api_item_note"),
                "item_activity_update": url_for("portal_scrum_api_activity_update"),
                "item_add": url_for("portal_scrum_api_item_add"),
            },
            kb_form_urls={
                "item_update": url_for("portal_scrum_sprint_item_update"),
                "item_delete": url_for("portal_scrum_sprint_item_delete"),
            },
            portal_kanban=True,
            sprint_readonly=_sprint_row_is_closed(sprint),
            **kb_leave_ctx,
        )

    @app.post("/portal/scrum/api/item/move")
    def portal_scrum_api_item_move():
        pu = _portal_session()
        if not pu:
            return jsonify({"ok": False, "error": "auth"}), 403
        if not _csrf_api_ok(app):
            return jsonify({"ok": False, "error": "csrf"}), 400
        roster_name = (pu.get("roster_name") or "").strip()
        data = request.get_json(silent=True) or {}
        item_id = _parse_optional_int(data.get("item_id"))
        sprint_id = _parse_optional_int(data.get("sprint_id"))
        to_col = _normalize_kanban_column(data.get("to_column"))
        note_raw = (data.get("note") or "").strip()[:2000]
        if not item_id or not sprint_id or not roster_name:
            return jsonify({"ok": False, "error": "missing"}), 400
        conn = get_db(app)
        team_id = _sprint_team_id(conn, sprint_id)
        if team_id is None or not _item_belongs_to_sprint_team(conn, item_id, sprint_id, team_id, roster_name):
            conn.close()
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if _sprint_is_closed_by_id(conn, sprint_id):
            conn.close()
            return jsonify({"ok": False, "error": "sprint_closed"}), 403
        row = conn.execute(
            "SELECT kanban_column FROM scrum_sprint_item WHERE id = ?", (item_id,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"ok": False, "error": "missing"}), 400
        from_col = _normalize_kanban_column(row["kanban_column"])
        ch = _parse_committed_hours_for_kanban_move(
            data.get("committed_hours"), from_col=from_col, to_col=to_col
        )
        artifacts_json: str | None = None
        if to_col == "done":
            artifacts_json, aerr = _normalize_done_artifacts_from_api(data.get("artifacts"))
            if aerr:
                conn.close()
                return jsonify({"ok": False, "error": aerr}), 400
        pl: dict = {"to_column": to_col, "committed_hours": ch, "note": note_raw}
        if to_col == "done" and artifacts_json is not None:
            pl["done_artifacts_json"] = artifacts_json
        vurls = _normalize_verification_urls_mixed(data.get("verification_urls"))
        if vurls:
            pl["verification_urls"] = vurls
        _insert_scrum_portal_proposal(conn, team_id, sprint_id, item_id, "item_move", roster_name, pl)
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "pending_approval": False})

    @app.post("/portal/scrum/api/item/note")
    def portal_scrum_api_item_note():
        pu = _portal_session()
        if not pu:
            return jsonify({"ok": False, "error": "auth"}), 403
        if not _csrf_api_ok(app):
            return jsonify({"ok": False, "error": "csrf"}), 400
        roster_name = (pu.get("roster_name") or "").strip()
        data = request.get_json(silent=True) or {}
        item_id = _parse_optional_int(data.get("item_id"))
        sprint_id = _parse_optional_int(data.get("sprint_id"))
        note_raw = (data.get("note") or "").strip()[:2000]
        ch = _parse_hours_field(str(data.get("committed_hours")))
        if not item_id or not sprint_id or not roster_name:
            return jsonify({"ok": False, "error": "missing"}), 400
        if not note_raw and ch < 0:
            return jsonify({"ok": False, "error": "missing"}), 400
        conn = get_db(app)
        team_id = _sprint_team_id(conn, sprint_id)
        if team_id is None or not _item_belongs_to_sprint_team(conn, item_id, sprint_id, team_id, roster_name):
            conn.close()
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if _sprint_is_closed_by_id(conn, sprint_id):
            conn.close()
            return jsonify({"ok": False, "error": "sprint_closed"}), 403
        row = conn.execute(
            "SELECT kanban_column FROM scrum_sprint_item WHERE id = ?", (item_id,)
        ).fetchone()
        if not row or _normalize_kanban_column(row["kanban_column"]) != "doing":
            conn.close()
            return jsonify({"ok": False, "error": "only_doing"}), 400
        pl = {"note": note_raw, "committed_hours": ch}
        vurls = _normalize_verification_urls_mixed(data.get("verification_urls"))
        if vurls:
            pl["verification_urls"] = vurls
        _insert_scrum_portal_proposal(conn, team_id, sprint_id, item_id, "item_note", roster_name, pl)
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "pending_approval": False})

    @app.post("/portal/scrum/api/item/activity_update")
    def portal_scrum_api_activity_update():
        pu = _portal_session()
        if not pu:
            return jsonify({"ok": False, "error": "auth"}), 403
        if not _csrf_api_ok(app):
            return jsonify({"ok": False, "error": "csrf"}), 400
        roster_name = (pu.get("roster_name") or "").strip()
        data = request.get_json(silent=True) or {}
        item_id = _parse_optional_int(data.get("item_id"))
        sprint_id = _parse_optional_int(data.get("sprint_id"))
        activity_id = _parse_optional_int(data.get("activity_id"))
        note_raw = (data.get("note") or "").strip()[:2000]
        ch = _parse_hours_field(str(data.get("committed_hours")))
        if not item_id or not sprint_id or not roster_name or not activity_id:
            return jsonify({"ok": False, "error": "missing"}), 400
        conn = get_db(app)
        team_id = _sprint_team_id(conn, sprint_id)
        if team_id is None or not _item_belongs_to_sprint_team(conn, item_id, sprint_id, team_id, roster_name):
            conn.close()
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if _sprint_is_closed_by_id(conn, sprint_id):
            conn.close()
            return jsonify({"ok": False, "error": "sprint_closed"}), 403
        
        pl = {"activity_id": activity_id, "note": note_raw, "committed_hours": ch}
        _insert_scrum_portal_proposal(conn, team_id, sprint_id, item_id, "item_activity_update", roster_name, pl)
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "pending_approval": False})

    @app.post("/portal/scrum/api/item/add")
    def portal_scrum_api_item_add():
        pu = _portal_session()
        if not pu:
            return jsonify({"ok": False, "error": "auth"}), 403
        if not _csrf_api_ok(app):
            return jsonify({"ok": False, "error": "csrf"}), 400
        roster_name = (pu.get("roster_name") or "").strip()
        data = request.get_json(silent=True) or {}
        sprint_id = _parse_optional_int(data.get("sprint_id"))
        title = (data.get("title") or "").strip()[:500]
        notes = (data.get("notes") or "").strip()[:2000]
        dod = (data.get("dod") or "").strip()[:4000]
        est = _parse_hours_field(data.get("estimate_hours"))
        if not sprint_id or not title or not roster_name:
            return jsonify({"ok": False, "error": "missing"}), 400
        if est < 0:
            return jsonify({"ok": False, "error": "estimate_required"}), 400
        new_l = (data.get("new_kind_label") or "").strip()
        if new_l:
            return jsonify({"ok": False, "error": "no_new_kind"}), 400
        conn = get_db(app)
        team_id = _sprint_team_id(conn, sprint_id)
        if team_id is None or not _sprint_row_for_team(conn, sprint_id, team_id):
            conn.close()
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if _sprint_is_closed_by_id(conn, sprint_id):
            conn.close()
            return jsonify({"ok": False, "error": "sprint_closed"}), 403
        _ensure_team_task_kinds(conn, team_id)
        specified = "task_kind_code" in data
        v = data.get("task_kind_code") if specified else "ndy"
        raw = str(v or "ndy").strip().lower()
        if raw in SCRUM_LEGACY_TASK_KIND_MAP:
            raw = SCRUM_LEGACY_TASK_KIND_MAP[raw]
        tcode = raw if raw in SCRUM_TASK_KIND_CODES else "ndy"
        pl: dict = {
            "title": title,
            "notes": notes,
            "dod": dod,
            "estimate_hours": est,
            "task_kind_code": tcode,
            "area": (data.get("area") or "").strip()[:SCRUM_STICKY_AREA_MAX_LEN],
        }
        _insert_scrum_portal_proposal(conn, team_id, sprint_id, None, "item_add", roster_name, pl)
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "pending_approval": False})

    @app.post("/portal/scrum/sprint/item/update")
    def portal_scrum_sprint_item_update():
        pu = _portal_session()
        if not pu:
            flash("Sign in required.", "error")
            return redirect(url_for("home"))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("portal_my_sprint_kanban"))
        roster_name = (pu.get("roster_name") or "").strip()
        item_id = _parse_optional_int(request.form.get("item_id"))
        sprint_id = _parse_optional_int(request.form.get("sprint_id"))
        if not item_id or not sprint_id or not roster_name:
            flash("Missing backlog item.", "error")
            return redirect(url_for("portal_my_sprint_kanban"))
        conn = get_db(app)
        team_id = _sprint_team_id(conn, sprint_id)
        if team_id is None or not _sprint_row_for_team(conn, sprint_id, team_id):
            conn.close()
            flash("Invalid sprint.", "error")
            return redirect(url_for("portal_my_sprint_kanban"))
        if _sprint_is_closed_by_id(conn, sprint_id):
            conn.close()
            flash(SCRUM_SPRINT_READONLY_FLASH, "error")
            return redirect(url_for("portal_scrum_kanban_board", sprint_id=sprint_id))
        row = conn.execute(
            "SELECT assignee, kanban_column, sticky_color_hex FROM scrum_sprint_item WHERE id = ? AND sprint_id = ?",
            (item_id, sprint_id),
        ).fetchone()
        if not row or (row["assignee"] or "").strip() != roster_name:
            conn.close()
            flash("Item not found.", "error")
            return redirect(url_for("portal_scrum_kanban_board", sprint_id=sprint_id))
        cur_col = _normalize_kanban_column(row["kanban_column"] if "kanban_column" in row.keys() else None)

        if cur_col == "done":
            notes = (request.form.get("notes") or "").strip()[:2000]
            da_lines = request.form.get("done_artifacts_lines") or ""
            da_json, da_err = _normalize_done_artifacts_from_lines(da_lines)
            if da_err:
                conn.close()
                if da_err == "artifacts_limit":
                    msg = "Too many artifact links (maximum 20)."
                else:
                    msg = "Artifact links must be valid http(s) URLs (one per line, optional label before |)."
                flash(msg, "error")
                return redirect(url_for("portal_scrum_kanban_board", sprint_id=sprint_id))
            pl = {"notes": notes, "done_artifacts_json": da_json}
            vurls = _normalize_verification_urls_mixed(request.form.get("verification_urls_lines") or "")
            if vurls:
                pl["verification_urls"] = vurls
            _insert_scrum_portal_proposal(conn, team_id, sprint_id, item_id, "item_update_done", roster_name, pl)
            conn.commit()
            conn.close()
            flash("Changes saved.", "success")
            return redirect(url_for("portal_scrum_kanban_board", sprint_id=sprint_id))

        if cur_col != "do":
            conn.close()
            flash("Planning edits are only in Do; details and artifacts can be edited in Done.", "error")
            return redirect(url_for("portal_scrum_kanban_board", sprint_id=sprint_id))
        title = (request.form.get("title") or "").strip()
        if not title:
            conn.close()
            flash("Title is required.", "error")
            return redirect(url_for("portal_scrum_kanban_board", sprint_id=sprint_id))
        est = _parse_hours_field(request.form.get("estimate_hours"))
        if est < 0:
            conn.close()
            flash("Estimate cannot be negative.", "error")
            return redirect(url_for("portal_scrum_kanban_board", sprint_id=sprint_id))
        tkind = _coerce_sprint_item_task_kind(conn, team_id, request.form.get("task_kind"))
        notes = (request.form.get("notes") or "").strip()[:2000]
        dod = (request.form.get("dod") or "").strip()[:4000]
        pl: dict = {
            "title": title,
            "estimate_hours": est,
            "task_kind": tkind,
            "notes": notes,
            "dod": dod,
            "area": (request.form.get("area") or "").strip()[:SCRUM_STICKY_AREA_MAX_LEN],
        }
        _insert_scrum_portal_proposal(conn, team_id, sprint_id, item_id, "item_update_do", roster_name, pl)
        conn.commit()
        conn.close()
        flash("Changes saved.", "success")
        return redirect(url_for("portal_scrum_kanban_board", sprint_id=sprint_id))

    @app.post("/portal/scrum/sprint/item/delete")
    def portal_scrum_sprint_item_delete():
        pu = _portal_session()
        if not pu:
            flash("Sign in required.", "error")
            return redirect(url_for("home"))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("portal_my_sprint_kanban"))
        roster_name = (pu.get("roster_name") or "").strip()
        item_id = _parse_optional_int(request.form.get("item_id"))
        sprint_id = _parse_optional_int(request.form.get("sprint_id"))
        if not item_id or not sprint_id or not roster_name:
            flash("Missing item.", "error")
            return redirect(url_for("portal_my_sprint_kanban"))
        conn = get_db(app)
        team_id = _sprint_team_id(conn, sprint_id)
        if team_id is None or not _sprint_row_for_team(conn, sprint_id, team_id):
            conn.close()
            flash("Invalid sprint.", "error")
            return redirect(url_for("portal_my_sprint_kanban"))
        if _sprint_is_closed_by_id(conn, sprint_id):
            conn.close()
            flash(SCRUM_SPRINT_READONLY_FLASH, "error")
            return redirect(url_for("portal_scrum_kanban_board", sprint_id=sprint_id))
        ar = conn.execute(
            "SELECT assignee FROM scrum_sprint_item WHERE id = ? AND sprint_id = ?",
            (item_id, sprint_id),
        ).fetchone()
        if not ar or (ar["assignee"] or "").strip() != roster_name:
            conn.close()
            flash("Item not found.", "error")
            return redirect(url_for("portal_scrum_kanban_board", sprint_id=sprint_id))
        _insert_scrum_portal_proposal(conn, team_id, sprint_id, item_id, "item_delete", roster_name, {})
        conn.commit()
        conn.close()
        flash("Changes saved.", "success")
        return redirect(url_for("portal_scrum_kanban_board", sprint_id=sprint_id))

    @app.before_request
    def _manager_team_roster_g() -> None:
        g.manager_teams = []
        g.manager_team_id = None
        g.manager_team_name = None
        g.manager_roster = EMPLOYEES
        g.team_hub_mode = "leave"
        if not _manager_logged_in():
            return
        conn = get_db(app)
        manager_email = session.get("manager_user_email")
        if manager_email:
            teams = list(conn.execute(
                "SELECT id, name, hub_mode FROM teams WHERE owner_email = ? OR owner_email = '' ORDER BY name COLLATE NOCASE",
                (manager_email,)
            ))
        else:
            teams = list(conn.execute("SELECT id, name, hub_mode FROM teams ORDER BY name COLLATE NOCASE"))
        if not teams:
            conn.close()
            return
        ids = [int(t["id"]) for t in teams]
        tid_raw = session.get("active_team_id")
        try:
            tid = int(tid_raw) if tid_raw is not None else ids[0]
        except (TypeError, ValueError):
            tid = ids[0]
        if tid not in ids:
            tid = ids[0]
        session["active_team_id"] = tid
        trow = next((t for t in teams if int(t["id"]) == tid), teams[0])
        g.manager_team_id = int(trow["id"])
        g.manager_team_name = str(trow["name"])
        g.team_hub_mode = _normalize_hub_mode(str(trow["hub_mode"] or "leave"))
        g.manager_teams = [
            {"id": int(t["id"]), "name": t["name"], "hub_mode": _normalize_hub_mode(str(t["hub_mode"] or "leave"))}
            for t in teams
        ]
        tname = str(trow["name"] or "").strip()
        if tname.casefold() == "default":
            urows = conn.execute(
                "SELECT DISTINCT employee_name FROM team_roster ORDER BY employee_name COLLATE NOCASE"
            ).fetchall()
        else:
            urows = conn.execute(
                """
                SELECT employee_name FROM team_roster
                WHERE team_id = ?
                ORDER BY sort_order, employee_name COLLATE NOCASE
                """,
                (g.manager_team_id,),
            ).fetchall()
        conn.close()
        if urows:
            g.manager_roster = tuple(r[0] for r in urows)
        elif tname.casefold() == "default":
            g.manager_roster = EMPLOYEES
        else:
            g.manager_roster = ()

    def _csrf_form_ok() -> bool:
        if not app.config.get("WTF_CSRF_ENABLED", True):
            return True
        try:
            validate_csrf(request.form.get("csrf_token"))
            return True
        except Exception:
            return False

    @app.context_processor
    def _inject_nav() -> dict:
        manager_email = session.get("manager_user_email")
        if manager_email:
            manager_name = manager_email.split("@")[0].replace(".", " ").title()
        else:
            manager_name = "Manager"

        return {
            "manager_logged_in": _manager_logged_in(),
            "manager_password_configured": _manager_password_configured(app),
            "lpo_sm_password_configured": _lpo_sm_password_configured(app),
            "manager_nav_label": (
                "LPO/SM" if (session.get("manager_role") or "").strip() == "lpo_sm" else manager_name
            ),
            "manager_teams": getattr(g, "manager_teams", []),
            "active_team_id": getattr(g, "manager_team_id", None),
            "active_team_name": getattr(g, "manager_team_name", None),
            "team_hub_mode": getattr(g, "team_hub_mode", "leave"),
            "portal_user": _portal_session(),
            "microsoft_oauth_configured": bool(app.config.get("MICROSOFT_OAUTH_CLIENT_ID")),
            "email_otp_configured": _portal_otp_mail_configured(app),
        }

    @app.errorhandler(404)
    def not_found(_e):  # noqa: ANN001
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(_e):  # noqa: ANN001
        return render_template("errors/500.html"), 500

    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}, 200

    @app.route("/api/employees")
    def api_employees():
        q = (request.args.get("q") or "").strip()
        union = get_union_roster_for_app(app)
        matches = fuzzy_employee_matches(q, 5, roster=union)
        return jsonify({"matches": matches})

    @app.route("/manager/login", methods=["GET", "POST"])
    def manager_login():
        """Legacy URL — forwards to /dashboard (307 keeps POST body for old bookmarks)."""
        if request.method == "POST":
            return redirect(url_for("dashboard"), code=307)
        return redirect(url_for("dashboard", **request.args))

    @app.route("/manager")
    def manager_dashboard():
        """Legacy URL — redirect to unified dashboard."""
        return redirect(url_for("dashboard", **request.args))

    @app.route("/manager/logout", methods=["POST"])
    def manager_logout():
        session.pop("manager", None)
        session.pop("manager_role", None)
        session.pop("active_team_id", None)
        session.pop("manager_user_email", None)
        flash("Signed out.", "success")
        return redirect(url_for("login_page"))

    @app.route("/dashboard", methods=["GET", "POST"])
    def dashboard():
        """Secret code on /dashboard when not signed in; full leave tracker after success."""
        pw_configured = _manager_password_configured(app)
        lpo_configured = _lpo_sm_password_configured(app)
        any_gate = pw_configured or lpo_configured

        def _gate_render(next_url_val: str | None, **extra):
            return render_template(
                "dashboard.html",
                gate_only=True,
                next_url=next_url_val,
                password_configured=pw_configured,
                lpo_sm_password_configured=lpo_configured,
                **extra,
            )

        if request.method == "POST" and _manager_logged_in():
            return redirect(url_for("dashboard", **request.args))

        if request.method == "POST" and not _manager_logged_in():
            if not any_gate:
                flash("Manager and LPO/SM access are not configured.", "error")
                return _gate_render((request.form.get("next") or request.args.get("next")))
            gate = (request.form.get("gate_kind") or "manager").strip().lower()
            next_raw = (request.form.get("next") or request.args.get("next") or "").strip()
            try:
                ry = int(request.form.get("return_year") or 0)
                rm = int(request.form.get("return_month") or 0)
            except ValueError:
                ry, rm = 0, 0

            def _finish_signin(role: str):
                session["manager"] = True
                session["manager_role"] = role
                session.pop("my_employee_name", None)
                flash("Signed in.", "success")
                dest = _safe_next_path(next_raw)
                if dest:
                    return redirect(dest, code=303)
                if 1 <= rm <= 12 and ry > 2000:
                    return redirect(url_for("dashboard", year=ry, month=rm), code=303)
                return redirect(url_for("dashboard"), code=303)

            if gate == "lpo_sm":
                if not lpo_configured:
                    flash("LPO/SM access is not configured.", "error")
                    return _gate_render(next_raw or None)
                pw = (request.form.get("secret_code") or request.form.get("lpo_sm_code") or "").strip()
                if not pw:
                    flash("Code required.", "error")
                    return _gate_render(next_raw or None)
                if not _check_lpo_sm_password(app, pw):
                    flash("Invalid code.", "error")
                    return _gate_render(next_raw or None)
                return _finish_signin("lpo_sm")

            if not pw_configured:
                flash("Manager access is not configured.", "error")
                return _gate_render(next_raw or None)
            pw = (request.form.get("secret_code") or request.form.get("manager_password") or "").strip()
            if not pw:
                flash("Code required.", "error")
                return _gate_render(next_raw or None)
            if not _check_manager_password(app, pw):
                flash("Invalid code.", "error")
                return _gate_render(next_raw or None)
            return _finish_signin("manager")

        if not _manager_logged_in():
            return _gate_render(request.args.get("next"))

        today = date.today()
        try:
            year = int(request.args.get("year") or today.year)
            month = int(request.args.get("month") or today.month)
        except ValueError:
            year, month = today.year, today.month
        month = max(1, min(12, month))

        roster: tuple[str, ...] = g.manager_roster
        ctx = build_month_context(app, year, month, roster=roster)
        prev_month = date(year, month, 1) - timedelta(days=1)
        next_month = date(year, month, 28) + timedelta(days=4)
        next_month = next_month.replace(day=1)

        conn = get_db(app)
        ph = ",".join("?" * len(roster))
        pending = conn.execute(
            f"""
            SELECT * FROM leave_requests
            WHERE status = 'pending' AND employee_name IN ({ph})
            ORDER BY created_at DESC
            LIMIT 50
            """,
            roster,
        ).fetchall()
        all_leaves = conn.execute(
            f"""
            SELECT * FROM leave_requests
            WHERE employee_name IN ({ph})
            ORDER BY start_date DESC, employee_name ASC
            LIMIT 300
            """,
            roster,
        ).fetchall()
        dashboard_sprint_summaries = _dashboard_sprint_summaries(conn, int(g.manager_team_id))
        conn.close()

        reason_labels = dict(LEAVE_REASONS)
        duration_labels = dict(DURATION_CHOICES)

        return render_template(
            "dashboard.html",
            gate_only=False,
            pending=pending,
            all_leaves=all_leaves,
            employees=roster,
            reason_labels=reason_labels,
            duration_labels=duration_labels,
            prev_y=prev_month.year,
            prev_m=prev_month.month,
            next_y=next_month.year,
            next_m=next_month.month,
            password_configured=pw_configured,
            dashboard_sprint_summaries=dashboard_sprint_summaries,
            today_iso=date.today().isoformat(),
            **ctx,
        )

    @app.route("/manager/audit.csv")
    def manager_audit_csv():
        if not _manager_logged_in():
            return redirect(url_for("dashboard", next=request.path))
        conn = get_db(app)
        rows = conn.execute(
            """
            SELECT id, employee_name, reason, start_date, end_date, duration_type,
                   status, created_at, submitted_ip
            FROM leave_requests
            ORDER BY created_at DESC
            """
        ).fetchall()
        conn.close()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            [
                "id",
                "employee_name",
                "reason",
                "start_date",
                "end_date",
                "duration_type",
                "status",
                "created_at",
                "submitted_ip",
            ]
        )
        for row in rows:
            w.writerow(
                [
                    row["id"],
                    row["employee_name"],
                    row["reason"],
                    row["start_date"],
                    row["end_date"],
                    row["duration_type"],
                    row["status"],
                    row["created_at"],
                    row["submitted_ip"] if "submitted_ip" in row.keys() else "",
                ]
            )
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=leave_audit_log.csv"},
        )

    @app.route("/dashboard/leave/<int:leave_id>/edit", methods=["GET", "POST"])
    def dashboard_leave_edit(leave_id: int):
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))

        conn = get_db(app)
        row = conn.execute("SELECT * FROM leave_requests WHERE id = ?", (leave_id,)).fetchone()
        conn.close()
        if not row:
            flash("Not found.", "error")
            return redirect(url_for("dashboard"))

        reason_labels = dict(LEAVE_REASONS)
        duration_labels = dict(DURATION_CHOICES)

        if request.method == "POST":
            employee_name = (request.form.get("employee_name") or "").strip()
            reason = (request.form.get("reason") or "").strip()
            description = ""
            start_date = (request.form.get("start_date") or "").strip()
            end_date = (request.form.get("end_date") or "").strip()
            duration_type = (request.form.get("duration_type") or "").strip()
            status = (request.form.get("status") or "").strip()

            errors: list[str] = []
            if employee_name not in g.manager_roster:
                errors.append("Employee must be one of the roster names.")
            if reason not in {c[0] for c in LEAVE_REASONS}:
                errors.append("Select a valid leave reason.")
            if not start_date or not end_date:
                errors.append("Start and end dates are required.")
            if duration_type not in {c[0] for c in DURATION_CHOICES}:
                errors.append("Select a valid duration.")
            if status not in {"pending", "approved", "rejected"}:
                errors.append("Select a valid status.")
            else:
                try:
                    if date.fromisoformat(end_date) < date.fromisoformat(start_date):
                        errors.append("End date must be on or after start date.")
                except ValueError:
                    errors.append("Invalid date format.")

            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template(
                    "leave_edit.html",
                    row=row,
                    reasons=LEAVE_REASONS,
                    durations=DURATION_CHOICES,
                    employees=g.manager_roster,
                    reason_labels=reason_labels,
                    duration_labels=duration_labels,
                    form=request.form,
                    return_year=request.form.get("return_year") or "",
                    return_month=request.form.get("return_month") or "",
                )

            conn = get_db(app)
            conn.execute(
                """
                UPDATE leave_requests
                SET employee_name = ?, reason = ?, description = ?, start_date = ?, end_date = ?,
                    duration_type = ?, status = ?
                WHERE id = ?
                """,
                (employee_name, reason, description, start_date, end_date, duration_type, status, leave_id),
            )
            conn.commit()
            conn.close()
            flash("Saved.", "success")
            try:
                ry = int(request.form.get("return_year") or 0)
                rm = int(request.form.get("return_month") or 0)
                if 1 <= rm <= 12 and ry > 2000:
                    return redirect(url_for("dashboard", year=ry, month=rm))
            except ValueError:
                pass
            return redirect(url_for("dashboard"))

        return render_template(
            "leave_edit.html",
            row=row,
            reasons=LEAVE_REASONS,
            durations=DURATION_CHOICES,
            employees=g.manager_roster,
            reason_labels=reason_labels,
            duration_labels=duration_labels,
            form={k: row[k] for k in row.keys()},
            return_year=request.args.get("year") or "",
            return_month=request.args.get("month") or "",
        )

    @app.route("/meet")
    def meet():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        anchor = _parse_meet_anchor(request.args.get("d"))
        ctx = build_meet_context(app, anchor, roster=g.manager_roster)
        return render_template("meet.html", reasons=LEAVE_REASONS, **ctx)

    @app.route("/meet/approve-day", methods=["POST"])
    def meet_approve_day():
        if not _manager_logged_in():
            return jsonify({"ok": False, "error": "auth"}), 403
        if not _csrf_form_ok():
            return jsonify({"ok": False, "error": "csrf"}), 400

        row, _anchor, err = _meet_validate_leave_in_window(
            app,
            request.form.get("leave_id"),
            (request.form.get("work_date") or "").strip(),
            request.form.get("anchor"),
            g.manager_roster,
        )
        if err or not row:
            return jsonify({"ok": False, "error": err or "bad_input"}), 400

        leave_id = int(row["id"])
        work_date = (request.form.get("work_date") or "").strip()[:10]
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        conn = get_db(app)
        conn.execute(
            """
            INSERT INTO meet_leave_day (leave_id, work_date, decision, updated_at)
            VALUES (?, ?, 'approved', ?)
            ON CONFLICT(leave_id, work_date) DO UPDATE SET
                decision = excluded.decision,
                updated_at = excluded.updated_at
            """,
            (leave_id, work_date, ts),
        )
        conn.commit()
        conn.close()
        _maybe_promote_leave_after_meet_days(app, leave_id)
        return jsonify({"ok": True})

    @app.route("/meet/remove-day", methods=["POST"])
    def meet_remove_day():
        if not _manager_logged_in():
            return jsonify({"ok": False, "error": "auth"}), 403
        if not _csrf_form_ok():
            return jsonify({"ok": False, "error": "csrf"}), 400

        row, _anchor, err = _meet_validate_leave_in_window(
            app,
            request.form.get("leave_id"),
            (request.form.get("work_date") or "").strip(),
            request.form.get("anchor"),
            g.manager_roster,
        )
        if err or not row:
            return jsonify({"ok": False, "error": err or "bad_input"}), 400

        leave_id = int(row["id"])
        work_date = (request.form.get("work_date") or "").strip()[:10]
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        conn = get_db(app)
        conn.execute(
            """
            INSERT INTO meet_leave_day (leave_id, work_date, decision, updated_at)
            VALUES (?, ?, 'removed', ?)
            ON CONFLICT(leave_id, work_date) DO UPDATE SET
                decision = excluded.decision,
                updated_at = excluded.updated_at
            """,
            (leave_id, work_date, ts),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    @app.route("/meet/quick-leave", methods=["POST"])
    def meet_quick_leave():
        if not _manager_logged_in():
            return jsonify({"ok": False, "error": "auth"}), 403
        if not _csrf_form_ok():
            return jsonify({"ok": False, "error": "csrf"}), 400

        emp = (request.form.get("employee_name") or "").strip()
        work_date = (request.form.get("work_date") or "").strip()
        reason = (request.form.get("reason") or "pl").strip()
        duration_type = (request.form.get("duration_type") or "full").strip()
        anchor = _parse_meet_anchor(request.form.get("anchor"))

        if emp not in g.manager_roster or not work_date:
            return jsonify({"ok": False, "error": "bad_input"}), 400
        if reason not in {c[0] for c in LEAVE_REASONS}:
            return jsonify({"ok": False, "error": "reason"}), 400
        if duration_type not in {c[0] for c in DURATION_CHOICES} or duration_type == "multi":
            return jsonify({"ok": False, "error": "dur"}), 400
        try:
            wd = date.fromisoformat(work_date)
        except ValueError:
            return jsonify({"ok": False, "error": "bad_date"}), 400
        d0, d4 = _meet_window(anchor)
        if not (d0 <= wd <= d4):
            return jsonify({"ok": False, "error": "out_of_window"}), 400

        created = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        ip = client_ip()
        conn = get_db(app)
        conn.execute(
            """
            INSERT INTO leave_requests
            (employee_name, reason, description, start_date, end_date, duration_type, status, created_at, submitted_ip)
            VALUES (?, ?, '', ?, ?, ?, 'approved', ?, ?)
            """,
            (emp, reason, work_date, work_date, duration_type, created, ip),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    @app.route("/scrum", methods=["GET"])
    def scrum():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        sprints = list(
            conn.execute(
                """
                SELECT id, name, start_date, end_date, goal, COALESCE(is_closed, 0) AS is_closed
                FROM scrum_sprint
                WHERE team_id = ?
                ORDER BY end_date DESC, start_date DESC, id DESC
                """,
                (team_id,),
            )
        )
        portal_proposals_pending = _count_pending_scrum_portal_proposals(conn, team_id)
        conn.close()
        td = date.today()
        end_default = scrum_sprint_default_end_date(td)
        cap_preview = round(
            compute_team_sprint_capacity_leave_hours(app, g.manager_roster, td, end_default), 1
        )
        return render_template(
            "scrum_hub.html",
            sprints=sprints,
            today_iso=td.isoformat(),
            sprint_capacity_preview_hours=cap_preview,
            portal_proposals_pending=portal_proposals_pending,
        )

    @app.get("/scrum/api/sprint-team-capacity")
    def scrum_api_sprint_team_capacity():
        if not _manager_logged_in():
            return jsonify({"ok": False, "error": "auth"}), 403
        try:
            sd = date.fromisoformat((request.args.get("start_date") or "").strip()[:10])
            ed = date.fromisoformat((request.args.get("end_date") or "").strip()[:10])
        except ValueError:
            return jsonify({"ok": False, "error": "bad_date"}), 400
        if ed < sd:
            sd, ed = ed, sd
        h = compute_team_sprint_capacity_leave_hours(app, g.manager_roster, sd, ed)
        return jsonify(
            {"ok": True, "hours": round(h, 1), "roster_count": len(g.manager_roster)}
        )

    @app.get("/scrum/team-bundle.json")
    def scrum_team_bundle_download():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        try:
            bundle = build_manager_team_bundle_dict(conn, team_id)
        except ValueError as e:
            conn.close()
            flash(str(e), "error")
            return redirect(url_for("scrum"))
        conn.close()
        raw = json.dumps(bundle, indent=2, ensure_ascii=False).encode("utf-8")
        safe = re.sub(r"[^\w.\-]+", "_", (getattr(g, "manager_team_name", None) or "team").strip()) or "team"
        fname = f"{safe}-team-bundle.json"
        return Response(
            raw,
            mimetype="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.post("/scrum/team-bundle/import")
    def scrum_team_bundle_import():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("scrum"))
        f = request.files.get("bundle_file")
        if not f or not (getattr(f, "filename", None) or "").strip():
            flash("Choose a JSON bundle file to import.", "error")
            return redirect(url_for("scrum"))
        try:
            raw = f.read().decode("utf-8")
            bundle = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            flash("Could not read that file as JSON.", "error")
            return redirect(url_for("scrum"))
        if not isinstance(bundle, dict):
            flash("Bundle must be a JSON object.", "error")
            return redirect(url_for("scrum"))
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        try:
            import_manager_team_bundle(conn, team_id, bundle)
            conn.commit()
        except ValueError as e:
            conn.rollback()
            conn.close()
            flash(str(e), "error")
            return redirect(url_for("scrum"))
        except Exception:
            conn.rollback()
            conn.close()
            _log.exception("team bundle import failed")
            flash("Import failed due to a database error.", "error")
            return redirect(url_for("scrum"))
        conn.close()
        flash(
            "Imported team bundle for this team: roster, hub mode, task kinds, sprints, stickies, "
            "activity, goals, daily tasks, appreciation, and portal proposals were replaced.",
            "success",
        )
        return redirect(url_for("scrum"))

    @app.route("/scrum/portal-proposals", methods=["GET"])
    def scrum_portal_proposals():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        rows = list(
            conn.execute(
                """
                SELECT p.*, s.name AS sprint_name,
                  (SELECT title FROM scrum_sprint_item WHERE id = p.item_id) AS item_title
                FROM scrum_portal_proposal p
                JOIN scrum_sprint s ON s.id = p.sprint_id AND s.team_id = p.team_id
                WHERE p.team_id = ? AND p.status = 'pending'
                ORDER BY p.created_at ASC
                """,
                (team_id,),
            )
        )
        conn.close()
        display: list[dict] = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            it = None
            if "item_title" in r.keys():
                it = r["item_title"]
            display.append(
                {
                    "id": int(r["id"]),
                    "created_at": r["created_at"],
                    "sprint_name": r["sprint_name"],
                    "sprint_id": int(r["sprint_id"]),
                    "proposer": r["proposer_roster_name"],
                    "action": r["action"],
                    "summary": _portal_proposal_summary_line(str(r["action"]), payload, it),
                    "payload_pretty": json.dumps(payload, indent=2, ensure_ascii=False)[:12000],
                }
            )
        return render_template("scrum_portal_proposals.html", proposals=display)

    @app.post("/scrum/portal-proposal/resolve")
    def scrum_portal_proposal_resolve():
        next_raw = request.form.get("next")
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return _redirect_after_portal_proposal(next_raw)
        proposal_id = _parse_optional_int(request.form.get("proposal_id"))
        decision = (request.form.get("decision") or "").strip().lower()
        note = (request.form.get("note") or "").strip()[:500]
        if not proposal_id or decision not in ("approve", "reject"):
            flash("Missing proposal or decision.", "error")
            return _redirect_after_portal_proposal(next_raw)
        if decision == "approve" and request.form.get("manager_attest") != "1":
            flash(
                "Check the confirmation box: you must review the change (including DoD checklist when shown) before approving.",
                "error",
            )
            return _redirect_after_portal_proposal(next_raw)
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        try:
            conn.execute("BEGIN IMMEDIATE")
            prop = conn.execute(
                """
                SELECT * FROM scrum_portal_proposal
                WHERE id = ? AND team_id = ? AND status = 'pending'
                """,
                (proposal_id, team_id),
            ).fetchone()
            if not prop:
                conn.rollback()
                conn.close()
                flash("That proposal was not found or is already resolved.", "error")
                return _redirect_after_portal_proposal(next_raw)
            ts = _utc_stamp()
            if decision == "reject":
                conn.execute(
                    """
                    UPDATE scrum_portal_proposal
                    SET status = 'rejected', resolved_at = ?, resolution_note = ?
                    WHERE id = ? AND team_id = ? AND status = 'pending'
                    """,
                    (ts, note, proposal_id, team_id),
                )
                conn.commit()
                conn.close()
                flash("Proposal rejected.", "success")
                return _redirect_after_portal_proposal(next_raw)
            ok, err = _apply_scrum_portal_proposal_core(conn, prop, team_id)
            if not ok:
                conn.rollback()
                conn.close()
                flash(
                    SCRUM_SPRINT_READONLY_FLASH
                    if err == "sprint_closed"
                    else f"Could not apply change: {err}",
                    "error",
                )
                return _redirect_after_portal_proposal(next_raw)
            conn.execute(
                """
                UPDATE scrum_portal_proposal
                SET status = 'approved', resolved_at = ?, resolution_note = ?
                WHERE id = ? AND team_id = ? AND status = 'pending'
                """,
                (ts, note, proposal_id, team_id),
            )
            conn.commit()
        except Exception as ex:
            conn.rollback()
            conn.close()
            _log.exception("portal proposal approve failed: %s", ex)
            flash("Something went wrong while applying the change.", "error")
            return _redirect_after_portal_proposal(next_raw)
        conn.close()
        flash("Change applied to the sprint board.", "success")
        return _redirect_after_portal_proposal(next_raw)

    @app.route("/scrum/sprint/<int:sprint_id>", methods=["GET"])
    def scrum_sprint_team(sprint_id: int):
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        sprint = conn.execute(
            "SELECT * FROM scrum_sprint WHERE id = ? AND team_id = ?", (int(sprint_id), team_id)
        ).fetchone()
        if not sprint:
            conn.close()
            flash("Sprint not found.", "error")
            return redirect(url_for("scrum"))
        session["active_sprint_id"] = int(sprint_id)
        members = _sprint_team_overview_rows(app, conn, team_id, int(sprint_id), g.manager_roster)
        pending_by = _pending_portal_proposals_grouped_for_sprint(conn, team_id, int(sprint_id))
        for m in members:
            m["pending_portal"] = pending_by.get(m["name"], [])
        bd_ctx = build_sprint_burndown_chart_context(
            conn, int(sprint_id), str(sprint["start_date"])[:10], str(sprint["end_date"])[:10]
        )
        kind_stack_ctx = build_sprint_task_kind_stack_chart_context(conn, team_id, int(sprint_id))
        portal_proposals_pending = _count_pending_scrum_portal_proposals(conn, team_id, int(sprint_id))
        conn.close()
        _tcap = sprint["team_capacity_hours"] if "team_capacity_hours" in sprint.keys() else None
        sprint_team_capacity_hours = float(_tcap) if _tcap is not None else None
        if sprint_team_capacity_hours is None:
            try:
                sd = date.fromisoformat(str(sprint["start_date"])[:10])
                ed = date.fromisoformat(str(sprint["end_date"])[:10])
                sprint_team_capacity_hours = round(
                    compute_team_sprint_capacity_leave_hours(app, g.manager_roster, sd, ed), 1
                )
            except ValueError:
                sprint_team_capacity_hours = None
        return render_template(
            "scrum_sprint_team.html",
            sprint_id=int(sprint_id),
            sprint_name=sprint["name"],
            sprint_start=str(sprint["start_date"])[:10],
            sprint_end=str(sprint["end_date"])[:10],
            sprint_team_capacity_hours=sprint_team_capacity_hours,
            sprint_readonly=_sprint_row_is_closed(sprint),
            sprint_status_label="CLOSED" if _sprint_row_is_closed(sprint) else "OPEN",
            members=members,
            portal_proposals_pending=portal_proposals_pending,
            **bd_ctx,
            **kind_stack_ctx,
        )

    @app.route("/scrum/sprint/<int:sprint_id>/leave-tracker", methods=["GET"])
    def scrum_sprint_leave_tracker(sprint_id: int):
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        sprint = conn.execute(
            "SELECT * FROM scrum_sprint WHERE id = ? AND team_id = ?", (int(sprint_id), team_id)
        ).fetchone()
        if not sprint:
            conn.close()
            flash("Sprint not found.", "error")
            return redirect(url_for("scrum"))
        try:
            sd = date.fromisoformat(str(sprint["start_date"])[:10])
            ed = date.fromisoformat(str(sprint["end_date"])[:10])
        except ValueError:
            conn.close()
            flash("Invalid sprint dates.", "error")
            return redirect(url_for("scrum_sprint_team", sprint_id=sprint_id))
        if sd > ed:
            sd, ed = ed, sd
        days_meta, grid_rows = build_sprint_leave_tracker_context(conn, app, sd, ed, g.manager_roster)
        conn.close()
        if sd.year == ed.year:
            if sd.month == ed.month:
                date_range_label = f"{sd.strftime('%b')} {sd.day}–{ed.day}, {sd.year}"
            else:
                date_range_label = f"{sd.strftime('%b %d')} – {ed.strftime('%b %d, %Y')}"
        else:
            date_range_label = f"{sd.strftime('%b %d, %Y')} – {ed.strftime('%b %d, %Y')}"
        return render_template(
            "scrum_sprint_leave_tracker.html",
            sprint_id=int(sprint_id),
            sprint_name=sprint["name"],
            sprint_start=sd.isoformat(),
            sprint_end=ed.isoformat(),
            date_range_label=date_range_label,
            days=days_meta,
            grid_rows=grid_rows,
            back_url=url_for("scrum_sprint_team", sprint_id=sprint_id),
        )

    @app.route("/scrum/sprint/<int:sprint_id>/board", methods=["GET"])
    def scrum_member_board(sprint_id: int):
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        raw = (request.args.get("assignee") or "").strip()
        assignee = resolve_employee_name(raw, roster=g.manager_roster) or raw[:200]
        if not assignee.strip():
            flash("Pick a team member (assignee).", "error")
            return redirect(url_for("scrum_sprint_team", sprint_id=sprint_id))
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        sprint = conn.execute(
            "SELECT * FROM scrum_sprint WHERE id = ? AND team_id = ?", (int(sprint_id), team_id)
        ).fetchone()
        if not sprint:
            conn.close()
            flash("Sprint not found.", "error")
            return redirect(url_for("scrum"))
        cards_by_col = _load_kanban_cards(conn, int(sprint_id), assignee, team_id)
        task_kinds_rows = _list_team_task_kinds(conn, team_id)
        conn.close()
        kb_leave_ctx = build_kanban_leave_worksheet_context(
            app,
            int(sprint_id),
            str(sprint["start_date"])[:10],
            str(sprint["end_date"])[:10],
            assignee,
        )
        return render_template(
            "scrum_kanban.html",
            sprint_id=int(sprint_id),
            sprint_name=sprint["name"],
            assignee=assignee,
            columns=SCRUM_KANBAN_COLUMNS,
            cards_by_col=cards_by_col,
            task_kinds_rows=task_kinds_rows,
            kb_back_url=url_for("scrum_sprint_team", sprint_id=sprint_id),
            kb_api_urls={
                "item_move": url_for("scrum_api_item_move"),
                "item_note": url_for("scrum_api_item_note"),
                "item_activity_update": url_for("scrum_api_activity_update"),
                "item_add": url_for("scrum_api_item_add"),
                "item_appreciation": url_for("scrum_api_item_appreciation"),
                "item_appreciation_delete_all": url_for("scrum_api_item_appreciation_delete_all"),
            },
            kb_form_urls={
                "item_update": url_for("scrum_sprint_item_update"),
                "item_delete": url_for("scrum_sprint_item_delete"),
            },
            portal_kanban=False,
            sprint_readonly=_sprint_row_is_closed(sprint),
            **kb_leave_ctx,
        )

    @app.post("/scrum/api/item/move")
    def scrum_api_item_move():
        if not _manager_logged_in():
            return jsonify({"ok": False, "error": "auth"}), 403
        if not _csrf_api_ok(app):
            return jsonify({"ok": False, "error": "csrf"}), 400
        data = request.get_json(silent=True) or {}
        item_id = _parse_optional_int(data.get("item_id"))
        sprint_id = _parse_optional_int(data.get("sprint_id"))
        assignee_raw = (data.get("assignee") or "").strip()
        assignee = resolve_employee_name(assignee_raw, roster=g.manager_roster) or assignee_raw[:200]
        to_col = _normalize_kanban_column(data.get("to_column"))
        body = (data.get("note") or "").strip()[:2000]
        if not item_id or not sprint_id:
            return jsonify({"ok": False, "error": "missing"}), 400
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        if not _item_belongs_to_sprint_team(conn, item_id, sprint_id, team_id, assignee):
            conn.close()
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if _sprint_is_closed_by_id(conn, sprint_id):
            conn.close()
            return jsonify({"ok": False, "error": "sprint_closed"}), 403
        row = conn.execute(
            "SELECT kanban_column FROM scrum_sprint_item WHERE id = ?", (item_id,)
        ).fetchone()
        from_col = _normalize_kanban_column(row["kanban_column"] if row else None)
        if from_col == "do" and to_col == "doing":
            conn.execute("DELETE FROM scrum_item_activity WHERE item_id = ?", (item_id,))
            ch = _parse_committed_hours_for_kanban_move(
                data.get("committed_hours"), from_col=from_col, to_col=to_col
            )
        elif from_col == "doing" and to_col == "do":
            conn.execute("DELETE FROM scrum_item_activity WHERE item_id = ?", (item_id,))
            ch = 0.0
        else:
            ch = _parse_committed_hours_for_kanban_move(
                data.get("committed_hours"), from_col=from_col, to_col=to_col
            )
        ts = _utc_stamp()
        st = _status_for_kanban_column(to_col)
        artifacts_json: str | None = None
        if to_col == "done":
            artifacts_json, aerr = _normalize_done_artifacts_from_api(data.get("artifacts"))
            if aerr:
                conn.close()
                return jsonify({"ok": False, "error": aerr}), 400
        if to_col == "done" and artifacts_json is not None:
            conn.execute(
                """
                UPDATE scrum_sprint_item
                SET kanban_column = ?, status = ?, updated_at = ?, done_artifacts = ?
                WHERE id = ? AND sprint_id = ?
                """,
                (to_col, st, ts, artifacts_json, item_id, sprint_id),
            )
        else:
            conn.execute(
                """
                UPDATE scrum_sprint_item
                SET kanban_column = ?, status = ?, updated_at = ?
                WHERE id = ? AND sprint_id = ?
                """,
                (to_col, st, ts, item_id, sprint_id),
            )
        conn.execute(
            """
            INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id, body, ch, from_col, to_col, ts),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    @app.post("/scrum/api/item/note")
    def scrum_api_item_note():
        if not _manager_logged_in():
            return jsonify({"ok": False, "error": "auth"}), 403
        if not _csrf_api_ok(app):
            return jsonify({"ok": False, "error": "csrf"}), 400
        data = request.get_json(silent=True) or {}
        item_id = _parse_optional_int(data.get("item_id"))
        sprint_id = _parse_optional_int(data.get("sprint_id"))
        assignee_raw = (data.get("assignee") or "").strip()
        assignee = resolve_employee_name(assignee_raw, roster=g.manager_roster) or assignee_raw[:200]
        body = (data.get("note") or "").strip()[:2000]
        ch = _parse_hours_field(str(data.get("committed_hours")))
        if not item_id or not sprint_id:
            return jsonify({"ok": False, "error": "missing"}), 400
        if not body and ch <= SCRUM_HOUR_EPS:
            return jsonify({"ok": False, "error": "missing"}), 400
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        if not _item_belongs_to_sprint_team(conn, item_id, sprint_id, team_id, assignee):
            conn.close()
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if _sprint_is_closed_by_id(conn, sprint_id):
            conn.close()
            return jsonify({"ok": False, "error": "sprint_closed"}), 403
        row = conn.execute(
            "SELECT kanban_column FROM scrum_sprint_item WHERE id = ?", (item_id,)
        ).fetchone()
        if not row or _normalize_kanban_column(row["kanban_column"]) != "doing":
            conn.close()
            return jsonify({"ok": False, "error": "only_doing"}), 400
        ts = _utc_stamp()
        conn.execute(
            "UPDATE scrum_sprint_item SET updated_at = ? WHERE id = ?",
            (ts, item_id),
        )
        conn.execute(
            """
            INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
            VALUES (?, ?, ?, 'doing', 'doing', ?)
            """,
            (item_id, body, ch, ts),
        )
        conn.commit()
        conn.close()
        conn.close()
        return jsonify({"ok": True})

    @app.post("/scrum/api/item/activity_update")
    def scrum_api_activity_update():
        if not _manager_logged_in():
            return jsonify({"ok": False, "error": "auth"}), 403
        if not _csrf_api_ok(app):
            return jsonify({"ok": False, "error": "csrf"}), 400
        data = request.get_json(silent=True) or {}
        item_id = _parse_optional_int(data.get("item_id"))
        sprint_id = _parse_optional_int(data.get("sprint_id"))
        activity_id = _parse_optional_int(data.get("activity_id"))
        assignee_raw = (data.get("assignee") or "").strip()
        assignee = resolve_employee_name(assignee_raw, roster=g.manager_roster) or assignee_raw[:200]
        body = (data.get("note") or "").strip()[:2000]
        ch = _parse_hours_field(str(data.get("committed_hours")))
        if not item_id or not sprint_id or not activity_id:
            return jsonify({"ok": False, "error": "missing"}), 400
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        if not _item_belongs_to_sprint_team(conn, item_id, sprint_id, team_id, assignee):
            conn.close()
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if _sprint_is_closed_by_id(conn, sprint_id):
            conn.close()
            return jsonify({"ok": False, "error": "sprint_closed"}), 403
        
        ts = _utc_stamp()
        conn.execute(
            "UPDATE scrum_item_activity SET body = ?, committed_hours = ? WHERE id = ? AND item_id = ?",
            (body, ch, activity_id, item_id),
        )
        conn.execute(
            "UPDATE scrum_sprint_item SET updated_at = ? WHERE id = ?",
            (ts, item_id),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    @app.post("/scrum/api/item/appreciation")
    def scrum_api_item_appreciation():
        """Record a manager 'Well Done' appreciation comment on a sticky."""
        if not _manager_logged_in():
            return jsonify({"ok": False, "error": "auth"}), 403
        if not _csrf_api_ok(app):
            return jsonify({"ok": False, "error": "csrf"}), 400
        data = request.get_json(silent=True) or {}
        item_id = _parse_optional_int(data.get("item_id"))
        sprint_id = _parse_optional_int(data.get("sprint_id"))
        assignee_raw = (data.get("assignee") or "").strip()
        assignee = resolve_employee_name(assignee_raw, roster=g.manager_roster) or assignee_raw[:200]
        comment = (data.get("comment") or "").strip()[:SCRUM_APPRECIATION_BODY_MAX]
        if not item_id or not sprint_id:
            return jsonify({"ok": False, "error": "missing"}), 400
        if not comment:
            return jsonify({"ok": False, "error": "missing_comment"}), 400
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        if not _item_belongs_to_sprint_team(conn, item_id, sprint_id, team_id, assignee):
            conn.close()
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if _sprint_is_closed_by_id(conn, sprint_id):
            conn.close()
            return jsonify({"ok": False, "error": "sprint_closed"}), 403
        author = str(getattr(g, "manager_team_name", "") or "").strip() or "Manager"
        ts = _utc_stamp()
        conn.execute(
            """
            INSERT INTO scrum_item_appreciation (item_id, author, comment, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (item_id, author[:200], comment, ts),
        )
        conn.execute(
            "UPDATE scrum_sprint_item SET updated_at = ? WHERE id = ? AND sprint_id = ?",
            (ts, item_id, sprint_id),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    @app.post("/scrum/api/item/appreciation/delete-all")
    def scrum_api_item_appreciation_delete_all():
        """Remove all manager appreciation rows for one sticky (manager kanban)."""
        if not _manager_logged_in():
            return jsonify({"ok": False, "error": "auth"}), 403
        if not _csrf_api_ok(app):
            return jsonify({"ok": False, "error": "csrf"}), 400
        data = request.get_json(silent=True) or {}
        item_id = _parse_optional_int(data.get("item_id"))
        sprint_id = _parse_optional_int(data.get("sprint_id"))
        assignee_raw = (data.get("assignee") or "").strip()
        assignee = resolve_employee_name(assignee_raw, roster=g.manager_roster) or assignee_raw[:200]
        if not item_id or not sprint_id:
            return jsonify({"ok": False, "error": "missing"}), 400
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        if not _item_belongs_to_sprint_team(conn, item_id, sprint_id, team_id, assignee):
            conn.close()
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if _sprint_is_closed_by_id(conn, sprint_id):
            conn.close()
            return jsonify({"ok": False, "error": "sprint_closed"}), 403
        cur = conn.execute("DELETE FROM scrum_item_appreciation WHERE item_id = ?", (item_id,))
        deleted = int(cur.rowcount or 0)
        ts = _utc_stamp()
        conn.execute(
            "UPDATE scrum_sprint_item SET updated_at = ? WHERE id = ? AND sprint_id = ?",
            (ts, item_id, sprint_id),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/scrum/api/task-kind/add")
    def scrum_api_task_kind_add():
        if not _manager_logged_in():
            return jsonify({"ok": False, "error": "auth"}), 403
        if not _csrf_api_ok(app):
            return jsonify({"ok": False, "error": "csrf"}), 400
        return jsonify({"ok": False, "error": "fixed_kinds_only"}), 400

    @app.post("/scrum/api/item/add")
    def scrum_api_item_add():
        if not _manager_logged_in():
            return jsonify({"ok": False, "error": "auth"}), 403
        if not _csrf_api_ok(app):
            return jsonify({"ok": False, "error": "csrf"}), 400
        data = request.get_json(silent=True) or {}
        sprint_id = _parse_optional_int(data.get("sprint_id"))
        assignee_raw = (data.get("assignee") or "").strip()
        assignee = resolve_employee_name(assignee_raw, roster=g.manager_roster) or assignee_raw[:200]
        title = (data.get("title") or "").strip()[:500]
        notes = (data.get("notes") or "").strip()[:2000]
        dod = (data.get("dod") or "").strip()[:4000]
        area = (data.get("area") or "").strip()[:SCRUM_STICKY_AREA_MAX_LEN]
        est = _parse_hours_field(str(data.get("estimate_hours")))
        if not sprint_id or not title or not assignee.strip():
            return jsonify({"ok": False, "error": "missing"}), 400
        if est <= SCRUM_HOUR_EPS:
            return jsonify({"ok": False, "error": "estimate_required"}), 400
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        if not _sprint_row_for_team(conn, sprint_id, team_id):
            conn.close()
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if _sprint_is_closed_by_id(conn, sprint_id):
            conn.close()
            return jsonify({"ok": False, "error": "sprint_closed"}), 403
        _ensure_team_task_kinds(conn, team_id)
        new_l = (data.get("new_kind_label") or "").strip()
        if new_l:
            conn.close()
            return jsonify({"ok": False, "error": "no_new_kinds"}), 400
        raw = str(data.get("task_kind_code") or "ndy").strip().lower()
        if raw in SCRUM_LEGACY_TASK_KIND_MAP:
            raw = SCRUM_LEGACY_TASK_KIND_MAP[raw]
        tcode = raw if raw in SCRUM_TASK_KIND_CODES else "ndy"
        ts = _utc_stamp()
        st = _status_for_kanban_column("do")
        mx_row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM scrum_sprint_item WHERE sprint_id = ?",
            (sprint_id,),
        ).fetchone()
        mx = int(mx_row["n"] if mx_row is not None else 0)
        conn.execute(
            """
            INSERT INTO scrum_sprint_item
            (sprint_id, assignee, title, estimate_hours, status, notes, dod, sort_order, created_at, updated_at, kanban_column, task_kind, sticky_color_hex, area)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'do', ?, ?, ?)
            """,
            (sprint_id, assignee, title, est, st, notes, dod, mx, ts, ts, tcode, None, area),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    @app.post("/scrum/sprint/export-xlsx")
    def scrum_sprint_export_xlsx():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("scrum"))
        try:
            sprint_id = int((request.form.get("sprint_id") or "").strip())
        except ValueError:
            flash("Pick a valid sprint.", "error")
            return redirect(url_for("scrum"))
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        blob, meta = build_sprint_export_xlsx_bytes(
            conn,
            team_id,
            sprint_id,
            app=app,
            manager_roster=g.manager_roster,
        )
        conn.close()
        if blob is None:
            flash(meta, "error")
            return redirect(url_for("scrum"))
        return send_file(
            io.BytesIO(blob),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=meta,
        )

    @app.post("/team/hub-mode")
    def team_hub_mode_set():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("dashboard"))
        mode = _normalize_hub_mode(request.form.get("hub_mode"))
        tid = int(g.manager_team_id)
        conn = get_db(app)
        conn.execute("UPDATE teams SET hub_mode = ? WHERE id = ?", (mode, tid))
        conn.commit()
        conn.close()
        label = "Scrum dashboard" if mode == "scrum" else "Leave tracker"
        flash(f"Saved: this team’s hub is set to {label}.", "success")
        dest = _safe_next_path(request.form.get("next"))
        if dest:
            return redirect(dest)
        return redirect(url_for("scrum" if mode == "scrum" else "dashboard"))

    @app.post("/scrum/sprint/create")
    def scrum_sprint_create():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("scrum"))
        name = (request.form.get("name") or "").strip() or "Sprint"
        goal = ""
        try:
            sd = date.fromisoformat((request.form.get("start_date") or "").strip()[:10])
        except ValueError:
            flash("Invalid sprint dates (use YYYY-MM-DD).", "error")
            return redirect(url_for("scrum"))
        ed = scrum_sprint_default_end_date(sd)
        ts = _utc_stamp()
        team_id = int(g.manager_team_id)
        cap_h = compute_team_sprint_capacity_leave_hours(app, g.manager_roster, sd, ed)
        conn = get_db(app)
        if _sprint_name_exists_for_team(conn, team_id, name):
            conn.close()
            flash(
                f'A sprint named "{name}" already exists for this team. Choose a different name.',
                "error",
            )
            return redirect(url_for("scrum"))
        cur = conn.execute(
            """
            INSERT INTO scrum_sprint (team_id, name, start_date, end_date, goal, team_capacity_hours, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (team_id, name, sd.isoformat(), ed.isoformat(), goal, cap_h, ts, ts),
        )
        new_id = int(cur.lastrowid)
        n_carried = _carry_forward_do_doing_to_new_sprint(conn, team_id, new_id, sd, ts)
        conn.commit()
        conn.close()
        session["active_sprint_id"] = new_id
        if n_carried:
            flash(
                f"Sprint created — carried {n_carried} sticky(ies) from Do / In progress on the prior sprint into this sprint’s backlog (same assignees and details; estimates reduced by logged Burnt hours).",
                "success",
            )
        else:
            flash("Sprint created — open it to see the team and sticky-note boards.", "success")
        return redirect(url_for("scrum_sprint_team", sprint_id=new_id))

    @app.post("/scrum/sprint/delete")
    def scrum_sprint_delete():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("scrum"))
        sid = _parse_optional_int(request.form.get("sprint_id"))
        if not sid:
            flash("Missing sprint.", "error")
            return redirect(url_for("scrum"))
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        if not _sprint_row_for_team(conn, sid, team_id):
            conn.close()
            flash("Unknown sprint.", "error")
            return redirect(url_for("scrum"))
        if _sprint_is_closed_by_id(conn, sid):
            conn.close()
            flash(SCRUM_SPRINT_READONLY_FLASH, "error")
            return redirect(url_for("scrum_sprint_team", sprint_id=sid))
        conn.execute("DELETE FROM scrum_sprint WHERE id = ? AND team_id = ?", (sid, team_id))
        conn.commit()
        conn.close()
        if session.get("active_sprint_id") == sid:
            session.pop("active_sprint_id", None)
        flash("Sprint deleted.", "success")
        return redirect(url_for("scrum"))

    @app.post("/scrum/sprint/update")
    def scrum_sprint_update():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("scrum"))
        sid = _parse_optional_int(request.form.get("sprint_id"))
        if not sid:
            flash("Missing sprint.", "error")
            return redirect(url_for("scrum"))
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        if not _sprint_row_for_team(conn, sid, team_id):
            conn.close()
            flash("Unknown sprint.", "error")
            return redirect(url_for("scrum"))
        if _sprint_is_closed_by_id(conn, sid):
            conn.close()
            flash(SCRUM_SPRINT_READONLY_FLASH, "error")
            return redirect(url_for("scrum_sprint_team", sprint_id=sid))
        try:
            sd = date.fromisoformat((request.form.get("start_date") or "").strip()[:10])
        except ValueError:
            conn.close()
            flash("Invalid sprint dates (use YYYY-MM-DD).", "error")
            return redirect(url_for("scrum_sprint_team", sprint_id=sid))
        ed = scrum_sprint_default_end_date(sd)
        ts = _utc_stamp()
        cap_h = compute_team_sprint_capacity_leave_hours(app, g.manager_roster, sd, ed)
        conn.execute(
            """
            UPDATE scrum_sprint
            SET start_date = ?, end_date = ?, team_capacity_hours = ?, updated_at = ?
            WHERE id = ? AND team_id = ?
            """,
            (sd.isoformat(), ed.isoformat(), cap_h, ts, sid, team_id),
        )
        conn.commit()
        conn.close()
        flash("Sprint start updated — end date is always 14 calendar days from start (inclusive).", "success")
        return redirect(url_for("scrum_sprint_team", sprint_id=sid))

    @app.post("/scrum/sprint/rename")
    def scrum_sprint_rename():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("scrum"))
        sid = _parse_optional_int(request.form.get("sprint_id"))
        if not sid:
            flash("Missing sprint.", "error")
            return redirect(url_for("scrum"))
        team_id = int(g.manager_team_id)
        name = (request.form.get("name") or "").strip()[:200]
        if not name:
            flash("Sprint name is required.", "error")
            return redirect(url_for("scrum"))
        conn = get_db(app)
        row = _sprint_row_for_team(conn, sid, team_id)
        if not row:
            conn.close()
            flash("Unknown sprint.", "error")
            return redirect(url_for("scrum"))
        if _sprint_row_is_closed(row):
            conn.close()
            flash(SCRUM_SPRINT_READONLY_FLASH, "error")
            return redirect(url_for("scrum"))
        old = (str(row["name"]) or "").strip()
        if name.lower() == old.lower():
            conn.close()
            return redirect(url_for("scrum"))
        if _sprint_name_taken_by_other(conn, team_id, name, sid):
            conn.close()
            flash(
                f'A sprint named "{name}" already exists for this team. Choose a different name.',
                "error",
            )
            return redirect(url_for("scrum"))
        ts = _utc_stamp()
        conn.execute(
            "UPDATE scrum_sprint SET name = ?, updated_at = ? WHERE id = ? AND team_id = ?",
            (name, ts, sid, team_id),
        )
        conn.commit()
        conn.close()
        flash("Sprint name updated.", "success")
        return redirect(url_for("scrum"))

    @app.post("/scrum/sprint/close")
    def scrum_sprint_close():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("scrum"))
        sid = _parse_optional_int(request.form.get("sprint_id"))
        if not sid:
            flash("Missing sprint.", "error")
            return redirect(url_for("scrum"))
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        if not _sprint_row_for_team(conn, sid, team_id):
            conn.close()
            flash("Unknown sprint.", "error")
            return redirect(url_for("scrum"))
        if _sprint_is_closed_by_id(conn, sid):
            conn.close()
            flash("This sprint is already closed.", "info")
            return redirect(url_for("scrum_sprint_team", sprint_id=sid))
        ts = _utc_stamp()
        conn.execute(
            "UPDATE scrum_sprint SET is_closed = 1, updated_at = ? WHERE id = ? AND team_id = ?",
            (ts, sid, team_id),
        )
        conn.commit()
        conn.close()
        flash("Sprint closed — the board and sprint details are read-only until you open it again.", "success")
        return redirect(url_for("scrum_sprint_team", sprint_id=sid))

    @app.post("/scrum/sprint/open")
    def scrum_sprint_open():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("scrum"))
        sid = _parse_optional_int(request.form.get("sprint_id"))
        if not sid:
            flash("Missing sprint.", "error")
            return redirect(url_for("scrum"))
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        if not _sprint_row_for_team(conn, sid, team_id):
            conn.close()
            flash("Unknown sprint.", "error")
            return redirect(url_for("scrum"))
        if not _sprint_is_closed_by_id(conn, sid):
            conn.close()
            flash("This sprint is already open.", "info")
            return redirect(url_for("scrum_sprint_team", sprint_id=sid))
        ts = _utc_stamp()
        conn.execute(
            "UPDATE scrum_sprint SET is_closed = 0, updated_at = ? WHERE id = ? AND team_id = ?",
            (ts, sid, team_id),
        )
        conn.commit()
        conn.close()
        flash("Sprint opened — editing is enabled again.", "success")
        return redirect(url_for("scrum_sprint_team", sprint_id=sid))

    @app.post("/scrum/sprint/item/add")
    def scrum_sprint_item_add():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("scrum"))
        sprint_id = _parse_optional_int(request.form.get("sprint_id"))
        team_id = int(g.manager_team_id)
        conn = get_db(app)
        if not sprint_id or not _sprint_row_for_team(conn, sprint_id, team_id):
            conn.close()
            flash("Invalid sprint.", "error")
            return redirect(url_for("scrum"))
        if _sprint_is_closed_by_id(conn, sprint_id):
            conn.close()
            flash(SCRUM_SPRINT_READONLY_FLASH, "error")
            return redirect(url_for("scrum_sprint_team", sprint_id=sprint_id))
        title = (request.form.get("title") or "").strip()
        if not title:
            conn.close()
            flash("Task title is required.", "error")
            return redirect(url_for("scrum_sprint_team", sprint_id=sprint_id))
        assign_raw = (request.form.get("assignee") or "").strip()
        assignee = resolve_employee_name(assign_raw, roster=g.manager_roster) or assign_raw[:200]
        est = _parse_hours_field(request.form.get("estimate_hours"))
        if est <= SCRUM_HOUR_EPS:
            conn.close()
            flash("Estimated hours are required (must be greater than zero).", "error")
            return redirect(url_for("scrum_sprint_team", sprint_id=sprint_id))
        notes = (request.form.get("notes") or "").strip()[:2000]
        dod = (request.form.get("dod") or "").strip()[:4000]
        area = (request.form.get("area") or "").strip()[:SCRUM_STICKY_AREA_MAX_LEN]
        kcol = _normalize_kanban_column(request.form.get("kanban_column") or "do")
        raw_tk = request.form.get("task_kind")
        if raw_tk is None:
            raw_tk = "ndy"
        tkind = _coerce_sprint_item_task_kind(conn, team_id, raw_tk)
        sticky_hex: str | None = None
        st = _status_for_kanban_column(kcol)
        ts = _utc_stamp()
        mx_row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM scrum_sprint_item WHERE sprint_id = ?",
            (sprint_id,),
        ).fetchone()
        mx = int(mx_row["n"] if mx_row is not None else 0)
        conn.execute(
            """
            INSERT INTO scrum_sprint_item
            (sprint_id, assignee, title, estimate_hours, status, notes, dod, sort_order, created_at, updated_at, kanban_column, task_kind, sticky_color_hex, area)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sprint_id, assignee, title, est, st, notes, dod, mx, ts, ts, kcol, tkind, sticky_hex, area),
        )
        conn.commit()
        conn.close()
        flash("Sticky added.", "success")
        return redirect(
            url_for("scrum_member_board", sprint_id=sprint_id, assignee=assignee)
        )

    @app.post("/scrum/sprint/item/update")
    def scrum_sprint_item_update():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("scrum"))
        item_id = _parse_optional_int(request.form.get("item_id"))
        sprint_id = _parse_optional_int(request.form.get("sprint_id"))
        team_id = int(g.manager_team_id)
        if not item_id or not sprint_id:
            flash("Missing backlog item.", "error")
            return redirect(url_for("scrum"))
        conn = get_db(app)
        if not _sprint_row_for_team(conn, sprint_id, team_id):
            conn.close()
            flash("Invalid sprint.", "error")
            return redirect(url_for("scrum"))
        if _sprint_is_closed_by_id(conn, sprint_id):
            conn.close()
            flash(SCRUM_SPRINT_READONLY_FLASH, "error")
            return redirect(url_for("scrum_sprint_team", sprint_id=sprint_id))
        row = conn.execute(
            "SELECT assignee, kanban_column, sticky_color_hex FROM scrum_sprint_item WHERE id = ? AND sprint_id = ?",
            (item_id, sprint_id),
        ).fetchone()
        if not row:
            conn.close()
            flash("Item not found.", "error")
            return redirect(url_for("scrum_sprint_team", sprint_id=sprint_id))
        assignee = (row["assignee"] or "").strip()
        cur_col = _normalize_kanban_column(row["kanban_column"] if "kanban_column" in row.keys() else None)
        ts = _utc_stamp()

        if cur_col == "done":
            notes = (request.form.get("notes") or "").strip()[:2000]
            da_lines = request.form.get("done_artifacts_lines") or ""
            da_json, da_err = _normalize_done_artifacts_from_lines(da_lines)
            if da_err:
                conn.close()
                if da_err == "artifacts_limit":
                    msg = "Too many artifact links (maximum 20)."
                else:
                    msg = "Artifact links must be valid http(s) URLs (one per line, optional label before |)."
                flash(msg, "error")
                return redirect(url_for("scrum_member_board", sprint_id=sprint_id, assignee=assignee))
            conn.execute(
                """
                UPDATE scrum_sprint_item SET notes = ?, done_artifacts = ?, updated_at = ?
                WHERE id = ? AND sprint_id = ?
                """,
                (notes, da_json, ts, item_id, sprint_id),
            )
            conn.commit()
            conn.close()
            flash("Done sticky updated (details and artifacts).", "success")
            return redirect(url_for("scrum_member_board", sprint_id=sprint_id, assignee=assignee))

        if cur_col != "do":
            conn.close()
            flash("Planning edits are only in Do; details and artifacts can be edited in Done.", "error")
            return redirect(url_for("scrum_member_board", sprint_id=sprint_id, assignee=assignee))
        title = (request.form.get("title") or "").strip()
        if not title:
            conn.close()
            flash("Title is required.", "error")
            return redirect(url_for("scrum_member_board", sprint_id=sprint_id, assignee=assignee))
        est = _parse_hours_field(request.form.get("estimate_hours"))
        if est <= SCRUM_HOUR_EPS:
            conn.close()
            flash("Estimated hours must be greater than zero while planning in Do.", "error")
            return redirect(url_for("scrum_member_board", sprint_id=sprint_id, assignee=assignee))
        tkind = _coerce_sprint_item_task_kind(conn, team_id, request.form.get("task_kind"))
        notes = (request.form.get("notes") or "").strip()[:2000]
        dod = (request.form.get("dod") or "").strip()[:4000]
        area = (request.form.get("area") or "").strip()[:SCRUM_STICKY_AREA_MAX_LEN]
        sticky_hex = None
        st = _status_for_kanban_column(cur_col)
        conn.execute(
            """
            UPDATE scrum_sprint_item SET
                title = ?, estimate_hours = ?, task_kind = ?, notes = ?, dod = ?, status = ?, updated_at = ?, sticky_color_hex = ?, area = ?
            WHERE id = ? AND sprint_id = ?
            """,
            (title, est, tkind, notes, dod, st, ts, sticky_hex, area, item_id, sprint_id),
        )
        conn.commit()
        conn.close()
        flash("Plan saved — you can edit again while this sticky stays in Do.", "success")
        return redirect(url_for("scrum_member_board", sprint_id=sprint_id, assignee=assignee))

    @app.post("/scrum/sprint/item/delete")
    def scrum_sprint_item_delete():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("scrum"))
        item_id = _parse_optional_int(request.form.get("item_id"))
        sprint_id = _parse_optional_int(request.form.get("sprint_id"))
        team_id = int(g.manager_team_id)
        if not item_id or not sprint_id:
            flash("Missing item.", "error")
            return redirect(url_for("scrum"))
        conn = get_db(app)
        if not _sprint_row_for_team(conn, sprint_id, team_id):
            conn.close()
            flash("Invalid sprint.", "error")
            return redirect(url_for("scrum"))
        if _sprint_is_closed_by_id(conn, sprint_id):
            conn.close()
            flash(SCRUM_SPRINT_READONLY_FLASH, "error")
            return redirect(url_for("scrum_sprint_team", sprint_id=sprint_id))
        ar = conn.execute(
            "SELECT assignee FROM scrum_sprint_item WHERE id = ? AND sprint_id = ?",
            (item_id, sprint_id),
        ).fetchone()
        assignee = (ar["assignee"] or "").strip() if ar else ""
        conn.execute("DELETE FROM scrum_sprint_item WHERE id = ? AND sprint_id = ?", (item_id, sprint_id))
        conn.commit()
        conn.close()
        flash("Sticky removed.", "success")
        if assignee:
            return redirect(url_for("scrum_member_board", sprint_id=sprint_id, assignee=assignee))
        return redirect(url_for("scrum_sprint_team", sprint_id=sprint_id))

    @app.route("/")
    def home():
        return redirect(url_for("login_page"))

    @app.route("/login", methods=["GET", "POST"])
    def login_page():
        if request.method == "GET":
            if _manager_logged_in():
                return redirect(url_for("dashboard"))
            otp_ok = _portal_otp_mail_configured(app)
            return render_template(
                "login.html",
                hide_nav=True,
                email_otp_configured=otp_ok,
                portal_otp_pending_email=session.get("portal_otp_email"),
            )

        gate = (request.form.get("gate_kind") or "").strip().lower()

        if gate == "employee":
            return redirect(url_for("portal_email_otp_send"), code=307)

        if gate == "manager":
            email = (request.form.get("email") or "").strip().lower()
            password = (request.form.get("password") or "")
            if not email or not password:
                flash("Email and password are required.", "error")
                return redirect(url_for("login_page"))
            conn = get_db(app)
            row = conn.execute(
                "SELECT * FROM managers WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
            if not row:
                conn.close()
                pw_configured = _manager_password_configured(app)
                if pw_configured and _check_manager_password(app, password):
                    session["manager"] = True
                    session["manager_role"] = "manager"
                    session.pop("my_employee_name", None)
                    flash("Signed in.", "success")
                    return redirect(url_for("dashboard"))
                flash("Invalid email or password.", "error")
                return redirect(url_for("login_page"))
            if not _verify_manager_password(str(row["password_hash"]), password):
                conn.close()
                flash("Invalid email or password.", "error")
                return redirect(url_for("login_page"))
            team_name = str(row["team_name"] or "Default").strip()
            conn.close()
            session["manager"] = True
            session["manager_role"] = "manager"
            session["manager_user_email"] = email
            session.pop("my_employee_name", None)
            conn2 = get_db(app)
            trow = conn2.execute("SELECT id FROM teams WHERE name = ? COLLATE NOCASE", (team_name,)).fetchone()
            if trow:
                session["active_team_id"] = int(trow["id"])
            conn2.close()
            flash("Signed in.", "success")
            return redirect(url_for("dashboard"))

        flash("Invalid request.", "error")
        return redirect(url_for("login_page"))

    @app.route("/register", methods=["GET", "POST"])
    def register_page():
        if request.method == "GET":
            if _manager_logged_in():
                return redirect(url_for("dashboard"))
            return render_template("register.html", hide_nav=True)

        email = (request.form.get("email") or "").strip().lower()
        password = (request.form.get("password") or "")
        team_name = (request.form.get("team_name") or "").strip()
        if not team_name and email:
            team_name = f"{email.split('@')[0].capitalize()}'s Team"
        if not email or not password:
            flash("Email and password are required.", "error")
            return redirect(url_for("register_page"))
        if len(password) < 4:
            flash("Password must be at least 4 characters.", "error")
            return redirect(url_for("register_page"))
        if len(team_name) > 200:
            team_name = team_name[:200]
        conn = get_db(app)
        existing = conn.execute("SELECT 1 FROM managers WHERE email = ? COLLATE NOCASE", (email,)).fetchone()
        if existing:
            conn.close()
            flash("An account with that email already exists.", "error")
            return redirect(url_for("register_page"))
        pw_hash = _hash_manager_password(password)
        ts = _utc_stamp()
        conn.execute(
            "INSERT INTO managers (email, password_hash, team_name, created_at) VALUES (?, ?, ?, ?)",
            (email, pw_hash, team_name, ts),
        )
        conn.execute("INSERT OR IGNORE INTO teams (name, created_at, owner_email) VALUES (?, ?, ?)", (team_name, ts, email))
        conn.commit()
        conn.close()
        session["manager"] = True
        session["manager_role"] = "manager"
        session["manager_user_email"] = email
        session.pop("my_employee_name", None)
        conn2 = get_db(app)
        trow = conn2.execute("SELECT id FROM teams WHERE name = ? COLLATE NOCASE", (team_name,)).fetchone()
        if trow:
            session["active_team_id"] = int(trow["id"])
        conn2.close()
        flash("Account created. Set up your team roster below.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/my-requests", methods=["GET", "POST"])
    def my_requests():
        pu = _portal_session()
        if not pu:
            flash("Sign in to view your leave requests.", "error")
            return redirect(url_for("home"))
        if request.method == "POST":
            flash("You can only view your own requests.", "error")
            return redirect(url_for("my_requests"))

        name = pu["roster_name"]
        rows: list = []
        if name:
            conn = get_db(app)
            rows = conn.execute(
                """
                SELECT * FROM leave_requests
                WHERE employee_name = ?
                ORDER BY created_at DESC
                LIMIT 100
                """,
                (name,),
            ).fetchall()
            conn.close()
        reason_labels = dict(LEAVE_REASONS)
        duration_labels = dict(DURATION_CHOICES)
        return render_template(
            "my_requests.html",
            rows=rows,
            viewer=name,
            reason_labels=reason_labels,
            duration_labels=duration_labels,
            form={},
            portal_employee_view=True,
        )

    @app.route("/leave", methods=["GET", "POST"])
    def leave_apply():
        if _portal_session():
            return redirect(url_for("portal_leave_apply"))
        if not _manager_logged_in():
            flash("Sign in to apply for leave.", "error")
            return redirect(url_for("home"))
        today_iso = date.today().isoformat()
        if request.method == "POST":
            raw_name = (request.form.get("employee_name") or "").strip()
            canonical = resolve_employee_name(raw_name, roster=get_union_roster_for_app(app))
            reason = (request.form.get("reason") or "").strip()
            start_date = (request.form.get("start_date") or "").strip()
            end_date = (request.form.get("end_date") or "").strip()
            day_part = (request.form.get("day_part") or "full").strip()

            errors: list[str] = []
            if not canonical:
                errors.append("Name must match a team roster entry. Use the suggestions while typing.")
            if reason not in {c[0] for c in LEAVE_REASONS}:
                errors.append("Select a valid leave type.")
            if not start_date:
                errors.append("First day is required.")
            if not end_date:
                errors.append("Last day is required.")

            duration_type = "full"
            if not errors:
                try:
                    sd = date.fromisoformat(start_date)
                    ed = date.fromisoformat(end_date)
                except ValueError:
                    errors.append("Invalid date format.")
                else:
                    if ed < sd:
                        errors.append("Last day must be on or after the first day.")
                    elif ed > sd:
                        duration_type = "multi"
                    else:
                        if day_part not in ("full", "half_am", "half_pm"):
                            errors.append("Select how much of that day is off.")
                        else:
                            duration_type = day_part

            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template(
                    "leave.html",
                    reasons=LEAVE_REASONS,
                    day_parts=DAY_PART_CHOICES,
                    form=request.form,
                    default_start=today_iso,
                    default_end=today_iso,
                )

            created = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            ip = client_ip()
            conn = get_db(app)
            conn.execute(
                """
                INSERT INTO leave_requests
                (employee_name, reason, description, start_date, end_date, duration_type, status, created_at, submitted_ip)
                VALUES (?, ?, '', ?, ?, ?, 'pending', ?, ?)
                """,
                (canonical, reason, start_date, end_date, duration_type, created, ip),
            )
            conn.commit()
            conn.close()
            session["my_employee_name"] = canonical
            flash("Request submitted.", "success")
            return redirect(url_for("my_requests"))

        return render_template(
            "leave.html",
            reasons=LEAVE_REASONS,
            day_parts=DAY_PART_CHOICES,
            form={},
            default_start=today_iso,
            default_end=today_iso,
        )

    @app.route("/attendance", methods=["GET", "POST"])
    def attendance():
        return redirect(url_for("home")), 302

    @app.route("/reports/team-select", methods=["POST"])
    def reports_team_select():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("reports"))
        try:
            tid = int((request.form.get("team_id") or "").strip())
        except ValueError:
            tid = 0
        conn = get_db(app)
        manager_email = session.get("manager_user_email")
        if manager_email:
            row = conn.execute("SELECT id, name FROM teams WHERE id = ? AND (owner_email = ? OR owner_email = '')", (tid, manager_email)).fetchone()
        else:
            row = conn.execute("SELECT id, name FROM teams WHERE id = ?", (tid,)).fetchone()
        conn.close()
        if not row:
            flash("Unknown team.", "error")
            return redirect(_safe_next_path(request.form.get("next")) or url_for("reports"))
        session["active_team_id"] = int(row["id"])
        flash(f"Active team: {row['name']}.", "success")
        dest = _safe_next_path(request.form.get("next"))
        return redirect(dest or url_for("reports"))

    @app.route("/delete_team", methods=["POST"])
    def delete_team():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("dashboard"))
            
        try:
            tid = int((request.form.get("team_id") or "").strip())
        except ValueError:
            tid = 0
            
        conn = get_db(app)
        manager_email = session.get("manager_user_email")
        if manager_email:
            row = conn.execute("SELECT id, name FROM teams WHERE id = ? AND (owner_email = ? OR owner_email = '')", (tid, manager_email)).fetchone()
        else:
            row = conn.execute("SELECT id, name FROM teams WHERE id = ?", (tid,)).fetchone()
            
        if not row:
            conn.close()
            flash("Unknown team or access denied.", "error")
            return redirect(_safe_next_path(request.form.get("next")) or url_for("home"))
            
        team_name = row["name"]
        conn.execute("DELETE FROM teams WHERE id = ?", (tid,))
        
        # Basic cleanup of associated data
        conn.execute("DELETE FROM team_roster WHERE team_id = ?", (tid,))
        conn.execute("DELETE FROM scrum_sprint WHERE team_id = ?", (tid,))
        conn.commit()
        conn.close()
        
        if session.get("active_team_id") == tid:
            session.pop("active_team_id", None)
            
        flash(f"Team '{team_name}' has been permanently deleted.", "success")
        return redirect(_safe_next_path(request.form.get("next")) or url_for("home"))


    @app.route("/reports/roster-upload", methods=["POST"])
    def reports_roster_upload():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        if not _csrf_form_ok():
            flash("Invalid security token. Try again.", "error")
            return redirect(url_for("reports"))
        f = request.files.get("roster_csv")
        if not f or not f.filename:
            flash("Choose a roster file (.xlsx or CSV).", "error")
            return redirect(url_for("reports"))
        raw = f.read()
        if len(raw) > 2_000_000:
            flash("File too large (max 2 MB).", "error")
            return redirect(url_for("reports"))
        fn = (f.filename or "").lower()
        try:
            if fn.endswith(".xlsx") or (len(raw) >= 4 and raw[:4] == b"PK\x03\x04"):
                n_teams, n_rows, warns = ingest_team_roster_xlsx(app, raw)
            else:
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    try:
                        text = raw.decode("latin-1")
                    except Exception:  # noqa: BLE001
                        flash("CSV could not be decoded (try UTF-8).", "error")
                        return redirect(url_for("reports"))
                n_teams, n_rows, warns = ingest_team_roster_csv(app, text)
        except ValueError as ex:
            flash(str(ex), "error")
            return redirect(url_for("reports"))
        for w in warns:
            flash(w, "error")
        flash(f"Roster updated: {n_teams} team(s), {n_rows} member row(s).", "success")
        return redirect(url_for("reports"))

    @app.route("/reports/roster-template.xlsx")
    def reports_roster_template_xlsx():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Roster Template"
        ws.append(["Team Name", "Name", "EMAIL"])
        buf = io.BytesIO()
        wb.save(buf)
        body = buf.getvalue()
        return Response(
            body,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="roster_template.xlsx"'},
        )

    @app.route("/reports/roster-export.xlsx")
    def reports_roster_export_xlsx():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))
        from openpyxl import Workbook

        conn = get_db(app)
        export_pairs = build_team_roster_export_rows(conn)
        conn.close()
        wb = Workbook()
        ws = wb.active
        ws.append(["TeamName", "EmployeeName"])
        for team_name, emp_name in export_pairs:
            ws.append([team_name, emp_name])
        buf = io.BytesIO()
        wb.save(buf)
        body = buf.getvalue()
        fname = "team_roster_mapping.xlsx"
        return Response(
            body,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.route("/reports/roster-export.csv")
    def reports_roster_export_csv_legacy():
        """Old bookmark: roster export is now Excel (.xlsx)."""
        return redirect(url_for("reports_roster_export_xlsx"), code=301)

    @app.route("/reports")
    def reports():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))

        start = (request.args.get("start") or "").strip()
        end = (request.args.get("end") or "").strip()
        if not start:
            start = date.today().replace(day=1).isoformat()
        if not end:
            end = date.today().isoformat()

        roster = g.manager_roster
        ph = ",".join("?" * len(roster))
        conn = get_db(app)
        leaves = conn.execute(
            f"""
            SELECT * FROM leave_requests
            WHERE start_date <= ? AND end_date >= ? AND employee_name IN ({ph})
            ORDER BY start_date ASC, employee_name ASC
            """,
            (end, start, *roster),
        ).fetchall()

        attendance_rows = conn.execute(
            f"""
            SELECT * FROM attendance
            WHERE work_date >= ? AND work_date <= ? AND employee_name IN ({ph})
            ORDER BY work_date DESC, employee_name ASC
            """,
            (start, end, *roster),
        ).fetchall()
        conn.close()

        reason_labels = dict(LEAVE_REASONS)
        duration_labels = dict(DURATION_CHOICES)
        status_labels = dict(ATTENDANCE_STATUS)

        return render_template(
            "reports.html",
            leaves=leaves,
            attendance_rows=attendance_rows,
            start=start,
            end=end,
            reason_labels=reason_labels,
            duration_labels=duration_labels,
            status_labels=status_labels,
        )

    @app.route("/worksheet/nokia-audit", methods=["GET", "POST"])
    @app.route("/reports/nokia-audit", methods=["GET", "POST"])
    def nokia_audit():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))

        today = date.today()
        try:
            year = int(request.values.get("year") or today.year)
            month = int(request.values.get("month") or today.month)
        except ValueError:
            year, month = today.year, today.month
        year = max(2000, min(2100, year))
        month = max(1, min(12, month))

        preview_url: str | None = None
        worksheet_leave_rows: list[dict] = []
        ocr_used = False
        csv_used = False
        color_sampling_used = False
        defaults: list[dict] = []
        nokia_only: list[dict] = []
        unmatched: list[tuple[str, str]] = []
        parse_error: str | None = None
        csv_paste_value = ""

        if request.method == "POST":
            if not _csrf_form_ok():
                flash("Invalid security token. Try again.", "error")
            else:
                csv_paste_value = request.form.get("nokia_csv") or ""
                nokia_csv_max = 2_000_000
                csv_text_input = (csv_paste_value or "").strip()
                cf = request.files.get("nokia_csv_file")
                if cf and cf.filename:
                    raw_csv = cf.read()
                    if len(raw_csv) > nokia_csv_max:
                        flash("CSV file too large (max 2 MB).", "error")
                    else:
                        file_txt: str | None = None
                        try:
                            file_txt = raw_csv.decode("utf-8-sig")
                        except UnicodeDecodeError:
                            try:
                                file_txt = raw_csv.decode("latin-1")
                            except Exception:  # noqa: BLE001
                                flash("CSV file could not be decoded (try UTF-8).", "error")
                        if file_txt is not None:
                            csv_text_input = file_txt.strip()

                screenshot_raw: bytes | None = None
                f = request.files.get("screenshot")
                if f and f.filename:
                    mime = (f.mimetype or "").lower()
                    if mime not in ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"):
                        flash("Screenshot must be PNG, JPEG, WebP, or GIF.", "error")
                    else:
                        raw = f.read()
                        if len(raw) > 4_000_000:
                            flash("Screenshot too large (max 4 MB).", "error")
                        else:
                            screenshot_raw = raw
                            b64 = base64.standard_b64encode(raw).decode("ascii")
                            preview_url = f"data:{mime};base64,{b64}"

                nokia_source = ""
                ocr_text_stored = ""
                ocr_err: str | None = None
                color_sampling_used = False
                testing = bool(app.config.get("TESTING"))
                test_grid = (request.form.get("nokia_ocr_text") or "").strip() if testing else ""

                if screenshot_raw:
                    ocr_text, ocr_err = _ocr_image_to_text(screenshot_raw)
                    ocr_text_stored = (ocr_text or "").strip()

                if test_grid:
                    nokia_source = test_grid
                elif csv_text_input:
                    nokia_source = csv_text_input
                    csv_used = True
                elif ocr_text_stored:
                    nokia_source = ocr_text_stored
                    ocr_used = True

                if not test_grid:
                    has_grid_input = bool(
                        (screenshot_raw)
                        or (csv_text_input and csv_text_input.strip())
                    )
                    if not has_grid_input:
                        if not testing:
                            parse_error = (
                                "Add a screenshot of the Nokia leave grid (paste, drag, or choose a file), "
                                "or paste / upload a CSV export of the same grid, then set the year and month and compare."
                            )
                        else:
                            parse_error = "Tests must send a screenshot, nokia_ocr_text, or CSV grid content."

                tess_path = _resolve_tesseract_cmd()
                color_map: dict[str, set[int]] | None = None
                color_err: str | None = None
                if screenshot_raw and tess_path and not test_grid:
                    try:
                        from leave_grid_image import extract_leave_from_colored_grid

                        color_map, color_err = extract_leave_from_colored_grid(
                            screenshot_raw, year, month, tess_path, roster=tuple(g.manager_roster)
                        )
                    except Exception as ex:  # noqa: BLE001
                        color_err = str(ex)

                if parse_error is None:
                    nokia_map = None
                    err_txt: str | None = None
                    unmatched = []
                    if nokia_source.strip():
                        nokia_map, unmatched, err_txt = parse_nokia_grid_combined(
                            nokia_source, year, month, roster=g.manager_roster
                        )

                    if (
                        nokia_map is None
                        and err_txt
                        and csv_used
                        and ocr_text_stored
                        and not test_grid
                    ):
                        nokia_map, unmatched, err_txt = parse_nokia_grid_combined(
                            ocr_text_stored, year, month, roster=g.manager_roster
                        )
                        if nokia_map is not None:
                            csv_used = False
                            ocr_used = True

                    if color_map:
                        color_sampling_used = True
                        if nokia_map is None:
                            nokia_map = {e: set(color_map.get(e, ())) for e in g.manager_roster}
                            unmatched = []
                        else:
                            for emp in g.manager_roster:
                                nokia_map[emp] |= color_map.get(emp, set())

                    if nokia_map is None:
                        parse_error = (
                            err_txt
                            or color_err
                            or ocr_err
                            or (
                                "Could not read the grid from the image. Install Tesseract OCR on the server "
                                "(and Pillow + pytesseract), or try a larger / sharper screenshot, "
                                "or use a CSV export instead."
                            )
                        )
                    elif err_txt and not color_map:
                        parse_error = err_txt
                    else:
                        app_map = app_leave_days_by_employee_month(app, year, month, roster=g.manager_roster)
                        defaults, nokia_only = compare_nokia_vs_app(
                            nokia_map, app_map, year, month, roster=g.manager_roster
                        )
                        worksheet_leave_rows = [
                            {
                                "employee": emp,
                                "days": sorted(app_map[emp]),
                                "label": ", ".join(str(d) for d in sorted(app_map[emp])) or "—",
                            }
                            for emp in g.manager_roster
                        ]
                        msg = "Comparison updated."
                        if csv_used:
                            msg += " The Nokia grid was read from pasted or uploaded CSV."
                        if color_sampling_used:
                            msg += " Colored leave cells were read from the screenshot (layout + color sampling)."
                        flash(msg, "success")

        month_label = date(year, month, 1).strftime("%B %Y")
        month_choices = [(m, calendar.month_name[m]) for m in range(1, 13)]
        if month <= 1:
            prev_year, prev_month = year - 1, 12
        else:
            prev_year, prev_month = year, month - 1
        if month >= 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1
        prev_ym = f"{prev_year:04d}-{prev_month:02d}"
        next_ym = f"{next_year:04d}-{next_month:02d}"
        return render_template(
            "nokia_audit.html",
            year=year,
            month=month,
            month_label=month_label,
            month_choices=month_choices,
            prev_year=prev_year,
            prev_month=prev_month,
            next_year=next_year,
            next_month=next_month,
            prev_ym=prev_ym,
            next_ym=next_ym,
            today_year=today.year,
            today_month=today.month,
            defaults=defaults,
            nokia_only=nokia_only,
            unmatched=unmatched,
            parse_error=parse_error,
            preview_url=preview_url,
            ocr_used=ocr_used,
            csv_used=csv_used,
            color_sampling_used=color_sampling_used,
            worksheet_leave_rows=worksheet_leave_rows,
            csv_paste_value=csv_paste_value,
        )

    @app.route("/reports/leave/<int:leave_id>/status", methods=["POST"])
    def update_leave_status(leave_id: int):
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard"))

        new_status = (request.form.get("status") or "").strip()
        if new_status not in {"pending", "approved", "rejected"}:
            flash("Invalid status.", "error")
            return redirect(url_for("reports"))

        conn = get_db(app)
        conn.execute(
            "UPDATE leave_requests SET status = ? WHERE id = ?",
            (new_status, leave_id),
        )
        conn.commit()
        conn.close()
        flash("Saved.", "success")
        if request.form.get("redirect_manager") == "1" and _manager_logged_in():
            try:
                y = int(request.form.get("mgr_year") or 0)
                m = int(request.form.get("mgr_month") or 0)
                if 1 <= m <= 12 and y > 2000:
                    return redirect(url_for("dashboard", year=y, month=m))
            except ValueError:
                pass
        rs = (request.form.get("report_start") or "").strip()
        re = (request.form.get("report_end") or "").strip()
        return redirect(url_for("reports", start=rs or None, end=re or None))

    @app.route("/reports/export-leaves.xlsx")
    def reports_leave_export_xlsx():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))

        start_raw = (request.args.get("start") or "").strip() or date.today().replace(day=1).isoformat()
        end_raw = (request.args.get("end") or "").strip() or date.today().isoformat()
        try:
            sd = date.fromisoformat(start_raw[:10])
            ed = date.fromisoformat(end_raw[:10])
        except ValueError:
            flash("Invalid date range.", "error")
            return redirect(url_for("reports"))
        if ed < sd:
            sd, ed = ed, sd

        roster = tuple(g.manager_roster)
        detail_rows, totals = _leave_tracker_day_rows_for_range(app, roster, sd, ed)

        req_units: dict[int, float] = {}
        req_date_sets: dict[int, set[str]] = {}
        for r in detail_rows:
            lid = int(r["leave_request_id"])
            req_units[lid] = req_units.get(lid, 0.0) + float(r["day_units"])
            req_date_sets.setdefault(lid, set()).add(str(r["leave_date"]))
        req_dates_csv = {k: ",".join(sorted(v)) for k, v in req_date_sets.items()}

        reason_labels = dict(LEAVE_REASONS)
        duration_labels = dict(DURATION_CHOICES)

        ph = ",".join("?" * len(roster))
        conn = get_db(app)
        if roster:
            all_leaves = conn.execute(
                f"""
                SELECT employee_name, reason, start_date, end_date, duration_type, status, created_at, submitted_ip, id
                FROM leave_requests
                WHERE start_date <= ? AND end_date >= ? AND employee_name IN ({ph})
                ORDER BY start_date ASC, employee_name ASC, id ASC
                """,
                (ed.isoformat(), sd.isoformat(), *roster),
            ).fetchall()
        else:
            all_leaves = []
        conn.close()

        from openpyxl import Workbook
        from sprint_hub_snapshot_png import sanitize_excel_sheet_title

        def request_detail_row(row: sqlite3.Row) -> list[Any]:
            lid = int(row["id"])
            reason = str(row["reason"] or "").strip()
            leave_type = str(reason_labels.get(reason, reason))
            dur_code = str(row["duration_type"] or "").strip()
            dur_label = str(duration_labels.get(dur_code, dur_code))
            try:
                ls = date.fromisoformat(str(row["start_date"])[:10])
                le = date.fromisoformat(str(row["end_date"])[:10])
            except ValueError:
                ls = le = sd
            if le < ls:
                ls, le = le, ls
            cal_full = (le - ls).days + 1
            ov = _leave_report_overlap_window(str(row["start_date"]), str(row["end_date"]), sd, ed)
            if ov:
                wdays_ov = _working_weekdays_count(ov[0], ov[1])
            else:
                wdays_ov = 0
            taken = float(round(req_units.get(lid, 0.0), 4))
            leaves_dates = req_dates_csv.get(lid, "")
            return [
                lid,
                row["employee_name"],
                leave_type,
                row["status"],
                cal_full,
                wdays_ov,
                taken,
                row["created_at"],
                str(row["start_date"])[:10],
                str(row["end_date"])[:10],
                dur_code,
                dur_label,
                leaves_dates,
                reason,
                row["submitted_ip"] if "submitted_ip" in row.keys() else "",
            ]

        wb = Workbook()
        ws0 = wb.active
        ws0.title = "Summary"
        team_nm = getattr(g, "manager_team_name", None) or ""
        ws0.append(["Team", team_nm])
        ws0.append(["From", sd.isoformat(), "To", ed.isoformat()])
        ws0.append([])
        ws0.append(
            [
                "Totals use the same rules as the leave tracker: pending and approved requests only; "
                "DSM meet per-day approve/remove applies; when several requests overlap the same day, "
                "the newest request wins; half-day (AM/PM) counts as 0.5.",
            ]
        )
        ws0.append([])
        ws0.append(["Employee", "Total leave days (tracker units in range)"])
        for emp in roster:
            ws0.append([emp, float(round(totals.get(emp, 0.0), 4))])

        ws_detail = wb.create_sheet("Request detail", 1)
        hdr = [
            "leave_request_id",
            "employee_name",
            "leave_type",
            "leave_status",
            "no_of_calendar_days_full_request",
            "working_days_in_report_overlap",
            "days_leaves_taken_in_range_tracker",
            "dates_applied_submitted",
            "date_from",
            "date_to",
            "duration_type",
            "duration_label",
            "leaves_taken_dates_in_range",
            "reason_code",
            "submitted_ip",
        ]
        ws_detail.append(hdr)
        for row in all_leaves:
            ws_detail.append(request_detail_row(row))

        ws_leave = wb.create_sheet("Leave dates", 2)
        ws_leave.append(
            [
                "Employee",
                "Leave date",
                "Day units",
                "Reason code",
                "Reason",
                "Duration code",
                "Duration",
                "Status",
                "Leave request id",
                "Request start",
                "Request end",
            ]
        )
        for r in detail_rows:
            ws_leave.append(
                [
                    r["employee_name"],
                    r["leave_date"],
                    r["day_units"],
                    r["reason"],
                    r["reason_label"],
                    r["duration_type"],
                    r["duration_label"],
                    r["status"],
                    r["leave_request_id"],
                    r["request_start"],
                    r["request_end"],
                ]
            )

        used_person_tabs: set[str] = set()
        for emp in roster:
            tab = sanitize_excel_sheet_title(emp)
            base_tab = tab
            n = 2
            while tab in used_person_tabs:
                tab = sanitize_excel_sheet_title(f"{base_tab[:22]}_{n}")
                n += 1
            used_person_tabs.add(tab)
            wsp = wb.create_sheet(tab)
            wsp.append(["Team", team_nm])
            wsp.append(["Employee", emp])
            wsp.append(["Report from", sd.isoformat(), "Report to", ed.isoformat()])
            wsp.append([])
            tot_u = float(round(totals.get(emp, 0.0), 4))
            emp_reqs = [row for row in all_leaves if str(row["employee_name"]) == emp]
            wsp.append(["Summary — total leave days in range (tracker units)", tot_u])
            wsp.append(["Summary — requests overlapping date range (any status)", len(emp_reqs)])
            wsp.append([])
            wsp.append(hdr)
            for row in emp_reqs:
                wsp.append(request_detail_row(row))

        buf = io.BytesIO()
        wb.save(buf)
        body = buf.getvalue()
        fname = f"leave_report_{sd.isoformat()}_to_{ed.isoformat()}.xlsx"
        return Response(
            body,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.route("/reports/export.csv")
    def export_csv():
        if not _manager_logged_in():
            flash("Manager sign-in required.", "error")
            return redirect(url_for("dashboard", next=request.path))

        start = (request.args.get("start") or date.today().replace(day=1).isoformat()).strip()
        end = (request.args.get("end") or date.today().isoformat()).strip()

        roster = g.manager_roster
        ph = ",".join("?" * len(roster))
        conn = get_db(app)
        leaves = conn.execute(
            f"""
            SELECT employee_name, reason, start_date, end_date, duration_type, status, created_at, submitted_ip
            FROM leave_requests
            WHERE start_date <= ? AND end_date >= ? AND employee_name IN ({ph})
            ORDER BY start_date ASC
            """,
            (end, start, *roster),
        ).fetchall()
        conn.close()

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            [
                "employee_name",
                "reason",
                "start_date",
                "end_date",
                "duration_type",
                "status",
                "created_at",
                "submitted_ip",
            ]
        )
        for row in leaves:
            w.writerow(
                [
                    row["employee_name"],
                    row["reason"],
                    row["start_date"],
                    row["end_date"],
                    row["duration_type"],
                    row["status"],
                    row["created_at"],
                    row["submitted_ip"] if "submitted_ip" in row.keys() else "",
                ]
            )

        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=leave_report_{start}_to_{end}.csv"
            },
        )

    with app.app_context():
        init_db(app)

    return app


if __name__ == "__main__":
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "1").lower() in ("1", "true", "yes")
    create_app().run(host=host, port=port, debug=debug)

"""
Heuristic extraction of leave days from Nokia-style *colored* calendar screenshots.

Tesseract plain text often misses solid red / pink cells with no glyphs. This module uses
``image_to_data`` word boxes to find the date header row and body rows, then samples RGB
pixels at each day cell to detect leave-like fills (red / pink) vs white / blue weekend bands.
"""

from __future__ import annotations

import io
import statistics
from calendar import monthrange
from collections import defaultdict
from typing import Any, Sequence

import numpy as np
from PIL import Image


def _cell_is_leave_color(rgb: np.ndarray) -> bool:
    """True if pixel looks like red / pink leave fill (not white/grey/blue header)."""
    r, g, b = float(rgb[0]), float(rgb[1]), float(rgb[2])
    mx = max(r, g, b)
    if mx < 35:
        return False
    # Strong red / coral leave
    if r > 95 and r > g + 18 and r > b + 18 and (r + g + b) / 3 < 220:
        return True
    # Light pink / salmon
    if r > 150 and g > 90 and b > 90 and r >= g - 15 and r >= b - 15 and (r - min(g, b)) > 25:
        return True
    return False


def _cell_is_weekend_blue(rgb: np.ndarray) -> bool:
    """Weekend columns in many Nokia themes are saturated blue."""
    r, g, b = float(rgb[0]), float(rgb[1]), float(rgb[2])
    return b > r + 35 and b > g + 15 and b > 90


def _conf_int(raw: Any) -> int:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return -1


def extract_leave_from_colored_grid(
    raw: bytes,
    year: int,
    month: int,
    tess_cmd: str,
    roster: Sequence[str] | None = None,
) -> tuple[dict[str, set[int]] | None, str | None]:
    """
    Returns (leave_by_roster_name, error). On failure returns (None, message).
    Lazy-imports app roster helpers to avoid import cycles when app loads this module lazily.
    """
    from app import EMPLOYEES, _resolve_nokia_row_name

    roster_t = tuple(roster) if roster is not None else EMPLOYEES

    try:
        import pytesseract
    except ImportError:
        return None, "pytesseract is not installed."

    pytesseract.pytesseract.tesseract_cmd = tess_cmd

    try:
        pil = Image.open(io.BytesIO(raw)).convert("RGB")
    except OSError as e:
        return None, f"Could not read image: {e}"

    arr = np.asarray(pil, dtype=np.uint8)
    h, w = arr.shape[:2]
    if h < 80 or w < 200:
        return None, "Image too small to analyze."

    # Upscale narrow screenshots so cell samples hit the right pixels
    scale = max(1.0, 1400.0 / float(w))
    if scale > 1.01:
        nw, nh = int(w * scale), int(h * scale)
        pil = pil.resize((nw, nh), Image.Resampling.LANCZOS)
        arr = np.asarray(pil, dtype=np.uint8)
        h, w = arr.shape[:2]

    data = pytesseract.image_to_data(pil, output_type=pytesseract.Output.DICT, config="--oem 3 --psm 6")
    n = len(data.get("text", ()))
    by_line: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)

    for i in range(n):
        txt = (data["text"][i] or "").strip()
        if not txt:
            continue
        if _conf_int(data["conf"][i]) < 15:
            continue
        key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
        left, top, ww, ht = int(data["left"][i]), int(data["top"][i]), int(data["width"][i]), int(data["height"][i])
        by_line[key].append(
            {
                "text": txt,
                "cx": left + ww // 2,
                "cy": top + ht // 2,
                "top": top,
                "h": ht,
            }
        )

    def dom_from_token(t: str) -> int | None:
        if not t.isdigit():
            return None
        v = int(t)
        if 1 <= v <= 31:
            return v
        return None

    # Pick header line: most tokens that are day numbers 1–31
    best_key: tuple[int, int, int] | None = None
    best_score = 0
    for key, words in by_line.items():
        score = sum(1 for w in words if dom_from_token(w["text"]) is not None)
        if score > best_score and score >= 10:
            best_score = score
            best_key = key

    if best_key is None:
        return None, "Could not locate a date header row (1…31) in the screenshot."

    header_words = sorted(by_line[best_key], key=lambda z: z["cx"])
    dom_to_x: dict[int, float] = {}
    for hw in header_words:
        d = dom_from_token(hw["text"])
        if d is not None and d not in dom_to_x:
            dom_to_x[d] = float(hw["cx"])

    if len(dom_to_x) < 10:
        return None, "Too few day columns detected in the header."

    _, last_day = monthrange(year, month)
    # Linear x(dom): fit from observed day centers
    pairs = sorted(dom_to_x.items())
    xs = [p[1] for p in pairs]
    ds = [p[0] for p in pairs]
    if len(ds) >= 2:
        gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
        cell_w = max(8.0, statistics.median(gaps))
    else:
        cell_w = max(8.0, (max(xs) - min(xs)) / max(1, (max(ds) - min(ds))))

    def x_for_dom(dom: int) -> float:
        # nearest anchor + delta
        nearest = min(dom_to_x.keys(), key=lambda d: abs(d - dom))
        return dom_to_x[nearest] + (dom - nearest) * cell_w

    header_y = statistics.mean([hw["cy"] for hw in header_words])

    # Second header row (weekday letters) — sample slightly below number row for weekend blue
    weekday_probe_y = min(h - 2, int(header_y + statistics.mean([hw["h"] for hw in header_words]) * 1.2))

    weekend_dom: set[int] = set()
    for dom in range(1, last_day + 1):
        xi = int(max(1, min(w - 2, x_for_dom(dom))))
        yi = int(max(1, min(h - 2, weekday_probe_y)))
        if _cell_is_weekend_blue(arr[yi, xi, :]):
            weekend_dom.add(dom)

    # Body rows: lines below header with at least one non-digit token (name parts)
    header_top = min(hw["top"] for hw in header_words)
    body_lines: list[tuple[float, str, list[dict[str, Any]]]] = []
    for key, words in by_line.items():
        if key == best_key:
            continue
        if not words:
            continue
        top = min(x["top"] for x in words)
        if top <= header_top + 5:
            continue
        texts = [x["text"] for x in sorted(words, key=lambda z: z["cx"])]
        digitish = sum(1 for t in texts if dom_from_token(t) is not None)
        if digitish >= max(4, len(texts) - 1):
            continue
        line_text = " ".join(texts)
        if len(line_text) < 4:
            continue
        yc = float(statistics.mean([x["cy"] for x in words]))
        body_lines.append((yc, line_text, words))

    body_lines.sort(key=lambda t: t[0])

    out: dict[str, set[int]] = {e: set() for e in roster_t}
    matched_any = False

    for _yc, line_text, words in body_lines:
        resolved = _resolve_nokia_row_name(line_text, roster=roster_t)
        if not resolved:
            parts = line_text.split()
            if parts and parts[0].isdigit() and len(parts) > 1:
                resolved = _resolve_nokia_row_name(" ".join(parts[1:]), roster=roster_t)
        if not resolved:
            continue
        matched_any = True
        y_center = int(max(1, min(h - 2, round(_yc))))
        for dom in range(1, last_day + 1):
            if dom in weekend_dom:
                continue
            xi = int(max(1, min(w - 2, round(x_for_dom(dom)))))
            y0, y1 = max(0, y_center - 2), min(h, y_center + 3)
            x0, x1 = max(0, xi - 2), min(w, xi + 3)
            patch = arr[y0:y1, x0:x1, :].reshape(-1, 3).astype(np.float32)
            rgb = patch.mean(axis=0)
            if _cell_is_leave_color(rgb):
                out[resolved].add(dom)

    if not matched_any:
        return None, "Could not match roster names to any table row for color sampling."

    return out, None
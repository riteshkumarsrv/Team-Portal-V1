#!/usr/bin/env python3
"""Sync February 2026 *planned* leave (snapshot: purple N) and *half-day* (L1) from Team 1 worksheet.

Maps into the leave DB as Nokia audit rows are **not** touched:
  - N  → pending ``reason=pl``, ``duration_type=full``  (grid shows **PL** — same as “planned” P in your legend)
  - L1 → pending ``reason=pl``, ``duration_type=half_am`` (grid shows **PL½** — lowercase “p” / half-day)

Rows with ``description`` containing ``[nokia-audit-approved]`` (green **A**) are never updated or removed.
Inserts are skipped on any calendar day where an overlapping row already carries that marker.

Only employees listed in ``SNAPSHOT`` are processed (others unchanged).

Usage (PowerShell)::

  $env:TEAM_TRACKER_DB_PATH = "C:\\path\\to\\team_tracker.db"   # optional
  python scripts/sync_feb2026_planned_worksheet_snapshot.py
  python scripts/sync_feb2026_planned_worksheet_snapshot.py --dry-run

The snapshot is keyed to **roster names** in ``app.EMPLOYEES`` (February 2026, Team 1 grid).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from calendar import monthrange
from datetime import date, datetime, timezone
from pathlib import Path

# Must match app.NOKIA_AUDIT_LEAVE_DESCRIPTION_MARKER (do not change independently).
NOKIA_MARKER = "[nokia-audit-approved]"

YEAR = 2026
MONTH = 2
_, LAST_D = monthrange(YEAR, MONTH)
FEB_LO = date(YEAR, MONTH, 1).isoformat()
FEB_HI = date(YEAR, MONTH, LAST_D).isoformat()

# Roster keys = canonical ``EMPLOYEES`` names. Values: full-day planned (N), half-day (L1).
SNAPSHOT: dict[str, dict[str, set[int]]] = {
    "Jancy Mariam Jose": {"P": {23, 24, 25, 26}, "p": set()},
    "Sasikumar Sampath": {"P": {20}, "p": set()},
    "Maddala Satyasai": {"P": set(), "p": {23}},
    "Bharath G Krishna": {"P": {16, 20}, "p": {23}},
    "Dabbiru Siva Seshasai": {"P": {12, 16, 19, 23, 25}, "p": set()},
    "Shaishta Anjum": {"P": {16}, "p": set()},
    "Rashmi K Nazare": {"P": {6, 9, 13, 18}, "p": set()},
    "Varshitha S": {"P": {16, 19}, "p": {4, 9}},
    # Listed on snapshot with no N / L1 for February (still purge stale Feb-only pending PL below):
    "Sangjukta Giri": {"P": set(), "p": set()},
    "Sumit Patra": {"P": set(), "p": set()},
    "Anas P": {"P": set(), "p": set()},
    "Jayachandra Reddy Mure": {"P": set(), "p": set()},
    "Gumparlapati Penchala Sasi Kumar": {"P": set(), "p": set()},
    "Shruthi K": {"P": set(), "p": set()},
    "Zaiba Nousheen Khanum": {"P": set(), "p": set()},
}


def _has_nokia_overlap(conn: sqlite3.Connection, employee_name: str, day_iso: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM leave_requests
        WHERE employee_name = ?
          AND start_date <= ? AND end_date >= ?
          AND status IN ('pending', 'approved')
          AND instr(COALESCE(description, ''), ?) > 0
        LIMIT 1
        """,
        (employee_name, day_iso, day_iso, NOKIA_MARKER),
    ).fetchone()
    return row is not None


def _purge_feb_pending_pl_no_nokia(conn: sqlite3.Connection, employee_name: str) -> list[int]:
    """Remove pending PL rows fully inside February with no Nokia marker; returns deleted ids."""
    rows = conn.execute(
        """
        SELECT id FROM leave_requests
        WHERE employee_name = ?
          AND status = 'pending'
          AND reason = 'pl'
          AND start_date >= ? AND end_date <= ?
          AND instr(COALESCE(description, ''), ?) = 0
        """,
        (employee_name, FEB_LO, FEB_HI, NOKIA_MARKER),
    ).fetchall()
    ids = [int(r[0]) for r in rows]
    for lid in ids:
        conn.execute("DELETE FROM meet_leave_day WHERE leave_id = ?", (lid,))
        conn.execute("DELETE FROM leave_requests WHERE id = ?", (lid,))
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print actions only; do not commit.")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    default_db = root / "data" / "team_tracker.db"
    db_path = Path(os.environ.get("TEAM_TRACKER_DB_PATH", str(default_db)))
    if not db_path.is_file():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    created = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    ip = "snapshot-script"
    desc = "Feb 2026 Team 1 worksheet snapshot — pending PL / PL½ (N / L1); Nokia A untouched"

    conn = sqlite3.connect(str(db_path))
    try:
        total_del = 0
        total_ins = 0
        skipped_nokia = 0

        for emp, blocks in SNAPSHOT.items():
            p_days = set(blocks.get("P") or ())
            half_days = set(blocks.get("p") or ())
            if p_days & half_days:
                print(f"Warning: {emp} has same day in P and p: {p_days & half_days}", file=sys.stderr)

            if args.dry_run:
                n_del = conn.execute(
                    """
                    SELECT COUNT(*) FROM leave_requests
                    WHERE employee_name = ?
                      AND status = 'pending'
                      AND reason = 'pl'
                      AND start_date >= ? AND end_date <= ?
                      AND instr(COALESCE(description, ''), ?) = 0
                    """,
                    (emp, FEB_LO, FEB_HI, NOKIA_MARKER),
                ).fetchone()[0]
                total_del += int(n_del)
            else:
                removed = _purge_feb_pending_pl_no_nokia(conn, emp)
                total_del += len(removed)

            for dom in sorted(p_days - half_days):
                d_iso = date(YEAR, MONTH, dom).isoformat()
                if _has_nokia_overlap(conn, emp, d_iso):
                    skipped_nokia += 1
                    continue
                if args.dry_run:
                    print(f"would insert PL full {emp} {d_iso}")
                    total_ins += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO leave_requests
                    (employee_name, reason, description, start_date, end_date, duration_type, status, created_at, submitted_ip)
                    VALUES (?, 'pl', ?, ?, ?, 'full', 'pending', ?, ?)
                    """,
                    (emp, desc, d_iso, d_iso, created, ip),
                )
                total_ins += 1

            for dom in sorted(half_days):
                d_iso = date(YEAR, MONTH, dom).isoformat()
                if _has_nokia_overlap(conn, emp, d_iso):
                    skipped_nokia += 1
                    continue
                if args.dry_run:
                    print(f"would insert PL½ {emp} {d_iso}")
                    total_ins += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO leave_requests
                    (employee_name, reason, description, start_date, end_date, duration_type, status, created_at, submitted_ip)
                    VALUES (?, 'pl', ?, ?, ?, 'half_am', 'pending', ?, ?)
                    """,
                    (emp, desc, d_iso, d_iso, created, ip),
                )
                total_ins += 1

        if args.dry_run:
            print(
                f"Dry-run: would delete {total_del} Feb-only pending PL rows (no Nokia); "
                f"would insert {total_ins}; skipped {skipped_nokia} Nokia-marked day(s)."
            )
        else:
            conn.commit()
            print(f"Deleted {total_del} prior Feb-only pending PL rows (no Nokia marker).")
            print(f"Inserted {total_ins} rows. Skipped {skipped_nokia} day(s) with existing Nokia-approved marker.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Remove one row from the SQLite `managers` table by email (case-insensitive).

Does not delete teams, leave data, or scrum data — only the manager login row.

Usage:
  set TEAM_TRACKER_DB_PATH=path\\to\\team_tracker.db   (optional; default: ./data/team_tracker.db next to app.py)
  python scripts/delete_manager_account.py user@example.com
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/delete_manager_account.py <email>", file=sys.stderr)
        return 2
    email = sys.argv[1].strip()
    if "@" not in email:
        print("Expected an email address.", file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parent.parent
    default_db = root / "data" / "team_tracker.db"
    db_path = Path(os.environ.get("TEAM_TRACKER_DB_PATH", str(default_db)))
    if not db_path.is_file():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("DELETE FROM managers WHERE email = ? COLLATE NOCASE", (email,))
        conn.commit()
        n = cur.rowcount
    finally:
        conn.close()

    print(f"Deleted {n} manager row(s) for {email!r}.")
    return 0 if n else 0


if __name__ == "__main__":
    raise SystemExit(main())

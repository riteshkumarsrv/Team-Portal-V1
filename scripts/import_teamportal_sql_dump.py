"""Regenerate data/team_tracker.db from data/teamportalfinal_full_dump.sql (Oracle unistr → SQLite)."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

# SCRUM_vF project root (parent of scripts/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DUMP = PROJECT_ROOT / "data" / "teamportalfinal_full_dump.sql"
FIXED = PROJECT_ROOT / "data" / "teamportalfinal_full_dump_sqlite.sql"
DB = PROJECT_ROOT / "data" / "team_tracker.db"


def convert_unistr(sql: str) -> tuple[str, int]:
    pat = re.compile(r"unistr\(\s*'((?:''|[^'])*)'\s*\)", re.IGNORECASE)

    def repl(m: re.Match[str]) -> str:
        inner = m.group(1).replace("''", "'")

        def uhex(mm: re.Match[str]) -> str:
            return chr(int(mm.group(1), 16))

        inner2 = re.sub(r"\\u([0-9a-fA-F]{4})", uhex, inner, flags=re.I)
        inner2 = re.sub(r"\\U([0-9a-fA-F]{8})", lambda mm: chr(int(mm.group(1), 16)), inner2, flags=re.I)
        esc = inner2.replace("'", "''")
        return "'" + esc + "'"

    out, n = pat.subn(repl, sql)
    return out, n


def main() -> None:
    if not DUMP.is_file():
        raise SystemExit(f"Missing dump file: {DUMP}")
    sql = DUMP.read_text(encoding="utf-8", errors="replace")
    sql2, n = convert_unistr(sql)
    print(f"unistr() replacements: {n}")
    FIXED.write_text(sql2, encoding="utf-8")
    print(f"Wrote fixed SQL: {FIXED}")
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(str(DB))
    try:
        con.executescript(sql2)
        con.commit()
    finally:
        con.close()
    print(f"SQLite database: {DB} ({DB.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

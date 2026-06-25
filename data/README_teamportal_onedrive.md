# Team portal SQL sources (see `scripts/import_teamportal_sql_dump.py`)

SQL inputs and recovery copies are kept under **`Not Relevant Files/sql_dumps/`** (not in `data/`), so `data/` stays for the live DB and runtime assets only.

- **`teamportalfinal_full_dump.sql`** — Oracle dump input used to build **`data/team_tracker.db`**.
- **`teamportalfinal_full_dump_sqlite.sql`** — generated after `unistr()` → SQLite string fixes.

Regenerate the live DB:

```powershell
python scripts\import_teamportal_sql_dump.py
```

**Note:** Dumps include `managers` password hashes — keep dumps and `data/` private; `*.db` stays gitignored.


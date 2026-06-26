# Not Relevant Files

This folder holds **local / legacy artifacts** that are not needed to **run or build** the Flask app from the project root (`main.py`, `app.py`, `templates/`, `static/`, `data/team_tracker.db`, etc.).

| Subfolder | Contents |
|-----------|----------|
| `sql_dumps/` | TeamPortal Oracle→SQLite migration inputs (`teamportalfinal_*.sql`) and older `.bak` / `.recoverbak` copies. Used by `python scripts/import_teamportal_sql_dump.py`. |
| `sqlite_snapshots/` | Old copies of `team_tracker.db` from `data/` (e.g. `*.bak_*`, `*.recoverbak_*`). |
| `temp_root_databases/` | Scratch SQLite files from import tests (`_tmp_import_test*.db`). |
| `adhoc_scripts/` | One-off scripts (e.g. `_tmp_check_leave_dates.py`). |
| `sample_spreadsheets/` | Example leave worksheets; `scripts/generate_april_2026_leave_worksheet.py` writes its output here. |
| `legacy_static_demo/` | `standalone.html` (legacy static demo, no Flask). |
| `legacy_duplicate_tree_SCRUM_vF_1/` | Former nested copy **`SCRUM_vF.1/`** of the repo (older tree + its own `.git`). Kept for reference only; **do not run the app from this path**. |
| `temp_workspace/` | Scratch logs and temp spreadsheets from local dev (`flask_run.log`, `_tmp_nav_*.xlsx`, etc.). |

The live app still writes scheduled DB copies to **`Backup/`** at the **project root** (next to `main.py`), not under this folder.

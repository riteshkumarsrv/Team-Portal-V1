# Latest Database

This folder contains a **snapshot of the live SQLite database** shipped with this release for GitHub / fresh clones.

| File | Purpose |
|------|---------|
| `team_tracker.db` | Full app data: teams, roster, leave tracker, scrum sprints, portal users, managers (hashed passwords), etc. |

## Restore on a new machine

1. Clone the repo and install dependencies (`python -m pip install -r requirements.txt`).
2. Copy this file into the runtime location:

   ```powershell
   copy "Latest Database\team_tracker.db" "data\team_tracker.db"
   ```

   Or set in `.env`:

   ```
   TEAM_TRACKER_DB_PATH=./Latest Database/team_tracker.db
   ```

3. Copy `.env.example` to `.env`, set `FLASK_SECRET_KEY` and manager/portal secrets.
4. Run `python main.py` and open http://127.0.0.1:5000.

**Security:** This file may include password hashes and employee leave data. Treat it as **confidential**; rotate manager passwords after sharing or use a sanitized DB for public demos.

**Snapshot date:** taken at package time from `data/team_tracker.db` (see file modified time).

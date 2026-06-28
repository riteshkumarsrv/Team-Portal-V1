"""Clear leave tracker day cells for a team roster (pending/approved leave_requests + meet_leave_day)."""
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB = os.environ.get(
    "TEAM_TRACKER_DB_PATH",
    r"c:\Users\z0017fzc\Projects\SCRUM_vF\data\team_tracker.db",
)
TEAM = sys.argv[1] if len(sys.argv) > 1 else "FRONTIER"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
tid = conn.execute("SELECT id FROM teams WHERE name = ?", (TEAM,)).fetchone()
if not tid:
    print(f"Team {TEAM!r} not found")
    sys.exit(1)
tid = tid["id"]
roster = [
    r["employee_name"]
    for r in conn.execute(
        "SELECT employee_name FROM team_roster WHERE team_id=? ORDER BY sort_order",
        (tid,),
    )
]
if not roster:
    print(f"No roster for team {TEAM}")
    sys.exit(1)

placeholders = ",".join("?" * len(roster))
leave_ids = [
    r["id"]
    for r in conn.execute(
        f"""
        SELECT id FROM leave_requests
        WHERE employee_name IN ({placeholders})
          AND status IN ('pending', 'approved')
        """,
        roster,
    )
]
print(f"Team {TEAM} ({len(roster)} members): clearing {len(leave_ids)} leave rows")

if leave_ids:
    id_ph = ",".join("?" * len(leave_ids))
    cur_mld = conn.execute(
        f"DELETE FROM meet_leave_day WHERE leave_id IN ({id_ph})",
        leave_ids,
    )
    cur_lr = conn.execute(
        f"""
        DELETE FROM leave_requests
        WHERE id IN ({id_ph})
        """,
        leave_ids,
    )
    conn.commit()
    print(f"Deleted {cur_mld.rowcount} meet_leave_day rows, {cur_lr.rowcount} leave_requests")
else:
    print("Nothing to delete")

remaining = conn.execute(
    f"""
    SELECT COUNT(*) AS n FROM leave_requests
    WHERE employee_name IN ({placeholders})
      AND status IN ('pending', 'approved')
    """,
    roster,
).fetchone()["n"]
print(f"Remaining pending/approved leaves for roster: {remaining}")
conn.close()

"""
SQLite table names and schema anchors for the team tracker.

The app uses raw SQL (not an ORM). These constants match ``init_db`` and
migration helpers in ``app.py`` so queries stay consistent when refactoring.
"""

from __future__ import annotations

from typing import Final

# Core tables from the initial schema in ``app.init_db``
T_LEAVE_REQUESTS: Final = "leave_requests"
T_ATTENDANCE: Final = "attendance"
T_MEET_ATTENDANCE: Final = "meet_attendance"
T_MEET_LEAVE_DAY: Final = "meet_leave_day"
T_TEAMS: Final = "teams"
T_TEAM_ROSTER: Final = "team_roster"
T_LEAVE_TRACKER_ELEAVES: Final = "leave_tracker_eleaves"
T_SCRUM_SPRINT: Final = "scrum_sprint"
T_SCRUM_SPRINT_ITEM: Final = "scrum_sprint_item"
T_SCRUM_DAILY_TASK: Final = "scrum_daily_task"
T_SCRUM_SPRINT_MEMBER_GOAL: Final = "scrum_sprint_member_goal"
T_SCRUM_ITEM_ACTIVITY: Final = "scrum_item_activity"
T_APP_MIGRATIONS: Final = "app_migrations"

# Paste this into PythonAnywhere Web → WSGI configuration file.
# Replace YOUR_USERNAME with your PythonAnywhere username (e.g. riteshkumarsrv).

import os
import sys

project_home = "/home/YOUR_USERNAME/Team-Portal-V1"
if project_home not in sys.path:
    sys.path.insert(0, project_home)

os.chdir(project_home)

os.environ.setdefault("TEAM_TRACKER_DB_PATH", os.path.join(project_home, "data", "team_tracker.db"))
os.environ.setdefault("TEAM_TRACKER_PRODUCTION", "1")
os.environ.setdefault("TEAM_TRACKER_AUTO_TESSERACT", "0")
os.environ.setdefault("PUBLIC_URL", "https://YOUR_USERNAME.pythonanywhere.com")

# FLASK_SECRET_KEY and MANAGER_DASHBOARD_PASSWORD are loaded from project .env by config.py

from wsgi import app as application  # noqa: E402

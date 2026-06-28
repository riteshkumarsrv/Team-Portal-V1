#!/usr/bin/env python3
"""Configure/reload Team Portal V1 on PythonAnywhere via API (optional).

Set environment variables:
  PA_USERNAME=your_pythonanywhere_username
  PA_API_TOKEN=from Account → API token tab
  PA_REGION=www   (or eu)

Then: python scripts/pythonanywhere_deploy_api.py
"""

from __future__ import annotations

import os
import sys

import requests

PROJECT = "Team-Portal-V1"
VENV_NAME = "team-portal-v1"


def main() -> int:
    username = (os.environ.get("PA_USERNAME") or "").strip()
    token = (os.environ.get("PA_API_TOKEN") or "").strip()
    region = (os.environ.get("PA_REGION") or "www").strip()
    if not username or not token:
        print("Set PA_USERNAME and PA_API_TOKEN", file=sys.stderr)
        return 1

    host = "www.pythonanywhere.com" if region == "www" else "eu.pythonanywhere.com"
    base = f"https://{host}/api/v0/user/{username}/"
    headers = {"Authorization": f"Token {token}"}

    domain = f"{username}.{host.replace('www.', '')}"
    if domain.endswith(".pythonanywhere.com.pythonanywhere.com"):
        domain = f"{username}.pythonanywhere.com"
    if region == "eu":
        domain = f"{username}.eu.pythonanywhere.com"

    project_home = f"/home/{username}/{PROJECT}"
    venv_path = f"/home/{username}/.virtualenvs/{VENV_NAME}"

    wsgi_body = f'''import os
import sys

project_home = "{project_home}"
if project_home not in sys.path:
    sys.path.insert(0, project_home)

os.chdir(project_home)

os.environ.setdefault("TEAM_TRACKER_DB_PATH", os.path.join(project_home, "data", "team_tracker.db"))
os.environ.setdefault("TEAM_TRACKER_PRODUCTION", "1")
os.environ.setdefault("TEAM_TRACKER_AUTO_TESSERACT", "0")
os.environ.setdefault("PUBLIC_URL", "https://{domain}")

from wsgi import app as application
'''

    apps = requests.get(base + "webapps/", headers=headers, timeout=60)
    apps.raise_for_status()
    existing = {a["domain_name"] for a in apps.json()}

    if domain not in existing:
        r = requests.post(
            base + "webapps/",
            headers=headers,
            data={"domain_name": domain, "python_version": "python310"},
            timeout=60,
        )
        if r.status_code not in (200, 201):
            print("Create webapp failed:", r.status_code, r.text, file=sys.stderr)
            return 1
        print("Created webapp", domain)

    r = requests.patch(
        base + f"webapps/{domain}/",
        headers=headers,
        data={
            "source_directory": project_home,
            "virtualenv_path": venv_path,
            "force_https": "true",
        },
        timeout=60,
    )
    if r.status_code not in (200,):
        print("Patch webapp failed:", r.status_code, r.text, file=sys.stderr)
        return 1
    print("Updated webapp config")

    r = requests.post(
        base + f"webapps/{domain}/wsgi/",
        headers=headers,
        data={"content": wsgi_body},
        timeout=60,
    )
    if r.status_code not in (200, 201):
        print("Upload WSGI failed:", r.status_code, r.text, file=sys.stderr)
        return 1
    print("Uploaded WSGI")

    static_url = "/static/"
    static_dir = f"{project_home}/static/"
    r = requests.post(
        base + f"webapps/{domain}/static_paths/",
        headers=headers,
        data={"url": static_url, "directory": static_dir},
        timeout=60,
    )
    if r.status_code not in (200, 201):
        print("Static path note:", r.status_code, r.text)

    r = requests.post(base + f"webapps/{domain}/reload/", headers=headers, timeout=120)
    if r.status_code != 200:
        print("Reload failed:", r.status_code, r.text, file=sys.stderr)
        return 1
    print("Reloaded OK → https://" + domain)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

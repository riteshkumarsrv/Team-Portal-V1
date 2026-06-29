#!/usr/bin/env python3
"""Bootstrap + configure Team Portal V1 on PythonAnywhere via API."""

from __future__ import annotations

import json
import os
import sys
import time

import requests

PROJECT = "Team-Portal-V1"
VENV_NAME = "team-portal-v1"
BOOTSTRAP_URL = (
    "https://raw.githubusercontent.com/riteshkumarsrv/Team-Portal-V1/main/"
    "scripts/pythonanywhere_bootstrap.sh"
)


def pa_session(username: str, token: str, region: str = "www") -> tuple[str, dict[str, str]]:
    host = "www.pythonanywhere.com" if region == "www" else "eu.pythonanywhere.com"
    base = f"https://{host}/api/v0/user/{username.lower()}/"
    headers = {"Authorization": f"Token {token}"}
    return base, headers


def create_bash_console(base: str, headers: dict[str, str]) -> int:
    r = requests.post(
        base + "consoles/",
        headers={**headers, "Content-Type": "application/json"},
        json={"executable": "bash"},
        timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Create console failed: {r.status_code} {r.text}")
    return int(r.json()["id"])


def send_console(base: str, headers: dict[str, str], console_id: int, cmd: str, wait: float = 2.0) -> None:
    r = requests.post(
        f"{base}consoles/{console_id}/send_input/",
        headers={**headers, "Content-Type": "application/json"},
        json={"input": cmd if cmd.endswith("\n") else cmd + "\n"},
        timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"send_input failed: {r.status_code} {r.text}")
    time.sleep(wait)


def run_bootstrap(base: str, headers: dict[str, str], username: str) -> None:
    console_id = create_bash_console(base, headers)
    print(f"Created bash console {console_id}")
    send_console(base, headers, console_id, "set -e", 1.0)
    send_console(
        base,
        headers,
        console_id,
        f"curl -sL {BOOTSTRAP_URL} | bash",
        90.0,
    )
    domain = f"{username.lower()}.pythonanywhere.com"
    send_console(
        base,
        headers,
        console_id,
        (
            f"grep -q '^PUBLIC_URL=' $HOME/{PROJECT}/.env && "
            f"sed -i 's|^PUBLIC_URL=.*|PUBLIC_URL=https://{domain}|' $HOME/{PROJECT}/.env || "
            f"echo 'PUBLIC_URL=https://{domain}' >> $HOME/{PROJECT}/.env"
        ),
        3.0,
    )
    if os.environ.get("MANAGER_DASHBOARD_PASSWORD"):
        pwd = os.environ["MANAGER_DASHBOARD_PASSWORD"].replace("'", "'\\''")
        send_console(
            base,
            headers,
            console_id,
            (
                f"grep -q '^MANAGER_DASHBOARD_PASSWORD=' $HOME/{PROJECT}/.env && "
                f"sed -i 's|^MANAGER_DASHBOARD_PASSWORD=.*|MANAGER_DASHBOARD_PASSWORD={pwd}|' "
                f"$HOME/{PROJECT}/.env || "
                f"echo 'MANAGER_DASHBOARD_PASSWORD={pwd}' >> $HOME/{PROJECT}/.env"
            ),
            3.0,
        )
    send_console(
        base,
        headers,
        console_id,
        f"source $HOME/.virtualenvs/{VENV_NAME}/bin/activate && "
        f"cd $HOME/{PROJECT} && python -c \"from wsgi import app; print('WSGI OK')\"",
        15.0,
    )
    print("Bootstrap commands sent")


def configure_webapp(base: str, headers: dict[str, str], username: str) -> str:
    user = username.lower()
    domain = f"{user}.pythonanywhere.com"
    project_home = f"/home/{user}/{PROJECT}"
    venv_path = f"/home/{user}/.virtualenvs/{VENV_NAME}"

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
            raise RuntimeError(f"Create webapp failed: {r.status_code} {r.text}")
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
    if r.status_code != 200:
        raise RuntimeError(f"Patch webapp failed: {r.status_code} {r.text}")

    r = requests.post(
        base + f"webapps/{domain}/wsgi/",
        headers=headers,
        data={"content": wsgi_body},
        timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Upload WSGI failed: {r.status_code} {r.text}")

    r = requests.post(
        base + f"webapps/{domain}/static_paths/",
        headers=headers,
        data={"url": "/static/", "directory": f"{project_home}/static/"},
        timeout=60,
    )
    if r.status_code not in (200, 201):
        print("Static path note:", r.status_code, r.text[:200])

    r = requests.post(base + f"webapps/{domain}/reload/", headers=headers, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Reload failed: {r.status_code} {r.text}")

    return f"https://{domain}"


def main() -> int:
    username = (os.environ.get("PA_USERNAME") or "").strip()
    token = (os.environ.get("PA_API_TOKEN") or "").strip()
    region = (os.environ.get("PA_REGION") or "www").strip()
    if not username or not token:
        print("Set PA_USERNAME and PA_API_TOKEN", file=sys.stderr)
        return 1

    base, headers = pa_session(username, token, region)

    r = requests.get(base, headers=headers, timeout=30)
    if r.status_code == 401:
        print("Invalid API token", file=sys.stderr)
        return 1
    if r.status_code not in (200, 404):
        print("API check:", r.status_code, r.text[:300], file=sys.stderr)
        return 1
    print("API token OK for", username.lower())

    run_bootstrap(base, headers, username)
    url = configure_webapp(base, headers, username)
    print("Deployed →", url)
    print("Verify:", url + "/healthz", "and", url + "/login")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

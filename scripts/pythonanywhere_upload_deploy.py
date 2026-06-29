#!/usr/bin/env python3
"""Upload Team Portal V1 to PythonAnywhere and configure web app."""

from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "Team-Portal-V1"
VENV_NAME = "team-portal-v1"
SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    "Secret",
    "Backup",
    "Not Relevant Files",
    "pa_vendor",  # uploaded separately as a zip
}
SKIP_FILES = {".env", "Secret"}
SKIP_SUFFIXES = (".pyc",)


def pa_base(username: str, token: str) -> tuple[str, dict[str, str]]:
    base = f"https://www.pythonanywhere.com/api/v0/user/{username}/"
    return base, {"Authorization": f"Token {token}"}


def upload_file(base: str, headers: dict[str, str], local: Path, remote: str) -> None:
    with local.open("rb") as fh:
        content = fh.read()
    for attempt in range(8):
        r = requests.post(
            base + f"files/path{remote}",
            headers=headers,
            files={"content": (local.name, content)},
            timeout=120,
        )
        if r.status_code in (200, 201):
            return
        if r.status_code == 429:
            wait = 20 * (attempt + 1)
            print(f"  rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        raise RuntimeError(f"Upload failed {local} -> {remote}: {r.status_code} {r.text[:300]}")
    raise RuntimeError(f"Upload failed after retries: {local}")


def iter_uploads(home: str) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        parts = rel.parts
        if parts[0] in SKIP_DIRS or any(p in SKIP_DIRS for p in parts):
            continue
        if path.name in SKIP_FILES or path.suffix in SKIP_SUFFIXES:
            continue
        if path.name.endswith(".db") and "Latest Database" not in parts:
            continue
        remote = f"{home}/{PROJECT}/" + rel.as_posix()
        out.append((path, remote))
    return out


def upload_repo(base: str, headers: dict[str, str], home: str) -> None:
    files = iter_uploads(home)
    print(f"Uploading {len(files)} files...")
    for i, (local, remote) in enumerate(files, 1):
        upload_file(base, headers, local, remote)
        time.sleep(1.6)
        if i % 15 == 0 or i == len(files):
            print(f"  {i}/{len(files)}")


def upload_install_script(
    base: str,
    headers: dict[str, str],
    home: str,
    domain: str,
    manager_password: str,
) -> None:
    secret = secrets.token_urlsafe(48)
    script = f"""#!/bin/bash
set -euo pipefail
PROJECT="{home}/{PROJECT}"
VENV="{VENV_NAME}"
PY=python3.10
cd "$PROJECT"
if [ ! -d "$HOME/.virtualenvs/$VENV" ]; then
  mkvirtualenv --python="$PY" "$VENV"
fi
source "$HOME/.virtualenvs/$VENV/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt
mkdir -p data
if [ ! -f data/team_tracker.db ] && [ -f "Latest Database/team_tracker.db" ]; then
  cp "Latest Database/team_tracker.db" data/team_tracker.db
fi
if [ ! -f .env ]; then cp .env.example .env; fi
grep -q '^FLASK_SECRET_KEY=' .env && sed -i 's|^FLASK_SECRET_KEY=.*|FLASK_SECRET_KEY={secret}|' .env || echo 'FLASK_SECRET_KEY={secret}' >> .env
grep -q '^TEAM_TRACKER_PRODUCTION=' .env && sed -i 's|^TEAM_TRACKER_PRODUCTION=.*|TEAM_TRACKER_PRODUCTION=1|' .env || echo 'TEAM_TRACKER_PRODUCTION=1' >> .env
grep -q '^PUBLIC_URL=' .env && sed -i 's|^PUBLIC_URL=.*|PUBLIC_URL=https://{domain}|' .env || echo 'PUBLIC_URL=https://{domain}' >> .env
grep -q '^MANAGER_DASHBOARD_PASSWORD=' .env && sed -i 's|^MANAGER_DASHBOARD_PASSWORD=.*|MANAGER_DASHBOARD_PASSWORD={manager_password}|' .env || echo 'MANAGER_DASHBOARD_PASSWORD={manager_password}' >> .env
grep -q '^TEAM_TRACKER_AUTO_TESSERACT=' .env || echo 'TEAM_TRACKER_AUTO_TESSERACT=0' >> .env
python -c "from wsgi import app; print('WSGI OK:', app.name)"
touch "$HOME/.team_portal_installed"
echo DONE
"""
    remote = f"{home}/install_team_portal.sh"
    r = requests.post(
        base + f"files/path{remote}",
        headers=headers,
        files={"content": ("install_team_portal.sh", script.encode())},
        timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Upload install script failed: {r.status_code} {r.text}")


def run_install_via_console(base: str, headers: dict[str, str], home: str, timeout_sec: int = 300) -> int:
    r = requests.post(
        base + "consoles/",
        headers={**headers, "Content-Type": "application/json"},
        json={"executable": "bash"},
        timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Create console failed: {r.status_code} {r.text}")
    console = r.json()
    cid = console["id"]
    url = "https://www.pythonanywhere.com" + console["console_url"]
    print(f"Open this Bash console in your browser (required once on free tier):")
    print(url)

    cmd = f"bash {home}/install_team_portal.sh\n"
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        r = requests.post(
            f"{base}consoles/{cid}/send_input/",
            headers={**headers, "Content-Type": "application/json"},
            json={"input": cmd},
            timeout=60,
        )
        if r.status_code in (200, 201):
            print("Install command sent; waiting for completion...")
            for _ in range(60):
                time.sleep(5)
                chk = requests.get(
                    base + f"files/path{home}/.team_portal_installed",
                    headers=headers,
                    timeout=30,
                )
                if chk.status_code == 200:
                    print("Install completed")
                    return cid
            break
        if r.status_code != 412:
            raise RuntimeError(f"send_input failed: {r.status_code} {r.text}")
        time.sleep(5)
    raise RuntimeError(
        f"Console not started within {timeout_sec}s. Open {url} and re-run this script."
    )


def configure_webapp(base: str, headers: dict[str, str], username: str, home: str) -> str:
    domain = f"{username}.pythonanywhere.com"
    project_home = f"{home}/{PROJECT}"
    venv_path = f"{home}/.virtualenvs/{VENV_NAME}"

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
        base + f"webapps/{domain}/static_files/",
        headers=headers,
        data={"url": "/static/", "path": f"{project_home}/static/"},
        timeout=60,
    )
    if r.status_code not in (200, 201):
        print("Static files note:", r.status_code, r.text[:200])

    r = requests.post(base + f"webapps/{domain}/reload/", headers=headers, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Reload failed: {r.status_code} {r.text}")

    return f"https://{domain}"


def main() -> int:
    username = (os.environ.get("PA_USERNAME") or "").strip()
    token = (os.environ.get("PA_API_TOKEN") or "").strip()
    manager_password = (os.environ.get("MANAGER_DASHBOARD_PASSWORD") or "TeamPortal2026!").strip()
    skip_install = os.environ.get("PA_SKIP_INSTALL") == "1"
    if not username or not token:
        print("Set PA_USERNAME and PA_API_TOKEN", file=sys.stderr)
        return 1

    home = f"/home/{username}"
    base, headers = pa_base(username, token)
    domain = f"{username}.pythonanywhere.com"

    upload_repo(base, headers, home)
    upload_install_script(base, headers, home, domain, manager_password)

    if not skip_install:
        marker = requests.get(base + f"files/path{home}/.team_portal_installed", headers=headers, timeout=30)
        if marker.status_code != 200:
            run_install_via_console(base, headers, home)

    url = configure_webapp(base, headers, username, home)
    print("Deployed →", url)
    print("Verify:", url + "/healthz")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

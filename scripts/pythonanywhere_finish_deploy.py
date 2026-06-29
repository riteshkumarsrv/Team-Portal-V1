#!/usr/bin/env python3
"""Finish PythonAnywhere deploy using bundled Linux vendor zip (no Bash console)."""

from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "Team-Portal-V1"


def pa(username: str, token: str) -> tuple[str, dict[str, str]]:
    return f"https://www.pythonanywhere.com/api/v0/user/{username}/", {
        "Authorization": f"Token {token}"
    }


def upload_bytes(base: str, headers: dict[str, str], remote: str, data: bytes, name: str) -> None:
    for attempt in range(8):
        r = requests.post(
            base + f"files/path{remote}",
            headers=headers,
            files={"content": (name, data)},
            timeout=300,
        )
        if r.status_code in (200, 201):
            return
        if r.status_code == 429:
            time.sleep(20 * (attempt + 1))
            continue
        raise RuntimeError(f"Upload {remote}: {r.status_code} {r.text[:400]}")


def build_env(domain: str, manager_password: str) -> str:
    secret = secrets.token_urlsafe(48)
    return f"""FLASK_SECRET_KEY={secret}
MANAGER_DASHBOARD_PASSWORD={manager_password}
TEAM_TRACKER_PRODUCTION=1
TEAM_TRACKER_AUTO_TESSERACT=0
PUBLIC_URL=https://{domain}
TEAM_TRACKER_DB_PATH=/home/{{username}}/{PROJECT}/data/team_tracker.db
"""


def wsgi_body(home: str, domain: str) -> str:
    project_home = f"{home}/{PROJECT}"
    return f'''import os
import sys
import zipfile

project_home = "{project_home}"
vendor_dir = os.path.join(project_home, "pa_vendor")
vendor_zip = os.path.join(project_home, "deploy", "pa_vendor.zip")
vendor_marker = os.path.join(vendor_dir, ".extracted")

if project_home not in sys.path:
    sys.path.insert(0, project_home)
if vendor_dir not in sys.path:
    sys.path.insert(0, vendor_dir)

if not os.path.isfile(vendor_marker) and os.path.isfile(vendor_zip):
    os.makedirs(vendor_dir, exist_ok=True)
    with zipfile.ZipFile(vendor_zip, "r") as zf:
        for name in zf.namelist():
            if not name.startswith("pa_vendor/"):
                continue
            target = os.path.join(project_home, name)
            if name.endswith("/"):
                os.makedirs(target, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as dst:
                    dst.write(src.read())
    open(vendor_marker, "w", encoding="utf-8").write("ok")

os.chdir(project_home)
os.environ.setdefault("TEAM_TRACKER_DB_PATH", os.path.join(project_home, "data", "team_tracker.db"))
os.environ.setdefault("TEAM_TRACKER_PRODUCTION", "1")
os.environ.setdefault("TEAM_TRACKER_AUTO_TESSERACT", "0")
os.environ.setdefault("PUBLIC_URL", "https://{domain}")

from wsgi import app as application
'''


def main() -> int:
    username = (os.environ.get("PA_USERNAME") or "").strip()
    token = (os.environ.get("PA_API_TOKEN") or "").strip()
    manager_password = (os.environ.get("MANAGER_DASHBOARD_PASSWORD") or "TeamPortal2026!").strip()
    if not username or not token:
        print("Set PA_USERNAME and PA_API_TOKEN", file=sys.stderr)
        return 1

    home = f"/home/{username}"
    domain = f"{username}.pythonanywhere.com"
    base, headers = pa(username, token)
    project = f"{home}/{PROJECT}"

    env_text = build_env(domain, manager_password).replace("{username}", username)
    upload_bytes(base, headers, f"{project}/.env", env_text.encode(), ".env")
    print("Uploaded .env")

    # DB is intentionally NOT uploaded on every deploy — the live database on
    # PythonAnywhere persists between deploys. Overwriting it would erase all
    # team member updates. To seed a fresh install only, use --seed-db flag.
    db = ROOT / "Latest Database" / "team_tracker.db"
    if "--seed-db" in sys.argv and db.is_file():
        upload_bytes(base, headers, f"{project}/data/team_tracker.db", db.read_bytes(), "team_tracker.db")
        print("Uploaded database (seed — only on first deploy)")

    vendor_zip = ROOT / "deploy" / "pa_vendor.zip"
    if not vendor_zip.is_file():
        print("Missing deploy/pa_vendor.zip — run vendor build first", file=sys.stderr)
        return 1
    print(f"Uploading vendor zip ({vendor_zip.stat().st_size // 1024 // 1024} MB)...")
    upload_bytes(base, headers, f"{project}/deploy/pa_vendor.zip", vendor_zip.read_bytes(), "pa_vendor.zip")
    print("Uploaded vendor zip")

    apps = requests.get(base + "webapps/", headers=headers, timeout=60).json()
    if not any(a["domain_name"] == domain for a in apps):
        r = requests.post(
            base + "webapps/",
            headers=headers,
            data={"domain_name": domain, "python_version": "python310"},
            timeout=60,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Create webapp: {r.status_code} {r.text}")

    r = requests.patch(
        base + f"webapps/{domain}/",
        headers=headers,
        data={"source_directory": project, "force_https": "true"},
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Patch webapp: {r.status_code} {r.text}")

    wsgi_path = f"/var/www/{username.lower()}_pythonanywhere_com_wsgi.py"
    upload_bytes(
        base,
        headers,
        wsgi_path,
        wsgi_body(home, domain).encode(),
        "wsgi.py",
    )
    print("Uploaded WSGI")

    static = requests.get(base + f"webapps/{domain}/static_files/", headers=headers, timeout=60)
    if static.status_code == 200 and not static.json():
        requests.post(
            base + f"webapps/{domain}/static_files/",
            headers=headers,
            data={"url": "/static/", "path": f"{project}/static/"},
            timeout=60,
        )

    r = requests.post(base + f"webapps/{domain}/reload/", headers=headers, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"Reload: {r.status_code} {r.text}")

    url = f"https://{domain}"
    print("Live ->", url)
    for i in range(12):
        time.sleep(10)
        try:
            h = requests.get(url + "/healthz", timeout=30)
            print("healthz", h.status_code, h.text[:120])
            if h.status_code == 200:
                return 0
        except requests.RequestException as exc:
            print("healthz error", exc)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

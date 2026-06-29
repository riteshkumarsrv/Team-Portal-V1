#!/usr/bin/env python3
"""
Download the live team_tracker.db from PythonAnywhere and save a timestamped
copy to LiveDatabaseBackup/.

Usage:
    python scripts/backup_live_db.py

Credentials are read from environment variables or the project Secret file:
    PA_USERNAME   — PythonAnywhere username  (default: TeamPortal)
    PA_API_TOKEN  — PythonAnywhere API token

Keeps the last 30 daily backups and removes older ones automatically.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── config ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT / "LiveDatabaseBackup"
PROJECT = "Team-Portal-V1"
REMOTE_DB_PATH = "data/team_tracker.db"
KEEP_DAYS = 30        # number of backups to retain
PA_BASE = "https://www.pythonanywhere.com/api/v0/user/{username}/"


def load_token() -> tuple[str, str]:
    username = os.environ.get("PA_USERNAME", "TeamPortal").strip()
    token = os.environ.get("PA_API_TOKEN", "").strip()
    if not token:
        # Fall back to the project Secret file if the env var is not set.
        secret_file = ROOT / "Secret"
        if secret_file.is_file():
            for line in secret_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "@" not in line:
                    token = line.split()[0]
                    break
    if not token:
        print("ERROR: Set PA_API_TOKEN env var or add the token to the Secret file.", file=sys.stderr)
        sys.exit(1)
    return username, token


def download_db(username: str, token: str) -> bytes:
    base = PA_BASE.format(username=username)
    headers = {"Authorization": f"Token {token}"}
    remote = f"/home/{username}/{PROJECT}/{REMOTE_DB_PATH}"
    url = base + f"files/path{remote}"
    print(f"Downloading from {url} ...")
    r = requests.get(url, headers=headers, timeout=120)
    if r.status_code != 200:
        print(f"ERROR: {r.status_code} {r.text[:400]}", file=sys.stderr)
        sys.exit(1)
    return r.content


def save_backup(data: bytes) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"team_tracker_{ts}.db"
    dest.write_bytes(data)
    return dest


def prune_old_backups() -> None:
    backups = sorted(BACKUP_DIR.glob("team_tracker_*.db"))
    if len(backups) > KEEP_DAYS:
        to_remove = backups[: len(backups) - KEEP_DAYS]
        for f in to_remove:
            f.unlink()
            print(f"  Removed old backup: {f.name}")


def main() -> None:
    username, token = load_token()
    data = download_db(username, token)
    dest = save_backup(data)
    size_kb = len(data) / 1024
    print(f"Saved -> {dest}  ({size_kb:.1f} KB)")
    prune_old_backups()
    remaining = list(BACKUP_DIR.glob("team_tracker_*.db"))
    print(f"Backups retained: {len(remaining)}")


if __name__ == "__main__":
    main()

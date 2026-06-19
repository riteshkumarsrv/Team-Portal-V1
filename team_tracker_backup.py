"""
Periodic SQLite backups for TEAM MANAGEMENT PORTAL.

Copies the configured DB into ``<project>/Backup/`` using SQLite's online backup API
(safe while the app is running). Optional background thread when ``create_app`` loads.

Environment (see README / .env.example):

- ``TEAM_TRACKER_DB_BACKUP_INTERVAL_HOURS`` — hours between backups while the Flask
  process is running. Default ``2``. Set to ``0`` to disable the in-app scheduler.
- ``TEAM_TRACKER_DB_BACKUP_KEEP`` — max number of backup files to retain (oldest
  deleted first). Default ``30``.

CLI::

    python team_tracker_backup.py --once

Run from the project directory (same folder as ``main.py``), or set ``TEAM_TRACKER_DB_PATH``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flask import Flask

_log = logging.getLogger(__name__)

_DEFAULT_INTERVAL_HOURS = 2.0
_DEFAULT_KEEP = 30
_BACKUP_PREFIX = "team_tracker_"


def _interval_hours_from_env() -> float | None:
    """Default 2 hours when unset. Set ``TEAM_TRACKER_DB_BACKUP_INTERVAL_HOURS=0`` to disable."""
    raw = (os.environ.get("TEAM_TRACKER_DB_BACKUP_INTERVAL_HOURS") or "").strip().lower()
    if raw in ("0", "false", "no", "off", "none"):
        return None
    if not raw:
        return _DEFAULT_INTERVAL_HOURS
    try:
        h = float(raw)
    except ValueError:
        return _DEFAULT_INTERVAL_HOURS
    if h <= 0:
        return None
    return max(h, 1.0 / 60.0)  # at least 1 minute


def _keep_count_from_env() -> int:
    raw = (os.environ.get("TEAM_TRACKER_DB_BACKUP_KEEP") or "").strip()
    if not raw:
        return _DEFAULT_KEEP
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_KEEP
    return max(1, min(n, 500))


def project_root_from_flask_app(app: Flask) -> Path:
    return Path(app.root_path).resolve()


def backup_dir(project_root: Path) -> Path:
    return project_root / "Backup"


def run_one_backup(app: Flask) -> Path | None:
    """Write one timestamped backup under ``Backup/``; prune old files. Returns path or None."""
    db_path = Path(str(app.config.get("DB_PATH") or "")).expanduser()
    if str(db_path) == ":memory:" or not db_path.exists():
        _log.warning("DB backup skipped (missing or in-memory DB): %s", db_path)
        return None

    root = project_root_from_flask_app(app)
    out_dir = backup_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = out_dir / f"{_BACKUP_PREFIX}{stamp}.db"

    try:
        src_conn = sqlite3.connect(str(db_path))
        try:
            dest_conn = sqlite3.connect(str(dest))
            try:
                src_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            src_conn.close()
    except OSError as e:
        _log.exception("DB backup failed (I/O): %s", e)
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        return None
    except sqlite3.Error as e:
        _log.exception("DB backup failed (SQLite): %s", e)
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        return None

    _log.info("DB backup written: %s", dest)
    _prune_old_backups(out_dir, _keep_count_from_env())
    return dest


def _prune_old_backups(out_dir: Path, keep: int) -> None:
    files = sorted(
        (p for p in out_dir.iterdir() if p.is_file() and p.suffix.lower() == ".db" and p.name.startswith(_BACKUP_PREFIX)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[keep:]:
        try:
            old.unlink()
            _log.info("Removed old DB backup: %s", old.name)
        except OSError as e:
            _log.warning("Could not remove old backup %s: %s", old, e)


def _should_start_background_threads() -> bool:
    """Avoid duplicate timers in Flask/Werkzeug reloader parent process."""
    if (os.environ.get("WERKZEUG_RUN_MAIN") or "").strip().lower() == "true":
        return True
    dbg = (os.environ.get("FLASK_DEBUG") or os.environ.get("FLASK_ENV") or "").strip().lower()
    if dbg in ("1", "true", "yes", "development"):
        return False
    return True


def maybe_start_backup_scheduler(app: Flask) -> None:
    """Start a daemon thread that backs up the DB every N hours (see env)."""
    if app.config.get("TESTING"):
        return
    interval_h = _interval_hours_from_env()
    if interval_h is None:
        return
    if not _should_start_background_threads():
        return

    interval_s = interval_h * 3600.0

    def _loop() -> None:
        time.sleep(5.0)
        while True:
            try:
                with app.app_context():
                    run_one_backup(app)
            except Exception:
                _log.exception("Scheduled DB backup failed")
            time.sleep(interval_s)

    t = threading.Thread(target=_loop, name="team_tracker_db_backup", daemon=True)
    t.start()
    _log.info(
        "DB auto-backup enabled: every %.2f h → %s",
        interval_h,
        backup_dir(project_root_from_flask_app(app)),
    )


def main() -> None:
    """CLI: load .env like ``create_app`` / ``main.py``, run a single backup, exit."""
    from dotenv import load_dotenv
    from flask import Flask

    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env", override=True)
    load_dotenv(override=False)

    db_default = root / "data" / "team_tracker.db"
    db_path = Path(os.environ.get("TEAM_TRACKER_DB_PATH", str(db_default))).expanduser()
    if str(db_path) == ":memory:" or not db_path.exists():
        print(f"No DB file to back up: {db_path}")
        raise SystemExit(1)

    cli_app = Flask("team_tracker_backup_cli", root_path=str(root))
    cli_app.config["DB_PATH"] = str(db_path)
    with cli_app.app_context():
        dest = run_one_backup(cli_app)
    if dest:
        print(dest)
    else:
        raise SystemExit(2)


if __name__ == "__main__":
    import sys

    if "--once" not in sys.argv:
        print("Usage: python team_tracker_backup.py --once", file=sys.stderr)
        raise SystemExit(64)
    main()

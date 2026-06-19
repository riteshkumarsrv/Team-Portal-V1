"""
Application configuration loaded from the environment (and optional ``.env``).

Layout follows the Dashboard_Base ``create-dashboard`` skill: central settings
separate from route and domain logic in ``app.py``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from flask import Flask

_log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "team_tracker.db"


def load_application_environment() -> None:
    """Load ``.env`` from the project root and apply selective fill from file to ``os.environ``."""
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    load_dotenv(override=False)
    fill_empty_env_from_dotenv_file(PROJECT_ROOT / ".env")


def fill_empty_env_from_dotenv_file(path: Path) -> None:
    """Apply selected .env values when the process env has missing or blank entries (employee auth only).

    Does not touch keys like MANAGER_DASHBOARD_PASSWORD so tests can force an empty password with monkeypatch.
    """
    if not path.is_file():
        return
    fill_keys = frozenset(
        {
            "MICROSOFT_OAUTH_CLIENT_ID",
            "MICROSOFT_OAUTH_CLIENT_SECRET",
            "MICROSOFT_OAUTH_TENANT_ID",
            "PORTAL_OTP_SMTP_HOST",
            "PORTAL_OTP_SMTP_PORT",
            "PORTAL_OTP_SMTP_USER",
            "PORTAL_OTP_SMTP_PASSWORD",
            "PORTAL_OTP_FROM",
            "PORTAL_OTP_FROM_NAME",
            "PORTAL_OTP_USE_TLS",
            "PORTAL_OTP_TTL_MINUTES",
            "PORTAL_OTP_DEV_CONSOLE",
            "TEAM_TRACKER_PRODUCTION",
        }
    )
    for k, v in (dotenv_values(path) or {}).items():
        if not k or k not in fill_keys or v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        cur = os.environ.get(k)
        if cur is None or (isinstance(cur, str) and cur.strip() == ""):
            os.environ[k] = s


def apply_flask_config_from_environ(app: Flask) -> None:
    """Populate ``app.config`` from environment variables (call after ``load_application_environment``)."""
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or "dev-only-change-with-FLASK_SECRET_KEY"
    app.config["DB_PATH"] = os.environ.get("TEAM_TRACKER_DB_PATH", str(DEFAULT_DB_PATH))
    _mgr_env = os.environ.get("MANAGER_DASHBOARD_PASSWORD")
    app.config["MANAGER_DASHBOARD_PASSWORD"] = ("team" if _mgr_env is None else _mgr_env).strip()
    app.config["PRIMARY_OWNER_MANAGER_EMAIL"] = (os.environ.get("PRIMARY_OWNER_MANAGER_EMAIL") or "").strip()
    app.config["LPO_SM_DASHBOARD_PASSWORD"] = (os.environ.get("LPO_SM_DASHBOARD_PASSWORD") or "").strip()
    app.config["WTF_CSRF_ENABLED"] = os.environ.get("WTF_CSRF_ENABLED", "true").lower() in (
        "1",
        "true",
        "yes",
    )
    ms_id = (os.environ.get("MICROSOFT_OAUTH_CLIENT_ID") or "").strip()
    app.config["MICROSOFT_OAUTH_CLIENT_ID"] = ms_id
    app.config["MICROSOFT_OAUTH_CLIENT_SECRET"] = (os.environ.get("MICROSOFT_OAUTH_CLIENT_SECRET") or "").strip()
    app.config["MICROSOFT_OAUTH_TENANT_ID"] = (
        (os.environ.get("MICROSOFT_OAUTH_TENANT_ID") or "organizations").strip() or "organizations"
    )

    app.config["PORTAL_OTP_SMTP_HOST"] = (os.environ.get("PORTAL_OTP_SMTP_HOST") or "").strip()
    app.config["PORTAL_OTP_SMTP_PORT"] = int((os.environ.get("PORTAL_OTP_SMTP_PORT") or "587").strip() or "587")
    app.config["PORTAL_OTP_SMTP_USER"] = (os.environ.get("PORTAL_OTP_SMTP_USER") or "").strip()
    app.config["PORTAL_OTP_SMTP_PASSWORD"] = (os.environ.get("PORTAL_OTP_SMTP_PASSWORD") or "").strip()
    app.config["PORTAL_OTP_FROM"] = (os.environ.get("PORTAL_OTP_FROM") or "").strip()
    app.config["PORTAL_OTP_FROM_NAME"] = (os.environ.get("PORTAL_OTP_FROM_NAME") or "TEAM MANAGEMENT PORTAL").strip()
    app.config["PORTAL_OTP_USE_TLS"] = os.environ.get("PORTAL_OTP_USE_TLS", "true").lower() in (
        "1",
        "true",
        "yes",
    )
    app.config["PORTAL_OTP_TTL_MINUTES"] = int((os.environ.get("PORTAL_OTP_TTL_MINUTES") or "15").strip() or "15")
    _production = os.environ.get("TEAM_TRACKER_PRODUCTION", "").strip().lower() in ("1", "true", "yes")
    _smtp_ready_env = bool(
        (app.config.get("PORTAL_OTP_SMTP_HOST") or "").strip()
        and (app.config.get("PORTAL_OTP_FROM") or "").strip()
    )
    _raw_dev_console = (os.environ.get("PORTAL_OTP_DEV_CONSOLE") or "").strip().lower()
    if _raw_dev_console in ("0", "false", "no", "off"):
        app.config["PORTAL_OTP_DEV_CONSOLE"] = False
    elif _raw_dev_console in ("1", "true", "yes"):
        app.config["PORTAL_OTP_DEV_CONSOLE"] = True
    else:
        app.config["PORTAL_OTP_DEV_CONSOLE"] = not _production and not ms_id and not _smtp_ready_env
    if (
        app.config["PORTAL_OTP_DEV_CONSOLE"]
        and _raw_dev_console == ""
        and not _production
        and not ms_id
        and not _smtp_ready_env
    ):
        _log.warning(
            "Employee email OTP dev console is auto-enabled (no Microsoft OAuth, no SMTP, TEAM_TRACKER_PRODUCTION unset). "
            "Set TEAM_TRACKER_PRODUCTION=1 and configure MICROSOFT_OAUTH_* or PORTAL_OTP_SMTP_* before production; "
            "or set PORTAL_OTP_DEV_CONSOLE=0 to hide employee sign-in until configured."
        )

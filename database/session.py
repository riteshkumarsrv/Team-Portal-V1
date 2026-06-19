"""SQLite connection factory for the Flask app (per-request pattern: pass ``current_app`` into ``get_db``)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import Flask


def db_path(app: Flask) -> Path:
    return Path(app.config["DB_PATH"])


def get_db(app: Flask) -> sqlite3.Connection:
    p = db_path(app)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

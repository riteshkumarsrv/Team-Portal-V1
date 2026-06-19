"""Runnable entry point for local development (create-dashboard layout)."""

from __future__ import annotations

import os

from app import create_app

if __name__ == "__main__":
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "1").lower() in ("1", "true", "yes")
    create_app().run(host=host, port=port, debug=debug)

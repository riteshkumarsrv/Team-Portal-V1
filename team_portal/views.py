"""
HTTP route registration.

Operational routes that do not need heavy imports from ``app`` live here as
Flask blueprints. Most portal, manager, and Scrum routes remain in
``app.create_app`` until they are migrated incrementally.
"""

from __future__ import annotations

from flask import Blueprint, Flask

system_bp = Blueprint("system", __name__)


@system_bp.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(system_bp)

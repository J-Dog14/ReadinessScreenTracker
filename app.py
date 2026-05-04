"""
Flask entry point for the Readiness Screen Tracker.

Two pages:
  /maintenance — UAIS-style ingestion UI
  /dashboard   — original-style readiness dashboard

Run:
  python app.py
or:
  flask --app app:create_app run --port 5057
"""
from __future__ import annotations

import logging
from flask import Flask, redirect, url_for

from config import get_flask_debug, get_flask_port
from routes.dashboard import bp as dashboard_bp
from routes.maintenance import bp as maintenance_bp


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["JSON_SORT_KEYS"] = False
    app.register_blueprint(maintenance_bp)
    app.register_blueprint(dashboard_bp)

    @app.route("/")
    def root():
        return redirect(url_for("maintenance.page"))

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return app


# Simple CLI: `python -m app.cli init-db` not used since this is a single file.
# Use `python -c "from db.connection import init_db; init_db()"` to migrate.

if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=get_flask_port(), debug=get_flask_debug(), threaded=True)

from contextlib import closing
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, render_template


def create_app(snapshot_getter, control_queue, db_path):
    app = Flask(__name__)
    path = Path(db_path).resolve()

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/state")
    def state():
        return jsonify(snapshot_getter())

    @app.get("/events")
    def events():
        uri = path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT id, ts, dose_id, kind, detail FROM events "
                "WHERE kind != 'state' ORDER BY id DESC LIMIT 30"
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.get("/schedules")
    def schedules():
        uri = path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT id, medication, slot, time_hhmm "
                "FROM schedules ORDER BY time_hhmm, slot, id"
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.post("/trigger")
    def trigger():
        control_queue.put("trigger_dose")
        return jsonify({"ok": True})

    @app.post("/confirm")
    def confirm():
        control_queue.put("confirm")
        return jsonify({"ok": True})

    return app
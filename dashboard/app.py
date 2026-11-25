from contextlib import closing
import json
import queue
import re
import sqlite3
import sys
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import db  # noqa: E402
from backend.validation import ValidationError, validate_schedule  # noqa: E402

NOTIFICATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FINAL_STATUSES = ("taken", "missed", "failed")
ALL_STATUSES = ("pending", "active", *FINAL_STATUSES)

def create_app(
    snapshot_getter,
    control_queue,
    db_path,
    broadcaster=None,
    reply_timeout=5.0,
    keepalive=15.0,
):
    """Build the dashboard app.

    Flask never writes the DB: writes are sent to the main loop as commands on
    ``control_queue`` and the handler waits (up to ``reply_timeout``) for the
    main loop's reply. Reads use a separate read-only SQLite connection.
    """
    app = Flask(__name__)
    path = Path(db_path).resolve()

    def read_only():
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        return closing(connection)

    def error(status, errors):
        return jsonify({"ok": False, "errors": errors}), status

    def send_command(command):
        reply = queue.Queue(maxsize=1)
        control_queue.put({**command, "reply": reply})
        try:
            result = reply.get(timeout=reply_timeout)
        except queue.Empty:
            return jsonify({"ok": True, "pending": True}), 202
        status = result.pop("status", 200)
        return jsonify(result), status

    def json_body():
        data = request.get_json(silent=True)
        if data is None:
            raise ValidationError({"body": "request body must be JSON (Content-Type: application/json)"})
        return data

    @app.get("/")
    def index():
        return render_template("index.html", page="home")

    @app.get("/caregiver")
    def caregiver_page():
        return render_template("caregiver.html", page="caregiver")

    @app.get("/patient")
    def patient_page():
        return render_template("patient.html", page="patient")

    @app.get("/robot")
    def robot_page():
        return render_template("robot.html", page="robot")

    @app.get("/state")
    def state():
        return jsonify(snapshot_getter())

    @app.get("/events")
    def events():
        with read_only() as connection:
            rows = connection.execute(
                "SELECT id, ts, dose_id, kind, detail FROM events "
                "WHERE kind != 'state' ORDER BY id DESC LIMIT 30"
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    # ------------------------------------------------------------ schedules

    @app.get("/schedules")
    def schedules():
        with read_only() as connection:
            rows = db.list_schedules(connection)
        return jsonify([db.schedule_to_dict(row) for row in rows])

    @app.post("/schedules")
    def create_schedule():
        try:
            data = validate_schedule(json_body())
        except ValidationError as exc:
            return error(400, exc.errors)
        return send_command({"type": "schedule_create", "data": data})

    @app.put("/schedules/<int:schedule_id>")
    def update_schedule(schedule_id):
        try:
            data = validate_schedule(json_body(), partial=True)
        except ValidationError as exc:
            return error(400, exc.errors)
        return send_command({"type": "schedule_update", "id": schedule_id, "data": data})

    @app.delete("/schedules/<int:schedule_id>")
    def delete_schedule(schedule_id):
        return send_command({"type": "schedule_delete", "id": schedule_id})

    # ---------------------------------------------------------------- doses

    @app.get("/doses")
    def doses():
        try:
            limit = int(request.args.get("limit", 50))
        except ValueError:
            return error(400, {"limit": "limit must be an integer"})
        limit = max(1, min(limit, 500))
        status_arg = request.args.get("status", "")
        statuses = [s.strip() for s in status_arg.split(",") if s.strip()]
        bad = [s for s in statuses if s not in ALL_STATUSES]
        if bad:
            return error(400, {"status": f"unknown status: {', '.join(bad)}"})
        order = "asc" if request.args.get("order") == "asc" else "desc"
        with read_only() as connection:
            rows = db.recent_doses(connection, limit, statuses or None, order)
        return jsonify(rows)

    # -------------------------------------------------------- notifications

    @app.get("/notifications")
    def notifications():
        with read_only() as connection:
            return jsonify(db.recent_notifications(connection, 50))

    @app.post("/notifications/<notification_id>/ack")
    def ack_notification(notification_id):
        if not NOTIFICATION_ID_RE.match(notification_id):
            return error(400, {"id": "invalid notification id"})
        return send_command({"type": "ack_notification", "id": notification_id})

    # --------------------------------------------------------- live updates

    @app.get("/stream")
    def stream():
        if broadcaster is None:
            return error(503, {"stream": "live updates are not available"})
        client = broadcaster.subscribe()

        def generate():
            try:
                yield "retry: 3000\n\n"
                yield _sse("state", snapshot_getter())
                while True:
                    try:
                        event, data = client.get(timeout=keepalive)
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    yield _sse(event, data)
            finally:
                broadcaster.unsubscribe(client)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/confirm")
    def confirm():
        control_queue.put("confirm")
        return jsonify({"ok": True})

    @app.post("/sim/<device>/<action>")
    def sim_control(device, action):
        """Simulation buttons: press_button, open_lid, estop, release_estop,
        lost_line, heartbeat_off/on, power_off/on, set_battery, refill."""
        body = request.get_json(silent=True) or {}
        extra = {k: body[k] for k in ("value", "slot", "count") if k in body}
        return send_command({"type": "sim", "device": device, "action": action, **extra})

    return app

def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from backend import db
from backend.agents import (
    ARRIVED,
    CAREGIVER_ALERT,
    DELIVERY_STARTED,
    ROBOT_COMMAND,
    SCHEDULE_CHANGED,
)
from backend.config import Config
from backend.core import Broadcaster
from backend.main import parse_schedule
from backend.runtime import Runtime
from backend.validation import ValidationError, validate_schedule
from dashboard.app import create_app


def local_ts(day, hour, minute=0, second=0):
    return datetime.combine(day, datetime.min.time()).replace(
        hour=hour, minute=minute, second=second
    ).timestamp()


TODAY = datetime.now().date()
NOON = local_ts(TODAY, 12)


class ManualClock:
    """Deterministic clock: tests advance ``t`` themselves."""

    def __init__(self, start):
        self.t = start
        self.speed = 1

    def now(self):
        return self.t


def default_config(**overrides):
    values = dict(
        load_timeout=3,
        reminder_interval=4,
        confirm_timeout=40,
        delivery_timeout=50,
        max_start_delay=8,
    )
    values.update(overrides)
    return Config(**values)


class RuntimeTestCase(unittest.TestCase):
    STEP = 0.25

    def make_runtime(self, config=None, start=NOON, db_path=None, **sim):
        if db_path is None:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            db_path = Path(temporary.name) / "test.db"
        self.db_path = db_path
        connection = db.init_db(db_path)
        self.addCleanup(connection.close)
        self.clock = ManualClock(start)
        sim.setdefault("travel_time", 8)
        sim.setdefault("seed", 1)
        runtime = Runtime(connection, config or default_config(), self.clock, **sim)
        self.alerts = []
        self.commands = []
        self.messages = []
        runtime.bus.subscribe(CAREGIVER_ALERT, self.alerts.append)
        runtime.bus.subscribe(ROBOT_COMMAND, self.commands.append)
        for topic in (DELIVERY_STARTED, ARRIVED):
            runtime.bus.subscribe(
                topic, lambda p, topic=topic: self.messages.append((topic, self.clock.now(), p))
            )
        return runtime, connection

    def advance(self, runtime, seconds, until=None):
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.t += self.STEP
            runtime.step(self.clock.t)
            if until is not None and until():
                return True
        return False

    @staticmethod
    def status(connection, dose_id):
        return db.get_dose(connection, dose_id)["status"]


class ScenarioTests(RuntimeTestCase):
    def run_scenario(
        self,
        confirm_prob=0,
        load_fail_prob=0,
        obstacle_prob=0,
        obstacle_s=6,
        config=None,
    ):
        runtime, connection = self.make_runtime(
            config,
            confirm_prob=confirm_prob,
            load_fail_prob=load_fail_prob,
            obstacle_prob=obstacle_prob,
            obstacle_s=obstacle_s,
        )
        dose_id = db.create_dose_now(connection, "Test medicine", 1, self.clock.now())

        def finished():
            status = self.status(connection, dose_id)
            if status in ("taken", "missed"):
                return runtime.robot.state == "IDLE"
            if status == "failed":
                return runtime.robot.state in ("IDLE", "CHARGING")
            return False

        self.advance(runtime, 500, finished)
        return connection, dose_id, self.alerts, self.commands

    def test_dose_taken(self):
        connection, dose_id, _, _ = self.run_scenario(confirm_prob=1)
        self.assertEqual(db.get_dose(connection, dose_id)["status"], "taken")

    def test_no_confirmation_escalates(self):
        connection, dose_id, alerts, commands = self.run_scenario(confirm_prob=0)
        self.assertEqual(db.get_dose(connection, dose_id)["status"], "missed")
        self.assertTrue(any(alert["level"] == "critical" for alert in alerts))
        reminders = [
            command
            for command in commands
            if command["action"] == "alert" and command["on"] is True
        ]
        self.assertGreaterEqual(len(reminders), 2)

    def test_load_failure_fails_dose(self):
        connection, dose_id, alerts, _ = self.run_scenario(load_fail_prob=1)
        self.assertEqual(db.get_dose(connection, dose_id)["status"], "failed")
        self.assertTrue(any("load_failed" in alert["message"] for alert in alerts))
        failure = connection.execute(
            "SELECT detail FROM events WHERE dose_id = ? AND kind = 'failure'",
            (dose_id,),
        ).fetchone()
        self.assertEqual(json.loads(failure["detail"])["reason"], "load_failed")

    def test_long_obstacle_fails_delivery(self):
        config = Config(
            load_timeout=3,
            reminder_interval=4,
            confirm_timeout=20,
            delivery_timeout=50,
            max_start_delay=8,
            obstacle_limit=3,
        )
        connection, dose_id, alerts, _ = self.run_scenario(
            obstacle_prob=1, obstacle_s=20, config=config
        )
        self.assertEqual(db.get_dose(connection, dose_id)["status"], "failed")
        self.assertTrue(any("path_blocked" in alert["message"] for alert in alerts))
        failure = connection.execute(
            "SELECT detail FROM events WHERE dose_id = ? AND kind = 'failure'",
            (dose_id,),
        ).fetchone()
        self.assertEqual(json.loads(failure["detail"])["reason"], "path_blocked")


class AgentFixTests(RuntimeTestCase):
    def test_info_notifications_have_required_fields(self):
        runtime, connection = self.make_runtime(confirm_prob=1)
        dose_id = db.create_dose_now(connection, "Test medicine", 1, self.clock.now())
        self.advance(runtime, 200, lambda: self.status(connection, dose_id) == "taken")
        self.advance(runtime, 2)
        info = [a for a in self.alerts if a["level"] == "info" and a["dose_id"] == dose_id]
        messages = " | ".join(a["message"] for a in info)
        self.assertIn("dispatched", messages)
        self.assertIn("arrived", messages)
        self.assertIn("taken", messages)
        for alert in self.alerts:
            for field in ("id", "ts", "level", "dose_id", "message"):
                self.assertIn(field, alert)
        self.assertEqual(len({a["id"] for a in self.alerts}), len(self.alerts))
        stored = db.recent_notifications(connection)
        self.assertEqual(len(stored), len(self.alerts))

    def test_confirm_deadline_starts_at_arrival(self):
        # Travel (30 s) is longer than the confirmation window (10 s). The old
        # behaviour started the clock at delivery_started and declared the dose
        # missed before the robot even arrived.
        config = default_config(confirm_timeout=10, delivery_timeout=100, reminder_interval=100)
        runtime, connection = self.make_runtime(config, travel_time=30)
        dose_id = db.create_dose_now(connection, "Test medicine", 1, self.clock.now())
        self.advance(runtime, 200, lambda: self.status(connection, dose_id) == "missed")
        self.assertEqual(self.status(connection, dose_id), "missed")
        arrived = [t for topic, t, p in self.messages if topic == ARRIVED and p["dose_id"] == dose_id]
        started = [t for topic, t, p in self.messages if topic == DELIVERY_STARTED and p["dose_id"] == dose_id]
        critical = [a for a in self.alerts if a["level"] == "critical" and a["dose_id"] == dose_id]
        self.assertEqual(len(arrived), 1)
        self.assertEqual(len(critical), 1)
        self.assertGreaterEqual(arrived[0] - started[0], 30)
        self.assertGreaterEqual(critical[0]["ts"] - arrived[0], 10)
        self.assertLess(critical[0]["ts"] - arrived[0], 12)

    def test_second_dose_is_queued_while_robot_busy(self):
        runtime, connection = self.make_runtime(confirm_prob=1)
        first = db.create_dose_now(connection, "First", 1, self.clock.now())
        second = db.create_dose_now(connection, "Second", 2, self.clock.now())
        self.advance(runtime, 30)
        self.assertEqual(self.status(connection, second), "pending")
        self.assertEqual(runtime.snapshot["queued"], 1)
        self.advance(
            runtime,
            400,
            lambda: self.status(connection, second) == "taken" and runtime.robot.state == "IDLE",
        )
        self.assertEqual(self.status(connection, first), "taken")
        self.assertEqual(self.status(connection, second), "taken")
        self.assertFalse(any("robot_unavailable" in a["message"] for a in self.alerts))

    def test_stale_pending_doses_become_missed_at_startup(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "test.db"
        seed = db.init_db(path)
        stale = db.create_dose_now(seed, "Old dose", 1, NOON - 3600)
        recent = db.create_dose_now(seed, "Recent dose", 1, NOON - 300)
        seed.close()

        runtime, connection = self.make_runtime(db_path=path, confirm_prob=1)
        runtime.step(self.clock.now())
        self.assertEqual(self.status(connection, stale), "missed")
        self.assertNotEqual(self.status(connection, recent), "missed")
        critical = [a for a in self.alerts if a["level"] == "critical"]
        self.assertEqual([a["dose_id"] for a in critical], [stale])
        self.assertEqual(critical[0]["reason"], "past_grace_window")
        self.advance(runtime, 200, lambda: self.status(connection, recent) == "taken")
        self.assertEqual(self.status(connection, recent), "taken")
        history = {d["id"]: d for d in db.recent_doses(connection)}
        self.assertEqual(history[stale]["reason"], "past_grace_window")

    def test_startup_does_not_generate_doses_older_than_grace(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "test.db"
        seed = db.init_db(path)
        db.add_schedule(seed, "Morning", 1, "08:00")
        db.add_schedule(seed, "Just now", 2, "11:55")
        db.add_schedule(seed, "Evening", 3, "20:00")
        seed.close()
        runtime, connection = self.make_runtime(db_path=path)
        runtime.step(self.clock.now())
        meds = {row["medication"] for row in connection.execute("SELECT medication FROM doses")}
        self.assertEqual(meds, {"Just now", "Evening"})

    def test_doses_regenerated_at_midnight(self):
        start = local_ts(TODAY, 23, 59, 0)
        tomorrow = TODAY + timedelta(days=1)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "test.db"
        seed = db.init_db(path)
        db.add_schedule(seed, "Midnight med", 1, "00:01")
        seed.close()
        runtime, connection = self.make_runtime(db_path=path, start=start)
        runtime.step(self.clock.now())
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM doses").fetchone()[0], 0)
        self.advance(runtime, 70)  # cross midnight
        row = connection.execute("SELECT * FROM doses").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["due_ts"], local_ts(tomorrow, 0, 1))

    def test_add_schedule_is_idempotent(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        connection = db.init_db(Path(temporary.name) / "test.db")
        self.addCleanup(connection.close)
        args = parse_schedule("8:00:Aspirin:1")
        self.assertEqual(args, ("08:00", "Aspirin", 1))
        first = db.ensure_schedule(connection, "Aspirin", 1, "08:00")
        second = db.ensure_schedule(connection, "Aspirin", 1, "08:00")
        self.assertTrue(first[1])
        self.assertFalse(second[1])
        self.assertEqual(first[0], second[0])
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM schedules").fetchone()[0], 1)

    def test_migration_adds_enabled_and_days_columns(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "old.db"
        old = sqlite3.connect(path)
        old.executescript(
            """
            CREATE TABLE schedules (id INTEGER PRIMARY KEY AUTOINCREMENT,
                medication TEXT NOT NULL, slot INTEGER NOT NULL, time_hhmm TEXT NOT NULL);
            INSERT INTO schedules (medication, slot, time_hhmm) VALUES ('Aspirin', 1, '08:00');
            """
        )
        old.commit()
        old.close()
        connection = db.init_db(path)
        self.addCleanup(connection.close)
        schedule = db.schedule_to_dict(db.get_schedule(connection, 1))
        self.assertTrue(schedule["enabled"])
        self.assertEqual(schedule["days"], [])
        db.init_db(path).close()  # running the migration twice is harmless


class ScheduleCommandTests(RuntimeTestCase):
    """Schedule CRUD through control_queue, as the dashboard does it."""

    def command(self, runtime, **command):
        reply = __import__("queue").Queue()
        runtime.control_queue.put({**command, "reply": reply})
        runtime.step(self.clock.now())
        return reply.get_nowait()

    def test_create_update_delete(self):
        runtime, connection = self.make_runtime()
        changed = []
        runtime.bus.subscribe(SCHEDULE_CHANGED, changed.append)
        result = self.command(
            runtime,
            type="schedule_create",
            data={"medication": "Aspirin", "slot": 1, "time_hhmm": "18:00", "days": [], "enabled": True},
        )
        self.assertEqual(result["status"], 201)
        schedule_id = result["schedule"]["id"]
        doses = connection.execute("SELECT * FROM doses WHERE schedule_id = ?", (schedule_id,)).fetchall()
        self.assertEqual(len(doses), 1)
        self.assertEqual(doses[0]["due_ts"], local_ts(TODAY, 18))

        result = self.command(
            runtime, type="schedule_update", id=schedule_id, data={"time_hhmm": "19:30", "medication": "Ibuprofen"}
        )
        self.assertEqual(result["status"], 200)
        doses = connection.execute("SELECT * FROM doses WHERE schedule_id = ?", (schedule_id,)).fetchall()
        self.assertEqual([(d["medication"], d["due_ts"]) for d in doses], [("Ibuprofen", local_ts(TODAY, 19, 30))])

        result = self.command(runtime, type="schedule_update", id=schedule_id, data={"enabled": False})
        self.assertFalse(result["schedule"]["enabled"])
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM doses").fetchone()[0], 0)

        self.command(runtime, type="schedule_update", id=schedule_id, data={"enabled": True})
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM doses").fetchone()[0], 1)

        result = self.command(runtime, type="schedule_delete", id=schedule_id)
        self.assertEqual(result["status"], 200)
        self.assertIsNone(db.get_schedule(connection, schedule_id))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM doses").fetchone()[0], 0)

        self.assertEqual([c["action"] for c in changed], ["created", "updated", "updated", "updated", "deleted"])
        info = [a for a in self.alerts if a["level"] == "info" and a["message"].startswith("Schedule")]
        self.assertEqual(len(info), 5)

        self.assertEqual(self.command(runtime, type="schedule_delete", id=schedule_id)["status"], 404)
        self.assertEqual(
            self.command(runtime, type="schedule_update", id=999, data={"slot": 2})["status"], 404
        )
        bad = self.command(runtime, type="schedule_create", data={"medication": "", "slot": 1, "time_hhmm": "8"})
        self.assertEqual(bad["status"], 400)

    def test_rebuild_never_touches_active_or_past_doses(self):
        runtime, connection = self.make_runtime()
        now = self.clock.now()
        schedule_id = db.add_schedule(connection, "Aspirin", 1, "18:00")
        active = connection.execute(
            "INSERT INTO doses (schedule_id, medication, slot, due_ts, status) VALUES (?, 'Aspirin', 1, ?, 'active')",
            (schedule_id, now - 60),
        ).lastrowid
        taken = connection.execute(
            "INSERT INTO doses (schedule_id, medication, slot, due_ts, status) VALUES (?, 'Aspirin', 1, ?, 'taken')",
            (schedule_id, now - 3600),
        ).lastrowid
        future = connection.execute(
            "INSERT INTO doses (schedule_id, medication, slot, due_ts, status) VALUES (?, 'Aspirin', 1, ?, 'pending')",
            (schedule_id, local_ts(TODAY, 18)),
        ).lastrowid
        connection.commit()
        self.command(runtime, type="schedule_update", id=schedule_id, data={"medication": "Paracetamol"})
        self.assertEqual(db.get_dose(connection, active)["medication"], "Aspirin")
        self.assertEqual(self.status(connection, active), "active")
        self.assertEqual(self.status(connection, taken), "taken")
        self.assertIsNone(db.get_dose(connection, future))
        new = connection.execute(
            "SELECT * FROM doses WHERE schedule_id = ? AND status = 'pending'", (schedule_id,)
        ).fetchall()
        self.assertEqual([d["medication"] for d in new], ["Paracetamol"])

    def test_acknowledge_notification(self):
        runtime, connection = self.make_runtime(load_fail_prob=1)
        dose_id = db.create_dose_now(connection, "Test medicine", 1, self.clock.now())
        self.advance(runtime, 100, lambda: self.status(connection, dose_id) == "failed")
        runtime.step(self.clock.now())
        warning = next(a for a in self.alerts if a["level"] == "warning")
        self.assertEqual(warning["reason"], "load_failed")
        result = self.command(runtime, type="ack_notification", id=warning["id"])
        self.assertEqual(result["status"], 200)
        row = connection.execute("SELECT acked FROM notifications WHERE id = ?", (warning["id"],)).fetchone()
        self.assertEqual(row["acked"], 1)
        snap = next(n for n in runtime.snapshot["notifications"] if n["id"] == warning["id"])
        self.assertTrue(snap["acked"])
        self.assertEqual(self.command(runtime, type="ack_notification", id="nope")["status"], 404)


class ValidationTests(unittest.TestCase):
    def test_valid(self):
        clean = validate_schedule({"medication": " Aspirin ", "slot": "2", "time_hhmm": "07:05", "days": ["Mon", "fri"]})
        self.assertEqual(clean, {"medication": "Aspirin", "slot": 2, "time_hhmm": "07:05", "days": ["mon", "fri"], "enabled": True})

    def test_invalid(self):
        cases = [
            ({"medication": "", "slot": 1, "time_hhmm": "08:00"}, "medication"),
            ({"medication": "A", "slot": "x", "time_hhmm": "08:00"}, "slot"),
            ({"medication": "A", "slot": 1.5, "time_hhmm": "08:00"}, "slot"),
            ({"medication": "A", "slot": True, "time_hhmm": "08:00"}, "slot"),
            ({"medication": "A", "slot": 1, "time_hhmm": "8:00"}, "time_hhmm"),
            ({"medication": "A", "slot": 1, "time_hhmm": "24:00"}, "time_hhmm"),
            ({"medication": "A", "slot": 1, "time_hhmm": "08:00", "days": ["xyz"]}, "days"),
            ({"medication": "A", "slot": 1, "time_hhmm": "08:00", "enabled": "yes"}, "enabled"),
            ({"medication": "A", "slot": 1, "time_hhmm": "08:00", "colour": "red"}, "body"),
        ]
        for data, field in cases:
            with self.subTest(data=data):
                with self.assertRaises(ValidationError) as raised:
                    validate_schedule(data)
                self.assertIn(field, raised.exception.errors)
        with self.assertRaises(ValidationError):
            validate_schedule({}, partial=True)


class DashboardApiTests(RuntimeTestCase):
    """Flask API against a Runtime running in a background thread."""

    def setUp(self):
        self.broadcaster = Broadcaster()
        self.runtime, self.connection = self.make_runtime(load_fail_prob=1, broadcaster=self.broadcaster)
        self.stop = threading.Event()
        self.lock = threading.Lock()

        def loop():
            while not self.stop.is_set():
                self.clock.t += self.STEP
                self.runtime.step(self.clock.t)
                time.sleep(0.002)

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.stop.set)
        app = create_app(
            self.runtime.get_snapshot, self.runtime.control_queue, self.db_path, self.broadcaster, keepalive=0.2
        )
        self.client = app.test_client()

    def test_pages_render(self):
        for path, marker in (
            ("/", b'data-page="home"'),
            ("/caregiver", b'id="schedule-form"'),
            ("/patient", b'id="confirm-button"'),
            ("/robot", b'id="state-sequence"'),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(marker, response.data, path)
            if path != "/":
                self.assertIn(b'aria-current="page"', response.data, path)

    def test_schedule_crud_api(self):
        response = self.client.post("/schedules", json={"medication": "Aspirin", "slot": 1, "time_hhmm": "23:30"})
        self.assertEqual(response.status_code, 201, response.get_json())
        schedule_id = response.get_json()["schedule"]["id"]
        listed = self.client.get("/schedules").get_json()
        self.assertEqual([s["id"] for s in listed], [schedule_id])
        self.assertEqual(listed[0]["days"], [])
        self.assertTrue(listed[0]["enabled"])

        response = self.client.put(f"/schedules/{schedule_id}", json={"days": ["mon", "tue"], "enabled": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["schedule"]["days"], ["mon", "tue"])

        self.assertEqual(self.client.delete(f"/schedules/{schedule_id}").status_code, 200)
        self.assertEqual(self.client.delete(f"/schedules/{schedule_id}").status_code, 404)
        self.assertEqual(self.client.get("/schedules").get_json(), [])

    def test_schedule_api_validation_errors(self):
        for body in (
            {"medication": "Aspirin", "slot": 1, "time_hhmm": "25:00"},
            {"medication": "Aspirin", "slot": "one", "time_hhmm": "08:00"},
            {"medication": "   ", "slot": 1, "time_hhmm": "08:00"},
            {"slot": 1, "time_hhmm": "08:00"},
        ):
            response = self.client.post("/schedules", json=body)
            self.assertEqual(response.status_code, 400, body)
            self.assertIn("errors", response.get_json())
        response = self.client.post("/schedules", data="not json", content_type="text/plain")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.put("/schedules/1", json={}).status_code, 400)
        self.assertEqual(self.client.put("/schedules/1", json={"slot": 2}).status_code, 404)

    def test_doses_history_ack_and_state(self):
        self.assertEqual(self.client.post("/trigger").status_code, 200)
        deadline = time.monotonic() + 10
        history = []
        while time.monotonic() < deadline:
            history = self.client.get("/doses?status=taken,missed,failed").get_json()
            if history:
                break
            time.sleep(0.05)
        self.assertEqual(history[0]["status"], "failed")
        self.assertEqual(history[0]["reason"], "load_failed")
        self.assertEqual(self.client.get("/doses?status=bogus").status_code, 400)

        notifications = self.client.get("/notifications").get_json()
        warning = next(n for n in notifications if n["level"] == "warning")
        self.assertFalse(warning["acked"])
        response = self.client.post(f"/notifications/{warning['id']}/ack")
        self.assertEqual(response.status_code, 200)
        notifications = self.client.get("/notifications").get_json()
        self.assertTrue(next(n for n in notifications if n["id"] == warning["id"])["acked"])
        self.assertEqual(self.client.post("/notifications/unknown/ack").status_code, 404)
        self.assertEqual(self.client.post("/notifications/bad id!/ack").status_code, 400)

        state = self.client.get("/state").get_json()
        for key in ("robot_state", "battery", "active_dose", "alerts", "cycle", "notifications"):
            self.assertIn(key, state)

    def test_stream_sends_state_and_notifications(self):
        response = self.client.get("/stream", buffered=False)
        self.assertEqual(response.mimetype, "text/event-stream")
        chunks = iter(response.response)
        self.assertTrue(next(chunks).startswith(b"retry:"))
        self.assertTrue(next(chunks).startswith(b"event: state\n"))
        self.client.post("/schedules", json={"medication": "Aspirin", "slot": 1, "time_hhmm": "23:45"})
        seen = []
        for _ in range(200):
            chunk = next(chunks)
            seen.append(chunk.split(b"\n", 1)[0])
            if chunk.startswith(b"event: notification\n"):
                break
        self.assertIn(b"event: notification", seen)
        self.assertIn(b"event: schedules", seen)
        response.close()


if __name__ == "__main__":
    unittest.main()

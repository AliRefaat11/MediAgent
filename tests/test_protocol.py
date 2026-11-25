"""Protocol and failure-path tests (pytest).

Run from the project root:  .venv/bin/python -m pytest -q
Everything runs on LocalBus with a manual clock except the tests marked
``mqtt``, which need a broker on localhost:1883 and are skipped otherwise.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend import db
from backend import protocol as p
from backend.config import Config
from backend.core import Clock, LocalBus, MqttBus
from backend.runtime import Runtime
from backend.simulator import SimDevices

ROOT = Path(__file__).resolve().parent.parent
ALL_TOPICS = list(p.TOPICS)


class ManualClock:
    def __init__(self, start):
        self.t = start
        self.speed = 1

    def now(self):
        return self.t


class Harness:
    STEP = 0.25

    def __init__(self, tmp_path, config=None, **sim):
        from datetime import datetime

        noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
        self.clock = ManualClock(noon)
        self.connection = db.init_db(tmp_path / "test.db")
        sim.setdefault("travel_time", 8)
        sim.setdefault("seed", 1)
        self.config = config or Config(
            load_timeout=3, reminder_interval=4, confirm_timeout=40, delivery_timeout=50,
            max_start_delay=8, critical_repeat_interval=20,
        )
        self.runtime = Runtime(self.connection, self.config, self.clock, **sim)
        self.log = []
        for topic in ALL_TOPICS:
            self.runtime.bus.subscribe(topic, lambda m, t=topic: self.log.append((t, m)))
        self.step()

    # helpers ---------------------------------------------------------------
    def step(self):
        self.runtime.step(self.clock.t)

    def advance(self, seconds, until=None):
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.t += self.STEP
            self.step()
            if until is not None and until():
                return True
        return until is None

    def dose(self, medication="Test medicine", slot=1):
        return db.create_dose_now(self.connection, medication, slot, self.clock.now())

    def status(self, dose_id):
        return db.get_dose(self.connection, dose_id)["status"]

    def messages(self, topic, **match):
        return [m for t, m in self.log if t == topic and all(m.get(k) == v for k, v in match.items())]

    def notifications(self, level=None):
        return [m for m in self.messages(p.NOTIFY_CAREGIVER) if level is None or m["level"] == level]

    def failure_reasons(self, dose_id):
        return [m["reason"] for m in self.messages(p.FAILURE, dose_id=dose_id)]

    def sim(self, device, action, **extra):
        self.runtime.bus.publish(p.SIM_CONTROL, p.envelope("test", self.clock.now(),
                                                           {"device": device, "action": action, **extra}))
        self.step()

    def robot_idle(self):
        return self.runtime.robot.state in ("IDLE", "CHARGING")


@pytest.fixture
def make(tmp_path):
    created = []

    def factory(**kwargs):
        directory = tmp_path / f"h{len(created)}"
        directory.mkdir()
        harness = Harness(directory, **kwargs)
        created.append(harness)
        return harness

    yield factory
    for harness in created:
        harness.connection.close()


# ------------------------------------------------------------------ envelope


def test_every_message_uses_the_v1_envelope(make):
    h = make(confirm_prob=1)
    dose_id = h.dose()
    assert h.advance(300, lambda: h.status(dose_id) == "taken" and h.runtime.robot.state == "IDLE")
    assert h.log
    for topic, message in h.log:
        assert p.is_valid(message), (topic, message)
        assert message["v"] == 1 and isinstance(message["ts"], float | int)
    # Hardware replies echo the correlation id.
    for message in h.messages(p.ROBOT_EVENT) + h.messages(p.DISPENSER_STATUS):
        assert message["dose_id"] == dose_id
    sources = {m["source"] for _, m in h.log}
    assert {p.SCHEDULING, p.DELIVERY, p.REMINDER, p.ESCALATION, p.ROBOT, p.DISPENSER} <= sources


def test_bus_drops_duplicate_msg_ids():
    bus = LocalBus()
    got = []
    bus.subscribe("t", got.append)
    message = p.envelope("x", 0, {"a": 1})
    bus.publish("t", message)
    bus.publish("t", dict(message))  # a QoS 1 redelivery
    bus.publish("t", p.envelope("x", 0, {"a": 1}))
    bus.pump()
    assert len(got) == 2
    assert bus.duplicates_dropped == 1


def test_duplicate_confirmation_closes_the_dose_once(make):
    h = make(confirm_prob=0)
    dose_id = h.dose()
    assert h.advance(60, lambda: h.runtime.robot.state == "WAITING_CONFIRM")
    confirm = p.envelope(p.ROBOT, h.clock.now(), {"event": "confirmed", "method": "button"}, dose_id)
    h.runtime.bus.publish(p.ROBOT_EVENT, confirm)
    h.runtime.bus.publish(p.ROBOT_EVENT, dict(confirm))
    h.advance(2)
    assert h.status(dose_id) == "taken"
    assert len(h.messages(p.DOSE_DONE, dose_id=dose_id)) == 1


def test_retained_state_reaches_late_subscribers():
    bus = LocalBus()
    bus.publish(p.ROBOT_STATE, p.envelope(p.ROBOT, 0, {"state": "IDLE", "battery": 80}))
    bus.pump()
    got = []
    bus.subscribe(p.ROBOT_STATE, got.append)
    bus.pump()
    assert [m["state"] for m in got] == ["IDLE"]


# ----------------------------------------------------------------- happy paths


def test_tray_lid_confirms_the_dose(make):
    h = make(confirm_prob=1, confirm_method="lid")
    dose_id = h.dose()
    assert h.advance(200, lambda: h.status(dose_id) == "taken")
    done = h.messages(p.DOSE_DONE, dose_id=dose_id)
    assert done[0]["result"] == "taken" and done[0]["method"] == "lid"
    assert any("tray lid" in n["message"] for n in h.notifications("info"))


def test_dispenser_reports_loading_state(make):
    h = make(confirm_prob=1)
    h.dose()
    h.advance(10)
    states = [m["state"] for m in h.messages(p.DISPENSER_STATE)]
    assert "LOADING" in states and states[-1] == "IDLE"
    assert h.messages(p.DISPENSER_STATE)[-1]["slots"]["1"] == 19


# ------------------------------------------------------------- failure paths


def test_load_failure_retries_then_fails(make):
    h = make(load_fail_prob=1)
    dose_id = h.dose()
    assert h.advance(100, lambda: h.status(dose_id) == "failed")
    commands = h.messages(p.DISPENSER_COMMAND, dose_id=dose_id)
    assert [c["attempt"] for c in commands] == [1, 2, 3]  # load_retries = 2
    assert h.failure_reasons(dose_id) == ["load_failed"]
    assert h.notifications("warning")[0]["requires_ack"] is True


def test_empty_slot_fails_without_loading(make):
    h = make()
    h.sim("dispenser", "refill", slot=2, count=0)
    h.advance(2)
    dose_id = h.dose(slot=2)
    assert h.advance(20, lambda: h.status(dose_id) == "failed")
    assert h.failure_reasons(dose_id) == ["slot_empty"]
    assert not h.messages(p.DISPENSER_COMMAND, dose_id=dose_id)


def test_robot_offline_last_will_and_unavailable(make):
    h = make()
    h.sim("robot", "power_off")
    h.advance(2)
    assert h.messages(p.ROBOT_STATE)[-1]["state"] == "OFFLINE"  # the Last Will
    assert h.runtime.snapshot["robot_state"] == "OFFLINE"
    dose_id = h.dose()
    assert h.advance(30, lambda: h.status(dose_id) == "failed")
    assert h.failure_reasons(dose_id) == ["robot_unavailable"]
    offline = [n for n in h.notifications("warning") if n["reason"] == "robot_offline"]
    assert len(offline) == 1 and offline[0]["dose_id"] is None


def test_low_battery_blocks_the_start(make):
    h = make()
    h.sim("robot", "set_battery", value=20)
    h.advance(2)
    dose_id = h.dose()
    assert h.advance(30, lambda: h.status(dose_id) == "failed")
    assert h.failure_reasons(dose_id) == ["robot_unavailable"]
    assert not h.messages(p.DOSE_LOADED, dose_id=dose_id)


def test_low_battery_during_delivery_returns_to_dock(make):
    h = make(travel_time=60)
    h.config.delivery_timeout = 200
    dose_id = h.dose()
    assert h.advance(30, lambda: h.runtime.robot.state == "DELIVERING")
    h.sim("robot", "set_battery", value=15.2)  # drains ~0.03 %/s while driving
    assert h.advance(200, lambda: h.status(dose_id) == "failed" and h.robot_idle())
    assert h.failure_reasons(dose_id) == ["low_battery"]
    actions = [c["action"] for c in h.messages(p.ROBOT_COMMAND, dose_id=dose_id)]
    assert actions[0] == "deliver" and actions[-1] == "return"


def test_obstacle_limit_fails_with_path_blocked(make):
    h = make(obstacle_prob=1, obstacle_s=20)
    dose_id = h.dose()
    assert h.advance(200, lambda: h.status(dose_id) == "failed" and h.robot_idle())
    assert h.failure_reasons(dose_id) == ["path_blocked"]


def test_delivery_timeout(make):
    h = make(obstacle_prob=1, obstacle_s=1000, config=Config(
        load_timeout=3, max_start_delay=8, delivery_timeout=20, obstacle_limit=1000))
    dose_id = h.dose()
    assert h.advance(100, lambda: h.status(dose_id) == "failed")
    assert h.failure_reasons(dose_id) == ["delivery_timeout"]


def test_lost_line_stops_the_robot(make):
    h = make(travel_time=30)
    dose_id = h.dose()
    assert h.advance(30, lambda: h.runtime.robot.state == "DELIVERING")
    h.sim("robot", "lost_line")
    assert h.advance(5, lambda: h.status(dose_id) == "failed")
    assert h.failure_reasons(dose_id) == ["lost_line"]
    assert h.messages(p.ROBOT_COMMAND, dose_id=dose_id)[-1]["action"] == "stop"
    h.advance(1)
    assert h.runtime.robot.state == "EMERGENCY_STOP"


def test_emergency_stop_then_recovery(make):
    h = make(travel_time=30, confirm_prob=1)
    first = h.dose()
    assert h.advance(30, lambda: h.runtime.robot.state == "DELIVERING")
    h.sim("robot", "estop")
    assert h.advance(5, lambda: h.status(first) == "failed")
    assert h.failure_reasons(first) == ["emergency_stop"]
    assert h.runtime.robot.state == "EMERGENCY_STOP"
    # Only the delivery failure warns; the state change does not warn twice.
    assert [n["reason"] for n in h.notifications("warning")] == ["emergency_stop"]
    h.sim("robot", "release_estop")
    assert h.advance(60, h.robot_idle)
    second = h.dose()
    assert h.advance(200, lambda: h.status(second) == "taken")


def test_heartbeat_loss_during_delivery(make):
    h = make(travel_time=60)
    dose_id = h.dose()
    assert h.advance(30, lambda: h.runtime.robot.state == "DELIVERING")
    h.sim("robot", "heartbeat_off")
    assert h.advance(30, lambda: h.status(dose_id) == "failed")
    assert h.failure_reasons(dose_id) == ["robot_offline"]
    assert any(n["reason"] == "robot_offline" and n["dose_id"] is None for n in h.notifications("warning"))
    h.sim("robot", "heartbeat_on")
    h.advance(3)
    assert any("back online" in n["message"] for n in h.notifications("info"))


def test_dispenser_offline_fails_due_dose(make):
    h = make()
    h.sim("dispenser", "power_off")
    dose_id = h.dose()
    assert h.advance(40, lambda: h.status(dose_id) == "failed")
    assert h.failure_reasons(dose_id) == ["dispenser_offline"]


# ------------------------------------------------------------ notifications


def test_critical_repeats_until_acknowledged(make):
    h = make(confirm_prob=0)
    dose_id = h.dose()
    assert h.advance(200, lambda: h.status(dose_id) == "missed")
    critical = h.notifications("critical")
    assert len(critical) == 1 and critical[0]["requires_ack"]
    notification_id = critical[0]["notification_id"]
    h.advance(45)  # repeat interval is 20 s
    repeats = [n for n in h.notifications("critical") if n["notification_id"] == notification_id]
    assert [n["repeat"] for n in repeats] == [0, 1, 2]
    # Repeats re-announce on the dashboard but are stored once.
    rows = h.connection.execute("SELECT COUNT(*) FROM notifications WHERE id = ?", (notification_id,))
    assert rows.fetchone()[0] == 1

    reply = __import__("queue").Queue()
    h.runtime.control_queue.put({"type": "ack_notification", "id": notification_id, "reply": reply})
    h.step()
    assert reply.get_nowait()["status"] == 200
    assert h.messages(p.CAREGIVER_ACK)[-1]["notification_id"] == notification_id
    h.advance(60)
    repeats = [n for n in h.notifications("critical") if n["notification_id"] == notification_id]
    assert len(repeats) == 3
    row = h.connection.execute("SELECT acked FROM notifications WHERE id = ?", (notification_id,)).fetchone()
    assert row["acked"] == 1


def test_warning_needs_ack_but_does_not_repeat(make):
    h = make(load_fail_prob=1)
    dose_id = h.dose()
    assert h.advance(100, lambda: h.status(dose_id) == "failed")
    h.advance(100)
    warnings = h.notifications("warning")
    assert len(warnings) == 1 and warnings[0]["requires_ack"]


def test_info_levels(make):
    h = make(confirm_prob=1)
    dose_id = h.dose()
    assert h.advance(200, lambda: h.status(dose_id) == "taken")
    for note in h.notifications("info"):
        assert note["requires_ack"] is False


def test_sim_control_rejects_unknown_actions(make):
    h = make()
    reply = __import__("queue").Queue()
    h.runtime.control_queue.put({"type": "sim", "device": "robot", "action": "self_destruct", "reply": reply})
    h.step()
    assert reply.get_nowait()["status"] == 400


# -------------------------------------------------------------- real broker


def _broker_up(host="localhost", port=1883):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


mqtt = pytest.mark.skipif(not _broker_up(), reason="no MQTT broker on localhost:1883")


@mqtt
def test_full_cycle_over_mqtt(tmp_path):
    clock = Clock(speed=20)
    agents_bus = MqttBus("localhost", client_id="test-agents")
    devices = SimDevices.over_mqtt("localhost", 1883, clock, confirm_prob=1, travel_time=20, seed=3)
    connection = db.init_db(tmp_path / "mqtt.db")
    runtime = Runtime(connection, Config(), clock, agents_bus, devices=devices)
    try:
        dose_id = db.create_dose_now(connection, "MQTT med", 1, clock.now())
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            runtime.step(clock.now())
            if db.get_dose(connection, dose_id)["status"] == "taken":
                break
            time.sleep(0.02)
        assert db.get_dose(connection, dose_id)["status"] == "taken"
    finally:
        devices.close(clock)
        agents_bus.close()
        connection.close()


@mqtt
def test_last_will_marks_a_crashed_robot_offline():
    script = (
        "import sys, time; sys.path.insert(0, %r)\n"
        "from backend.core import Clock\n"
        "from backend.simulator import SimDevices\n"
        "c = Clock(1); d = SimDevices.over_mqtt('localhost', 1883, c)\n"
        "while True:\n    d.step(c.now()); time.sleep(0.05)\n" % str(ROOT)
    )
    process = subprocess.Popen([sys.executable, "-c", script])
    clock = Clock(1)
    bus = MqttBus("localhost", client_id="test-lwt")
    states = []
    bus.subscribe(p.ROBOT_STATE, lambda m: states.append(m["state"]))
    try:
        deadline = time.monotonic() + 10
        while "IDLE" not in states and time.monotonic() < deadline:
            bus.pump()
            time.sleep(0.05)
        assert "IDLE" in states
        process.kill()  # no clean disconnect: the broker must publish the will
        deadline = time.monotonic() + 25  # keepalive 10 s x 1.5
        while (not states or states[-1] != "OFFLINE") and time.monotonic() < deadline:
            bus.pump()
            time.sleep(0.1)
        assert states[-1] == "OFFLINE"
    finally:
        process.kill()
        bus.close()

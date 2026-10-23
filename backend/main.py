import argparse
import json
import queue
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import db
from backend.agents import (
    CAREGIVER_ALERT,
    DOSE_DONE,
    DOSE_LOADED,
    DELIVERY_STARTED,
    ROBOT_COMMAND,
    ROBOT_EVENT,
    ROBOT_STATE,
    EscalationAgent,
    DeliveryAgent,
    ReminderAgent,
    SchedulingAgent,
)
from backend.config import Config, demo_config
from backend.core import Clock, Context, LocalBus, MqttBus
from backend.simulator import DispenserSim, RobotSim


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "mediagent.db"


def parse_schedule(value):
    try:
        hour_text, minute_text, medication, slot_text = value.split(":")
        time_hhmm = f"{hour_text}:{minute_text}"
        hour, minute = (int(part) for part in time_hhmm.split(":"))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
        return time_hhmm, medication, int(slot_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "schedule must be HH:MM:MEDICATION:SLOT"
        ) from exc


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="MediAgent medication demo")
    parser.add_argument("--speed", type=float)
    parser.add_argument("--demo-doses", type=int, default=0)
    parser.add_argument("--confirm-prob", type=float, default=0.5)
    parser.add_argument("--obstacle-prob", type=float, default=0)
    parser.add_argument("--obstacle-s", type=float, default=6)
    parser.add_argument("--load-fail-prob", type=float, default=0)
    parser.add_argument("--mqtt", action="store_true")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--add-schedule", type=parse_schedule)
    parser.add_argument("--dashboard", action="store_true")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args(argv)
    if args.speed is not None and args.speed <= 0:
        parser.error("--speed must be greater than zero")
    for name in ("confirm_prob", "obstacle_prob", "load_fail_prob"):
        if not 0 <= getattr(args, name) <= 1:
            parser.error(f"--{name.replace('_', '-')} must be between 0 and 1")
    if args.demo_doses < 0:
        parser.error("--demo-doses cannot be negative")
    return args


def run(argv=None):
    args = parse_args(argv)
    speed = args.speed if args.speed is not None else (5 if args.dashboard else 1)
    config = demo_config() if args.dashboard else Config()
    bus = MqttBus(args.host) if args.mqtt else LocalBus()
    clock = Clock(speed)
    connection = db.init_db(DB_PATH)
    context = Context(clock, bus, connection, config)
    db.generate_today(connection, clock.now())

    if args.add_schedule:
        time_hhmm, medication, slot = args.add_schedule
        db.add_schedule(connection, medication, slot, time_hhmm)
        db.generate_today(connection, clock.now())
    for _ in range(args.demo_doses):
        db.create_dose_now(connection, "Aspirin 100mg", 1, clock.now())

    agents = [
        SchedulingAgent(context),
        DeliveryAgent(context),
        ReminderAgent(context),
        EscalationAgent(context),
    ]
    dispenser = DispenserSim(context, args.load_fail_prob)
    robot = RobotSim(
        context,
        confirm_prob=0 if args.dashboard else args.confirm_prob,
        obstacle_prob=args.obstacle_prob,
        obstacle_s=args.obstacle_s,
    )
    snapshot = {
        "robot_state": "IDLE",
        "battery": 90.0,
        "active_dose": None,
        "alert_on": False,
        "alert_text": "",
        "alerts": [],
    }
    control_queue = queue.Queue()

    def replace_snapshot(**changes):
        nonlocal snapshot
        snapshot = {**snapshot, **changes}

    def log_message(topic, payload):
        now = clock.now()
        dose_id = payload.get("dose_id")
        kind = topic.rsplit("/", 1)[-1]
        state_changed = topic != ROBOT_STATE or payload["state"] != snapshot["robot_state"]
        if topic != ROBOT_STATE or state_changed:
            db.log_event(connection, now, dose_id, kind, json.dumps(payload))
            print(f"[{now:.1f}] {topic}: {payload}")
        if topic == DOSE_LOADED or topic == DELIVERY_STARTED:
            dose = db.get_dose(connection, dose_id)
            if topic == DELIVERY_STARTED:
                db.set_status(connection, dose_id, "active")
            replace_snapshot(
                active_dose={
                    "id": dose_id,
                    "medication": payload.get(
                        "medication", dose["medication"] if dose else "Medication"
                    ),
                }
            )
        elif topic == DOSE_DONE:
            db.set_status(connection, dose_id, payload["result"])
            replace_snapshot(active_dose=None)
        elif topic == CAREGIVER_ALERT:
            alerts = [*snapshot["alerts"], payload][-20:]
            replace_snapshot(alerts=alerts)
        elif topic == ROBOT_STATE:
            replace_snapshot(
                robot_state=payload["state"], battery=payload["battery"]
            )
        elif topic == ROBOT_COMMAND and payload["action"] == "alert":
            replace_snapshot(
                alert_on=payload["on"], alert_text=payload.get("text", "")
            )

    log_topics = (
        "mediagent/agents/dose_loaded",
        DELIVERY_STARTED,
        "mediagent/agents/arrived",
        DOSE_DONE,
        "mediagent/agents/failure",
        CAREGIVER_ALERT,
        ROBOT_STATE,
        ROBOT_COMMAND,
        ROBOT_EVENT,
    )
    for topic in log_topics:
        bus.subscribe(topic, lambda payload, topic=topic: log_message(topic, payload))

    if args.dashboard:
        from dashboard.app import create_app

        app = create_app(lambda: snapshot, control_queue, DB_PATH)
        threading.Thread(
            target=app.run,
            kwargs={"host": "127.0.0.1", "port": args.port, "use_reloader": False},
            daemon=True,
        ).start()
        print(f"Dashboard: http://localhost:{args.port}")

    try:
        while True:
            now = clock.now()
            bus.pump()
            for agent in agents:
                agent.tick(now)
            dispenser.tick(now)
            robot.tick(now)
            while True:
                try:
                    command = control_queue.get_nowait()
                except queue.Empty:
                    break
                if command == "trigger_dose":
                    db.create_dose_now(connection, "Aspirin 100mg", 1, now)
                elif command == "confirm" and robot.state == "WAITING_CONFIRM":
                    robot.press_confirm()
            bus.pump()
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("MediAgent stopped.")
    finally:
        connection.close()


if __name__ == "__main__":
    run()
import tempfile
import time
import unittest
import json
from pathlib import Path

from backend import db
from backend.agents import (
    CAREGIVER_ALERT,
    EscalationAgent,
    DeliveryAgent,
    ReminderAgent,
    ROBOT_COMMAND,
    SchedulingAgent,
)
from backend.config import Config
from backend.core import Clock, Context, LocalBus
from backend.simulator import DispenserSim, RobotSim


class ScenarioTests(unittest.TestCase):
    def run_scenario(
        self,
        confirm_prob=0,
        load_fail_prob=0,
        obstacle_prob=0,
        obstacle_s=6,
        config=None,
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        connection = db.init_db(Path(temporary.name) / "test.db")
        self.addCleanup(connection.close)
        clock = Clock(speed=100)
        bus = LocalBus()
        config = config or Config(
            load_timeout=3,
            reminder_interval=4,
            confirm_timeout=40,
            delivery_timeout=50,
            max_start_delay=8,
        )
        context = Context(clock, bus, connection, config)
        agents = [
            SchedulingAgent(context),
            DeliveryAgent(context),
            ReminderAgent(context),
            EscalationAgent(context),
        ]
        dispenser = DispenserSim(context, load_fail_prob, seed=1)
        robot = RobotSim(
            context,
            confirm_prob=confirm_prob,
            obstacle_prob=obstacle_prob,
            obstacle_s=obstacle_s,
            travel_time=8,
            seed=1,
        )
        dose_id = db.create_dose_now(connection, "Test medicine", 1, clock.now())
        alerts = []
        commands = []
        bus.subscribe(CAREGIVER_ALERT, alerts.append)
        bus.subscribe(ROBOT_COMMAND, commands.append)
        for topic in (
            "mediagent/agents/dose_loaded",
            "mediagent/agents/delivery_started",
            "mediagent/agents/arrived",
            "mediagent/agents/dose_done",
            "mediagent/agents/failure",
            CAREGIVER_ALERT,
        ):
            bus.subscribe(
                topic,
                lambda payload, topic=topic: self.log_message(
                    connection, clock, topic, payload
                ),
            )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            now = clock.now()
            bus.pump()
            for agent in agents:
                agent.tick(now)
            dispenser.tick(now)
            robot.tick(now)
            bus.pump()
            dose = db.get_dose(connection, dose_id)
            if dose["status"] in ("taken", "missed", "failed"):
                if dose["status"] in ("taken", "missed") and robot.state == "IDLE":
                    break
                if dose["status"] == "failed" and robot.state in ("IDLE", "CHARGING"):
                    break
            time.sleep(0.002)

        return connection, dose_id, alerts, commands

    @staticmethod
    def log_message(connection, clock, topic, payload):
        dose_id = payload.get("dose_id")
        db.log_event(connection, clock.now(), dose_id, topic.rsplit("/", 1)[-1], json.dumps(payload))
        if topic == "mediagent/agents/delivery_started":
            db.set_status(connection, dose_id, "active")
        elif topic == "mediagent/agents/dose_done":
            db.set_status(connection, dose_id, payload["result"])

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


if __name__ == "__main__":
    unittest.main()

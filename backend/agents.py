import uuid
from datetime import datetime

from backend import db


DISPENSER_COMMAND = "mediagent/dispenser/command"
DISPENSER_STATUS = "mediagent/dispenser/status"
ROBOT_COMMAND = "mediagent/robot/command"
ROBOT_STATE = "mediagent/robot/state"
ROBOT_EVENT = "mediagent/robot/event"
DOSE_LOADED = "mediagent/agents/dose_loaded"
DELIVERY_STARTED = "mediagent/agents/delivery_started"
ARRIVED = "mediagent/agents/arrived"
DOSE_DONE = "mediagent/agents/dose_done"
FAILURE = "mediagent/agents/failure"
CAREGIVER_ALERT = "mediagent/alert/caregiver"
# Added: published by the main loop after a schedule row is created, updated
# or deleted. Payload: {"schedule_id", "action", "medication", "slot",
# "time_hhmm", "days", "enabled"}.
SCHEDULE_CHANGED = "mediagent/schedule/changed"

# Reason attached to DOSE_DONE when a pending dose is older than the grace window.
PAST_GRACE = "past_grace_window"


class SchedulingAgent:
    """Picks due doses, loads them in the dispenser and hands them to the robot.

    A due dose is queued (left pending) while the robot is busy with another
    delivery. ``robot_unavailable`` is only reported when the robot is
    offline or its battery is too low for longer than ``max_start_delay``.
    """

    def __init__(self, ctx):
        self.ctx = ctx
        self.current = None
        self.load_started = None
        self.start_waiting = None
        self.retries = 0
        self.robot_state = None
        self.robot_battery = 0
        self.robot_seen = 0
        self.in_flight = None
        self.unavailable_since = None
        self.generated_for = None
        ctx.bus.subscribe(DISPENSER_STATUS, self.on_dispenser_status)
        ctx.bus.subscribe(ROBOT_STATE, self.on_robot_state)
        ctx.bus.subscribe(FAILURE, self.on_failure)
        ctx.bus.subscribe(DOSE_DONE, self.on_dose_done)
        ctx.bus.subscribe(SCHEDULE_CHANGED, self.on_schedule_changed)

    def on_robot_state(self, message):
        self.robot_state = message["state"]
        self.robot_battery = message["battery"]
        self.robot_seen = self.ctx.clock.now()

    def on_dispenser_status(self, message):
        if not self.current or message["dose_id"] != self.current["id"]:
            return
        if message["loaded"]:
            self.load_started = None
            self.start_waiting = self.ctx.clock.now()
        else:
            self._retry_or_fail()

    def on_failure(self, message):
        if self.current and message["dose_id"] == self.current["id"]:
            self._clear()

    def on_dose_done(self, message):
        if message["dose_id"] == self.in_flight:
            self.in_flight = None

    def on_schedule_changed(self, message):
        protect = [self.in_flight, self.current["id"] if self.current else None]
        db.rebuild_schedule_doses(
            self.ctx.db, message["schedule_id"], self.ctx.clock.now(), protect
        )

    def tick(self, now):
        self._generate_if_new_day(now)
        self._expire_stale(now)

        if not self.current:
            due = db.get_due_doses(self.ctx.db, now)
            if not due:
                self.unavailable_since = None
                return
            robot = self._robot_status(now)
            if robot == "busy":
                # Queue: leave the dose pending until the robot is free.
                self.unavailable_since = None
                return
            if robot != "ready":
                if self.unavailable_since is None:
                    self.unavailable_since = now
                elif now - self.unavailable_since >= self.ctx.config.max_start_delay:
                    self.unavailable_since = None
                    self.ctx.bus.publish(
                        FAILURE, {"dose_id": due[0]["id"], "reason": "robot_unavailable"}
                    )
                return
            self.unavailable_since = None
            self.current = dict(due[0])
            self.retries = 0
            self.start_waiting = None
            self._request_load(now)

        if self.load_started is not None and now - self.load_started >= self.ctx.config.load_timeout:
            self._retry_or_fail()
        if self.start_waiting is None:
            return

        if self._robot_status(now) == "ready":
            dose = self.current
            self.ctx.bus.publish(
                DOSE_LOADED,
                {"dose_id": dose["id"], "medication": dose["medication"]},
            )
            self.in_flight = dose["id"]
            self._clear()
        elif now - self.start_waiting >= self.ctx.config.max_start_delay:
            self._fail("robot_unavailable")

    def _robot_status(self, now):
        if now - self.robot_seen > self.ctx.config.robot_timeout:
            return "offline"
        if self.in_flight is not None or self.robot_state not in ("IDLE", "CHARGING"):
            return "busy"
        if self.robot_battery < self.ctx.config.min_battery:
            return "low_battery"
        return "ready"

    def _generate_if_new_day(self, now):
        today = datetime.fromtimestamp(now).date()
        if today == self.generated_for:
            return
        first_run = self.generated_for is None
        self.generated_for = today
        # At startup, skip schedule times already older than the grace window so
        # a late start does not invent stale doses. At midnight every dose of
        # the new day is in the future anyway.
        not_before = now - self.ctx.config.missed_grace if first_run else None
        db.generate_today(self.ctx.db, now, not_before=not_before)

    def _expire_stale(self, now):
        cutoff = now - self.ctx.config.missed_grace
        current_id = self.current["id"] if self.current else None
        for dose in db.get_due_doses(self.ctx.db, cutoff):
            if dose["id"] in (current_id, self.in_flight):
                continue
            # Mark immediately so the dose is not picked up again before the
            # DOSE_DONE message is persisted by the main loop.
            db.set_status(self.ctx.db, dose["id"], "missed")
            self.ctx.bus.publish(
                DOSE_DONE,
                {"dose_id": dose["id"], "result": "missed", "reason": PAST_GRACE},
            )

    def _request_load(self, now):
        dose = self.current
        self.load_started = now
        self.start_waiting = None
        self.ctx.bus.publish(
            DISPENSER_COMMAND,
            {
                "dose_id": dose["id"],
                "slot": dose["slot"],
                "medication": dose["medication"],
            },
        )

    def _retry_or_fail(self):
        if self.retries < self.ctx.config.load_retries:
            self.retries += 1
            self._request_load(self.ctx.clock.now())
        else:
            self._fail("load_failed")

    def _fail(self, reason):
        dose_id = self.current["id"]
        self.ctx.bus.publish(FAILURE, {"dose_id": dose_id, "reason": reason})
        self._clear()

    def _clear(self):
        self.current = None
        self.load_started = None
        self.start_waiting = None


class DeliveryAgent:
    def __init__(self, ctx):
        self.ctx = ctx
        self.dose_id = None
        self.started_at = None
        self.state = "IDLE"
        self.battery = 90
        self.obstacles = 0
        self.failed = False
        ctx.bus.subscribe(DOSE_LOADED, self.on_dose_loaded)
        ctx.bus.subscribe(ROBOT_STATE, self.on_robot_state)
        ctx.bus.subscribe(ROBOT_EVENT, self.on_robot_event)
        ctx.bus.subscribe(DOSE_DONE, self.on_dose_done)
        ctx.bus.subscribe(FAILURE, self.on_failure)

    def on_dose_loaded(self, message):
        self.dose_id = message["dose_id"]
        self.started_at = self.ctx.clock.now()
        self.obstacles = 0
        self.failed = False
        self.ctx.bus.publish(
            ROBOT_COMMAND, {"action": "deliver", "dose_id": self.dose_id}
        )
        self.ctx.bus.publish(
            DELIVERY_STARTED,
            {"dose_id": self.dose_id, "medication": message["medication"]},
        )

    def on_robot_state(self, message):
        self.state = message["state"]
        self.battery = message["battery"]

    def on_robot_event(self, message):
        if message["dose_id"] != self.dose_id:
            return
        event = message["event"]
        if event == "arrived":
            self.ctx.bus.publish(
                ARRIVED,
                {"dose_id": self.dose_id, "medication": self._medication()},
            )
        elif event == "obstacle":
            self.obstacles += 1
            if self.obstacles >= self.ctx.config.obstacle_limit:
                self._fail("path_blocked")
        elif event == "path_clear":
            self.obstacles = 0
        elif event == "lost_line":
            self._fail("lost_line")

    def on_dose_done(self, message):
        if message["dose_id"] != self.dose_id:
            return
        self.ctx.bus.publish(
            ROBOT_COMMAND, {"action": "return", "dose_id": self.dose_id}
        )
        self.ctx.bus.publish(
            ROBOT_COMMAND,
            {"action": "alert", "on": False, "dose_id": self.dose_id, "text": ""},
        )
        self.started_at = None

    def on_failure(self, message):
        if message["dose_id"] != self.dose_id:
            return
        self.failed = True
        self.ctx.bus.publish(
            ROBOT_COMMAND, {"action": "return", "dose_id": self.dose_id}
        )

    def tick(self, now):
        if self.dose_id is None or self.started_at is None or self.failed:
            return
        if self.state == "DELIVERING" and self.battery < self.ctx.config.critical_battery:
            self._fail("low_battery")
        elif (
            self.state == "DELIVERING"
            and now - self.started_at >= self.ctx.config.delivery_timeout
        ):
            self._fail("delivery_timeout")

    def _fail(self, reason):
        if self.failed:
            return
        self.failed = True
        self.ctx.bus.publish(FAILURE, {"dose_id": self.dose_id, "reason": reason})

    def _medication(self):
        dose = db.get_dose(self.ctx.db, self.dose_id)
        return dose["medication"] if dose else "Medication"


class ReminderAgent:
    def __init__(self, ctx):
        self.ctx = ctx
        self.dose_id = None
        self.medication = ""
        self.next_reminder = None
        self.repeats = 0
        ctx.bus.subscribe(ARRIVED, self.on_arrived)
        ctx.bus.subscribe(ROBOT_EVENT, self.on_robot_event)
        ctx.bus.subscribe(DOSE_DONE, self.on_dose_done)

    def on_arrived(self, message):
        self.dose_id = message["dose_id"]
        self.medication = message["medication"]
        self.repeats = 0
        self._alert(True, f"Take {self.medication}")
        self.next_reminder = (
            self.ctx.clock.now() + self.ctx.config.reminder_interval
        )

    def on_robot_event(self, message):
        if message["event"] != "confirmed" or message["dose_id"] != self.dose_id:
            return
        self._alert(False, "")
        self.ctx.bus.publish(
            DOSE_DONE, {"dose_id": self.dose_id, "result": "taken"}
        )

    def on_dose_done(self, message):
        if message["dose_id"] == self.dose_id:
            self.next_reminder = None
            if message["result"] != "taken":
                self._alert(False, "")

    def tick(self, now):
        if (
            self.next_reminder is not None
            and now >= self.next_reminder
            and self.repeats < self.ctx.config.reminder_max_repeats
        ):
            self.repeats += 1
            self._alert(True, f"Take {self.medication}")
            self.next_reminder = now + self.ctx.config.reminder_interval
        elif self.repeats >= self.ctx.config.reminder_max_repeats:
            self.next_reminder = None

    def _alert(self, on, text):
        self.ctx.bus.publish(
            ROBOT_COMMAND,
            {"action": "alert", "on": on, "dose_id": self.dose_id, "text": text},
        )


class EscalationAgent:
    """Publishes caregiver notifications on CAREGIVER_ALERT.

    Every payload has ``id``, ``ts``, ``level`` (info, warning, critical),
    ``dose_id`` and ``message``; warnings also carry ``reason``. The
    confirmation deadline starts when the robot *arrives*, so travel time does
    not eat into the patient's confirmation window.
    """

    def __init__(self, ctx):
        self.ctx = ctx
        self.doses = {}
        ctx.bus.subscribe(DELIVERY_STARTED, self.on_delivery_started)
        ctx.bus.subscribe(ARRIVED, self.on_arrived)
        ctx.bus.subscribe(DOSE_DONE, self.on_dose_done)
        ctx.bus.subscribe(FAILURE, self.on_failure)
        ctx.bus.subscribe(SCHEDULE_CHANGED, self.on_schedule_changed)

    def on_delivery_started(self, message):
        self.doses[message["dose_id"]] = {
            "medication": message["medication"],
            "deadline": None,
        }
        self._notify(
            "info", message["dose_id"], f"{message['medication']} dispatched to the patient"
        )

    def on_arrived(self, message):
        dose_id = message["dose_id"]
        dose = self.doses.setdefault(
            dose_id, {"medication": message["medication"], "deadline": None}
        )
        dose["deadline"] = self.ctx.clock.now() + self.ctx.config.confirm_timeout
        self._notify(
            "info", dose_id, f"{dose['medication']} arrived; waiting for the patient to confirm"
        )

    def on_dose_done(self, message):
        dose_id = message["dose_id"]
        dose = self.doses.pop(dose_id, None)
        if message["result"] == "taken":
            medication = dose["medication"] if dose else self._medication(dose_id)
            self._notify("info", dose_id, f"{medication} taken by the patient")
        elif message["result"] == "missed" and message.get("reason") == PAST_GRACE:
            medication = dose["medication"] if dose else self._medication(dose_id)
            self._notify(
                "critical",
                dose_id,
                f"Dose of {medication} was missed: it was not delivered before the grace window ended",
                reason=PAST_GRACE,
            )

    def on_failure(self, message):
        dose = self.doses.pop(message["dose_id"], None)
        medication = dose["medication"] if dose else self._medication(message["dose_id"])
        self._notify(
            "warning",
            message["dose_id"],
            f"{medication} delivery failed: {message['reason']}",
            reason=message["reason"],
        )
        self.ctx.bus.publish(
            DOSE_DONE, {"dose_id": message["dose_id"], "result": "failed"}
        )

    def on_schedule_changed(self, message):
        action = message.get("action", "updated")
        medication = message.get("medication") or "Medication"
        time_hhmm = message.get("time_hhmm")
        detail = f" at {time_hhmm}" if time_hhmm else ""
        if action == "updated" and message.get("enabled") is False:
            action = "updated (disabled)"
        self._notify("info", None, f"Schedule {action}: {medication}{detail}")

    def tick(self, now):
        for dose_id, dose in list(self.doses.items()):
            if dose["deadline"] is None or now < dose["deadline"]:
                continue
            self.doses.pop(dose_id)
            self._notify(
                "critical",
                dose_id,
                f"Dose of {dose['medication']} was not confirmed",
                reason="not_confirmed",
            )
            self.ctx.bus.publish(
                DOSE_DONE, {"dose_id": dose_id, "result": "missed"}
            )

    def _notify(self, level, dose_id, message, reason=None):
        payload = {
            "id": uuid.uuid4().hex,
            "ts": self.ctx.clock.now(),
            "level": level,
            "dose_id": dose_id,
            "message": message,
        }
        if reason:
            payload["reason"] = reason
        self.ctx.bus.publish(CAREGIVER_ALERT, payload)

    def _medication(self, dose_id):
        dose = db.get_dose(self.ctx.db, dose_id)
        return dose["medication"] if dose else "Dose"

    def notify(self, message):
        print(message)

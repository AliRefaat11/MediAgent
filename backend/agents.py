"""The four MediAgent agents.

Agents never call each other: they only subscribe and publish on the bus
(``backend.protocol`` lists the topics). Every agent is single-threaded and
keeps its timers in ``tick(now)``, which the main loop calls about 20 times a
second.

| Agent                       | Type                                  |
|-----------------------------|---------------------------------------|
| SchedulingAgent             | Goal-based, with internal state       |
| DeliveryAgent               | Model-based reflex (finite state m.)  |
| ReminderAgent               | Simple reflex plus timers             |
| EscalationAgent             | Utility/rule-based (Escalation &      |
|                             | Notification)                         |
"""
from __future__ import annotations

from datetime import datetime

from backend import db
from backend.core import Endpoint
from backend.protocol import *  # noqa: F401,F403  (topics/constants re-exported)
from backend.protocol import (
    ARRIVED, CAREGIVER_ACK, CMD_ALERT, CMD_DELIVER, CMD_RETURN, CMD_STOP, CRITICAL,
    DELIVERY, DELIVERY_STARTED, DELIVERY_TIMEOUT, DISPENSER_COMMAND,
    DISPENSER_OFFLINE, DISPENSER_STATE, DISPENSER_STATUS, DOSE_DONE, DOSE_LOADED,
    EMERGENCY_STOP, ESCALATION, EV_ARRIVED, EV_CONFIRMED, EV_EMERGENCY_STOP,
    EV_LOST_LINE, EV_OBSTACLE, EV_PATH_CLEAR, FAILED, FAILURE, INFO, LOAD_FAILED,
    LOST_LINE, LOW_BATTERY, MISSED, NOT_CONFIRMED, NOTIFY_CAREGIVER, PAST_GRACE,
    PATH_BLOCKED, REMINDER, ROBOT_COMMAND, ROBOT_EVENT, ROBOT_OFFLINE, ROBOT_STATE,
    ROBOT_UNAVAILABLE, SCHEDULE_CHANGED, SCHEDULING, SLOT_EMPTY, TAKEN, WARNING,
    new_msg_id,
)

# Kept for code written against the first protocol draft.
CAREGIVER_ALERT = NOTIFY_CAREGIVER


class Agent:
    source = "agent"

    def __init__(self, ctx):
        self.ctx = ctx
        self.out = Endpoint(ctx.bus, ctx.clock, self.source)

    def send(self, topic, payload=None, dose_id=None):
        return self.out.send(topic, payload, dose_id)

    def tick(self, now):  # pragma: no cover - overridden
        pass


class DeviceWatch:
    """Heartbeat model of one device built from its retained state topic."""

    def __init__(self, timeout):
        self.timeout = timeout
        self.state = None
        self.last = None   # message
        self.seen = None   # local receive time

    def update(self, message, now):
        self.last = message
        self.state = message.get("state")
        self.seen = now

    def online(self, now):
        return (
            self.seen is not None
            and self.state != "OFFLINE"
            and message_online(self.last)
            and now - self.seen <= self.timeout
        )


def message_online(message):
    return message is None or message.get("online", True) is not False


# =========================================================== Scheduling


class SchedulingAgent(Agent):
    """Goal: every due dose is loaded and handed to a ready robot.

    Turns schedules into dated doses (at startup, at midnight and after every
    ``schedule/changed``), expires doses older than the grace window, commands
    the dispenser, confirms the load with retries and releases the dose
    (``agents/dose_loaded``) only when the robot is ready. A due dose waits
    (stays pending) while the robot is busy with another delivery.
    """

    source = SCHEDULING

    def __init__(self, ctx):
        super().__init__(ctx)
        self.current = None
        self.load_started = None
        self.start_waiting = None
        self.retries = 0
        self.robot = DeviceWatch(ctx.config.robot_timeout)
        self.dispenser = DeviceWatch(ctx.config.dispenser_timeout)
        self.in_flight = None
        self.unavailable_since = None
        self.generated_for = None
        ctx.bus.subscribe(DISPENSER_STATUS, self.on_dispenser_status)
        ctx.bus.subscribe(DISPENSER_STATE, self.on_dispenser_state)
        ctx.bus.subscribe(ROBOT_STATE, self.on_robot_state)
        ctx.bus.subscribe(FAILURE, self.on_failure)
        ctx.bus.subscribe(DOSE_DONE, self.on_dose_done)
        ctx.bus.subscribe(SCHEDULE_CHANGED, self.on_schedule_changed)

    # ------------------------------------------------------------ percepts

    def on_robot_state(self, message):
        self.robot.update(message, self.ctx.clock.now())

    def on_dispenser_state(self, message):
        self.dispenser.update(message, self.ctx.clock.now())

    def on_dispenser_status(self, message):
        if not self.current or message.get("dose_id") != self.current["id"]:
            return  # replies must echo dose_id; anything else is stale
        if message.get("attempt", self.retries + 1) != self.retries + 1:
            return  # reply to an attempt that already timed out
        if message.get("loaded"):
            self.load_started = None
            self.start_waiting = self.ctx.clock.now()
        elif message.get("error") == SLOT_EMPTY:
            self._fail(SLOT_EMPTY)
        else:
            self._retry_or_fail()

    def on_failure(self, message):
        if self.current and message.get("dose_id") == self.current["id"]:
            self._clear()

    def on_dose_done(self, message):
        if message.get("dose_id") == self.in_flight:
            self.in_flight = None

    def on_schedule_changed(self, message):
        # Rebuild only that schedule's future pending doses; never an active one.
        protect = [self.in_flight, self.current["id"] if self.current else None]
        db.rebuild_schedule_doses(
            self.ctx.db, message["schedule_id"], self.ctx.clock.now(), protect
        )

    # ---------------------------------------------------------------- tick

    def tick(self, now):
        self._generate_if_new_day(now)
        self._expire_stale(now)

        if not self.current:
            due = db.get_due_doses(self.ctx.db, now)
            if not due:
                self.unavailable_since = None
                return
            blocker = self._blocker(now, due[0])
            if blocker == "busy":
                self.unavailable_since = None  # queue until the robot is free
                return
            if blocker == SLOT_EMPTY:
                self.send(FAILURE, {"reason": SLOT_EMPTY, "slot": due[0]["slot"]}, due[0]["id"])
                db.set_status(self.ctx.db, due[0]["id"], FAILED)  # do not pick it again
                return
            if blocker is not None:
                if self.unavailable_since is None:
                    self.unavailable_since = now
                elif now - self.unavailable_since >= self.ctx.config.max_start_delay:
                    self.unavailable_since = None
                    self.send(FAILURE, {"reason": blocker}, due[0]["id"])
                    db.set_status(self.ctx.db, due[0]["id"], FAILED)
                return
            self.unavailable_since = None
            self.current = dict(due[0])
            self.retries = 0
            self.start_waiting = None
            self._request_load(now)

        if self.load_started is not None and now - self.load_started >= self.ctx.config.load_timeout:
            self._retry_or_fail()
        if self.current is None or self.start_waiting is None:
            return

        robot = self._robot_status(now)
        if robot == "ready":
            dose = self.current
            self.send(
                DOSE_LOADED,
                {"medication": dose["medication"], "slot": dose["slot"]},
                dose["id"],
            )
            self.in_flight = dose["id"]
            self._clear()
        elif now - self.start_waiting >= self.ctx.config.max_start_delay:
            self._fail(ROBOT_UNAVAILABLE)

    # --------------------------------------------------------------- model

    def _blocker(self, now, dose):
        """Why the next dose cannot start now, or None if it can."""
        robot = self._robot_status(now)
        if robot == "busy":
            return "busy"
        if not self.dispenser.online(now):
            return DISPENSER_OFFLINE
        slots = (self.dispenser.last or {}).get("slots") or {}
        if slots.get(str(dose["slot"])) == 0:
            return SLOT_EMPTY
        if robot != "ready":
            return ROBOT_UNAVAILABLE
        return None

    def _robot_status(self, now):
        if not self.robot.online(now):
            return "offline"
        if self.in_flight is not None or self.robot.state not in ("IDLE", "CHARGING"):
            return "busy" if self.robot.state != "EMERGENCY_STOP" else "stopped"
        battery = (self.robot.last or {}).get("battery", 0)
        if battery < self.ctx.config.min_battery:
            return "low_battery"
        return "ready"

    def _generate_if_new_day(self, now):
        today = datetime.fromtimestamp(now).date()
        if today == self.generated_for:
            return
        first_run = self.generated_for is None
        self.generated_for = today
        # At startup, skip schedule times already older than the grace window
        # so a late start does not invent stale doses.
        not_before = now - self.ctx.config.missed_grace if first_run else None
        db.generate_today(self.ctx.db, now, not_before=not_before)

    def _expire_stale(self, now):
        cutoff = now - self.ctx.config.missed_grace
        current_id = self.current["id"] if self.current else None
        for dose in db.get_due_doses(self.ctx.db, cutoff):
            if dose["id"] in (current_id, self.in_flight):
                continue
            # Mark at once so the dose is not picked again before the recorder
            # persists DOSE_DONE.
            db.set_status(self.ctx.db, dose["id"], MISSED)
            self.send(DOSE_DONE, {"result": MISSED, "reason": PAST_GRACE}, dose["id"])

    def _request_load(self, now):
        dose = self.current
        self.load_started = now
        self.start_waiting = None
        self.send(
            DISPENSER_COMMAND,
            {"action": "load", "slot": dose["slot"], "medication": dose["medication"],
             "attempt": self.retries + 1},
            dose["id"],
        )

    def _retry_or_fail(self):
        if self.retries < self.ctx.config.load_retries:
            self.retries += 1
            self._request_load(self.ctx.clock.now())
        else:
            self._fail(LOAD_FAILED)

    def _fail(self, reason):
        self.send(FAILURE, {"reason": reason}, self.current["id"])
        self._clear()

    def _clear(self):
        self.current = None
        self.load_started = None
        self.start_waiting = None


# ============================================================= Delivery


class DeliveryAgent(Agent):
    """Model-based reflex agent: a finite state machine over robot events.

    Modes: IDLE -> DISPATCHING (deliver sent, waiting for the robot to report
    DELIVERING) -> EN_ROUTE -> AT_PATIENT -> RETURNING -> IDLE. Any failure
    publishes ``agents/failure`` and sends the robot home, except when the
    robot cannot drive safely (lost line, emergency stop, offline): then it is
    told to stop and a human has to collect it.
    """

    source = DELIVERY
    ACTIVE = ("DISPATCHING", "EN_ROUTE", "AT_PATIENT")
    STOP_REASONS = (LOST_LINE, EMERGENCY_STOP, ROBOT_OFFLINE)

    def __init__(self, ctx):
        super().__init__(ctx)
        self.mode = "IDLE"
        self.dose_id = None
        self.medication = None
        self.started_at = None
        self.dispatched_at = None
        self.obstacles = 0
        self.robot = DeviceWatch(ctx.config.robot_timeout)
        ctx.bus.subscribe(DOSE_LOADED, self.on_dose_loaded)
        ctx.bus.subscribe(ROBOT_STATE, self.on_robot_state)
        ctx.bus.subscribe(ROBOT_EVENT, self.on_robot_event)
        ctx.bus.subscribe(DOSE_DONE, self.on_dose_done)
        ctx.bus.subscribe(FAILURE, self.on_failure)

    # Kept for older callers/tests.
    @property
    def state(self):
        return self.robot.state

    def on_dose_loaded(self, message):
        dose_id = message.get("dose_id")
        if self.mode in self.ACTIVE:
            self.send(FAILURE, {"reason": ROBOT_UNAVAILABLE}, dose_id)
            return
        now = self.ctx.clock.now()
        self.mode = "DISPATCHING"
        self.dose_id = dose_id
        self.medication = message.get("medication") or self._medication()
        self.started_at = now
        self.dispatched_at = now
        self.obstacles = 0
        self.send(ROBOT_COMMAND, {"action": CMD_DELIVER, "medication": self.medication}, dose_id)
        self.send(DELIVERY_STARTED, {"medication": self.medication}, dose_id)

    def on_robot_state(self, message):
        self.robot.update(message, self.ctx.clock.now())
        state = self.robot.state
        if self.mode == "DISPATCHING" and state == "DELIVERING":
            self.mode = "EN_ROUTE"
        elif self.mode in self.ACTIVE and state == "EMERGENCY_STOP":
            self._fail(EMERGENCY_STOP)
        elif self.mode in self.ACTIVE and state == "OFFLINE":
            self._fail(ROBOT_OFFLINE)
        elif self.mode == "RETURNING" and state in ("IDLE", "CHARGING"):
            self._reset()

    def on_robot_event(self, message):
        event = message.get("event")
        if event == "docked" and self.mode == "RETURNING":
            self._reset()
            return
        if message.get("dose_id") != self.dose_id or self.mode not in self.ACTIVE:
            return
        if event == EV_ARRIVED:
            self.mode = "AT_PATIENT"
            self.send(ARRIVED, {"medication": self.medication}, self.dose_id)
        elif event == EV_OBSTACLE:
            self.obstacles += 1
            if self.obstacles >= self.ctx.config.obstacle_limit:
                self._fail(PATH_BLOCKED)
        elif event == EV_PATH_CLEAR:
            self.obstacles = 0
        elif event == EV_LOST_LINE:
            self._fail(LOST_LINE)
        elif event == EV_EMERGENCY_STOP:
            self._fail(EMERGENCY_STOP)

    def on_dose_done(self, message):
        if message.get("dose_id") != self.dose_id or self.mode not in self.ACTIVE:
            return
        self.send(ROBOT_COMMAND, {"action": CMD_ALERT, "on": False, "text": ""}, self.dose_id)
        self.send(ROBOT_COMMAND, {"action": CMD_RETURN}, self.dose_id)
        self.mode = "RETURNING"

    def on_failure(self, message):
        if message.get("dose_id") != self.dose_id or self.mode not in self.ACTIVE:
            return
        reason = message.get("reason")
        if reason in self.STOP_REASONS:
            if reason != ROBOT_OFFLINE:
                self.send(ROBOT_COMMAND, {"action": CMD_STOP, "reason": reason}, self.dose_id)
            self._reset()  # a person has to collect the robot
        else:
            self.send(ROBOT_COMMAND, {"action": CMD_ALERT, "on": False, "text": ""}, self.dose_id)
            self.send(ROBOT_COMMAND, {"action": CMD_RETURN}, self.dose_id)
            self.mode = "RETURNING"

    def tick(self, now):
        if self.mode not in self.ACTIVE:
            return
        battery = (self.robot.last or {}).get("battery", 100)
        if not self.robot.online(now):
            self._fail(ROBOT_OFFLINE)
        elif self.mode == "DISPATCHING" and now - self.dispatched_at >= self.ctx.config.start_timeout:
            self._fail(ROBOT_UNAVAILABLE)
        elif self.mode == "EN_ROUTE" and battery < self.ctx.config.critical_battery:
            self._fail(LOW_BATTERY)
        elif self.mode == "EN_ROUTE" and now - self.started_at >= self.ctx.config.delivery_timeout:
            self._fail(DELIVERY_TIMEOUT)

    def _fail(self, reason):
        if self.mode not in self.ACTIVE:
            return
        # on_failure (via the bus) moves the FSM on, the same way as for a
        # failure published by another agent.
        self.send(FAILURE, {"reason": reason}, self.dose_id)

    def _reset(self):
        self.mode = "IDLE"
        self.dose_id = None
        self.medication = None
        self.started_at = None
        self.dispatched_at = None
        self.obstacles = 0

    def _medication(self):
        dose = db.get_dose(self.ctx.db, self.dose_id) if self.dose_id is not None else None
        return dose["medication"] if dose else "Medication"


# ============================================================= Reminder


class ReminderAgent(Agent):
    """Simple reflex agent with timers.

    Rule 1: robot arrived -> alert (buzzer, LED, LCD text).
    Rule 2: every ``reminder_interval`` while unconfirmed -> alert again, at
            most ``reminder_max_repeats`` times.
    Rule 3: ``confirmed`` (button or tray-lid sensor) -> alert off, dose taken.
    """

    source = REMINDER

    def __init__(self, ctx):
        super().__init__(ctx)
        self.dose_id = None
        self.medication = ""
        self.next_reminder = None
        self.repeats = 0
        ctx.bus.subscribe(ARRIVED, self.on_arrived)
        ctx.bus.subscribe(ROBOT_EVENT, self.on_robot_event)
        ctx.bus.subscribe(DOSE_DONE, self.on_dose_done)
        ctx.bus.subscribe(FAILURE, self.on_failure)

    def on_arrived(self, message):
        self.dose_id = message.get("dose_id")
        self.medication = message.get("medication") or "your medicine"
        self.repeats = 0
        self._alert(True, f"Take {self.medication}")
        self.next_reminder = self.ctx.clock.now() + self.ctx.config.reminder_interval

    def on_robot_event(self, message):
        if (
            message.get("event") != EV_CONFIRMED
            or self.dose_id is None
            or message.get("dose_id") != self.dose_id
        ):
            return
        dose_id = self.dose_id
        self._alert(False, "")
        self._stop()
        self.send(DOSE_DONE, {"result": TAKEN, "method": message.get("method", "button")}, dose_id)

    def on_dose_done(self, message):
        if message.get("dose_id") == self.dose_id:
            if message.get("result") != TAKEN:
                self._alert(False, "")
            self._stop()

    def on_failure(self, message):
        if message.get("dose_id") == self.dose_id:
            self._stop()

    def tick(self, now):
        if self.next_reminder is None or now < self.next_reminder:
            return
        if self.repeats >= self.ctx.config.reminder_max_repeats:
            self.next_reminder = None  # stay quiet; Escalation owns the deadline
            return
        self.repeats += 1
        self._alert(True, f"Take {self.medication}", repeat=self.repeats)
        self.next_reminder = now + self.ctx.config.reminder_interval

    def _stop(self):
        self.dose_id = None
        self.next_reminder = None

    def _alert(self, on, text, repeat=0):
        self.send(
            ROBOT_COMMAND,
            {"action": CMD_ALERT, "on": on, "text": text, "repeat": repeat},
            self.dose_id,
        )


# ================================================ Escalation & Notification


class EscalationAgent(Agent):
    """Escalation & Notification: decides severity, notifies, closes doses.

    Levels (``notify/caregiver``):

    * info: dose dispatched, arrived, taken, schedule changed, device back
      online. No ack needed.
    * warning: delivery or load failed, device offline, emergency stop.
      Needs an ack (``caregiver/ack``).
    * critical: dose not confirmed before the deadline, or missed because of
      the grace window. Needs an ack and is re-sent every
      ``critical_repeat_interval`` until acknowledged.

    It also closes doses: ``failed`` on any ``agents/failure`` and ``missed``
    when the confirmation deadline (which starts at *arrival*) passes.
    """

    source = ESCALATION
    DEVICES = {"robot": ROBOT_STATE, "dispenser": DISPENSER_STATE}

    def __init__(self, ctx):
        super().__init__(ctx)
        self.doses = {}
        self.closed = set()
        self.pending_acks = {}
        self.started = ctx.clock.now()
        self.devices = {
            "robot": DeviceWatch(ctx.config.robot_timeout),
            "dispenser": DeviceWatch(ctx.config.dispenser_timeout),
        }
        self.device_online = {"robot": None, "dispenser": None}
        self.robot_estopped = False
        ctx.bus.subscribe(DELIVERY_STARTED, self.on_delivery_started)
        ctx.bus.subscribe(ARRIVED, self.on_arrived)
        ctx.bus.subscribe(DOSE_DONE, self.on_dose_done)
        ctx.bus.subscribe(FAILURE, self.on_failure)
        ctx.bus.subscribe(SCHEDULE_CHANGED, self.on_schedule_changed)
        ctx.bus.subscribe(CAREGIVER_ACK, self.on_ack)
        ctx.bus.subscribe(ROBOT_STATE, lambda m: self.on_device_state("robot", m))
        ctx.bus.subscribe(DISPENSER_STATE, lambda m: self.on_device_state("dispenser", m))

    # ------------------------------------------------------------ percepts

    def on_delivery_started(self, message):
        dose_id = message.get("dose_id")
        self.doses[dose_id] = {"medication": message.get("medication"), "deadline": None}
        self.notify(INFO, f"{message.get('medication')} dispatched to the patient", dose_id)

    def on_arrived(self, message):
        dose_id = message.get("dose_id")
        dose = self.doses.setdefault(
            dose_id, {"medication": message.get("medication"), "deadline": None}
        )
        dose["deadline"] = self.ctx.clock.now() + self.ctx.config.confirm_timeout
        self.notify(INFO, f"{dose['medication']} arrived; waiting for the patient to confirm", dose_id)

    def on_dose_done(self, message):
        dose_id = message.get("dose_id")
        self.closed.add(dose_id)
        dose = self.doses.pop(dose_id, None)
        medication = dose["medication"] if dose else self._medication(dose_id)
        if message.get("result") == TAKEN:
            how = " (tray lid opened)" if message.get("method") == "lid" else ""
            self.notify(INFO, f"{medication} taken by the patient{how}", dose_id)
        elif message.get("result") == MISSED and message.get("reason") == PAST_GRACE:
            self.notify(
                CRITICAL,
                f"Dose of {medication} was missed: it was not delivered before the grace window ended",
                dose_id, reason=PAST_GRACE,
            )

    def on_failure(self, message):
        dose_id = message.get("dose_id")
        reason = message.get("reason")
        if dose_id in self.closed:
            return  # a late failure for a dose that is already closed
        dose = self.doses.pop(dose_id, None)
        medication = dose["medication"] if dose else self._medication(dose_id)
        self.notify(WARNING, f"{medication} delivery failed: {reason}", dose_id, reason=reason)
        self.closed.add(dose_id)
        self.send(DOSE_DONE, {"result": FAILED, "reason": reason}, dose_id)

    def on_schedule_changed(self, message):
        action = message.get("action", "updated")
        medication = message.get("medication") or "Medication"
        time_hhmm = message.get("time_hhmm")
        detail = f" at {time_hhmm}" if time_hhmm else ""
        if action == "updated" and message.get("enabled") is False:
            action = "updated (disabled)"
        self.notify(INFO, f"Schedule {action}: {medication}{detail}")

    def on_ack(self, message):
        self.pending_acks.pop(message.get("notification_id"), None)

    def on_device_state(self, name, message):
        now = self.ctx.clock.now()
        self.devices[name].update(message, now)
        self._check_device(name, now)
        if name == "robot":
            estop = message.get("state") == "EMERGENCY_STOP"
            # An e-stop during a delivery is reported through agents/failure.
            if estop and not self.robot_estopped and message.get("dose_id") is None:
                self.notify(WARNING, "Robot emergency stop pressed", None, reason=EMERGENCY_STOP)
            self.robot_estopped = estop

    # ---------------------------------------------------------------- tick

    def tick(self, now):
        for dose_id, dose in list(self.doses.items()):
            if dose["deadline"] is None or now < dose["deadline"]:
                continue
            self.doses.pop(dose_id)
            self.closed.add(dose_id)
            self.notify(CRITICAL, f"Dose of {dose['medication']} was not confirmed", dose_id,
                        reason=NOT_CONFIRMED)
            self.send(DOSE_DONE, {"result": MISSED, "reason": NOT_CONFIRMED}, dose_id)
        for name in self.devices:
            self._check_device(name, now)
        self._repeat_criticals(now)

    # ------------------------------------------------------------- helpers

    def _check_device(self, name, now):
        watch = self.devices[name]
        online = watch.online(now)
        previous = self.device_online[name]
        if previous is None and not online and now - self.started < watch.timeout:
            return  # still starting up: give the device one heartbeat timeout
        if online == previous:
            return
        self.device_online[name] = online
        label = name.capitalize()
        if not online:
            reason = ROBOT_OFFLINE if name == "robot" else DISPENSER_OFFLINE
            self.notify(WARNING, f"{label} is offline (no heartbeat)", None, reason=reason)
        elif previous is False:
            self.notify(INFO, f"{label} is back online")

    def _repeat_criticals(self, now):
        interval = self.ctx.config.critical_repeat_interval
        for item in self.pending_acks.values():
            if item["level"] != CRITICAL or now < item["next_repeat"]:
                continue
            item["repeat"] += 1
            item["next_repeat"] = now + interval
            self.send(NOTIFY_CAREGIVER, {**item["payload"], "repeat": item["repeat"]},
                      item["payload"]["dose_id"])
            self.send_external({**item["payload"], "repeat": item["repeat"]})

    def notify(self, level, message, dose_id=None, reason=None):
        """Publish one caregiver notification and remember it if it needs an ack."""
        now = self.ctx.clock.now()
        payload = {
            "notification_id": new_msg_id(),
            "level": level,
            "message": message,
            "reason": reason,
            "requires_ack": level != INFO,
            "repeat": 0,
            "dose_id": dose_id,
        }
        self.send(NOTIFY_CAREGIVER, payload, dose_id)
        if level != INFO:
            self.pending_acks[payload["notification_id"]] = {
                "level": level,
                "payload": payload,
                "repeat": 0,
                "next_repeat": now + self.ctx.config.critical_repeat_interval,
            }
        self.send_external(payload)
        return payload["notification_id"]

    def send_external(self, payload):
        """Hook for a real channel (Telegram, e-mail). Not wired up yet."""

    def _medication(self, dose_id):
        dose = db.get_dose(self.ctx.db, dose_id) if dose_id is not None else None
        return dose["medication"] if dose else "Dose"

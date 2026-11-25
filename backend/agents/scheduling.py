"""SchedulingAgent: goal-based, with internal state."""
from __future__ import annotations

from datetime import datetime

from backend import db
from backend.agents.base import Agent, DeviceWatch
from backend.protocol import (
    DISPENSER_COMMAND,
    DISPENSER_OFFLINE,
    DISPENSER_STATE,
    DISPENSER_STATUS,
    DOSE_DONE,
    DOSE_LOADED,
    FAILED,
    FAILURE,
    LOAD_FAILED,
    MISSED,
    PAST_GRACE,
    ROBOT_STATE,
    ROBOT_UNAVAILABLE,
    SCHEDULE_CHANGED,
    SCHEDULING,
    SLOT_EMPTY,
)


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

"""DeliveryAgent: model-based reflex agent (finite state machine)."""
from __future__ import annotations

from backend import db
from backend.agents.base import Agent, DeviceWatch
from backend.protocol import (
    ARRIVED,
    CMD_ALERT,
    CMD_DELIVER,
    CMD_RETURN,
    CMD_STOP,
    DELIVERY,
    DELIVERY_STARTED,
    DELIVERY_TIMEOUT,
    DOSE_DONE,
    DOSE_LOADED,
    EMERGENCY_STOP,
    EV_ARRIVED,
    EV_EMERGENCY_STOP,
    EV_LOST_LINE,
    EV_OBSTACLE,
    EV_PATH_CLEAR,
    FAILURE,
    LOST_LINE,
    LOW_BATTERY,
    PATH_BLOCKED,
    ROBOT_COMMAND,
    ROBOT_EVENT,
    ROBOT_OFFLINE,
    ROBOT_STATE,
    ROBOT_UNAVAILABLE,
)


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

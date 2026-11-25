"""EscalationAgent: utility/rule-based escalation and notification."""
from __future__ import annotations

from backend import db
from backend.agents.base import Agent, DeviceWatch
from backend.protocol import (
    ARRIVED,
    CAREGIVER_ACK,
    CRITICAL,
    DELIVERY_STARTED,
    DISPENSER_OFFLINE,
    DISPENSER_STATE,
    DOSE_DONE,
    EMERGENCY_STOP,
    ESCALATION,
    FAILED,
    FAILURE,
    INFO,
    MISSED,
    NOT_CONFIRMED,
    NOTIFY_CAREGIVER,
    PAST_GRACE,
    ROBOT_OFFLINE,
    ROBOT_STATE,
    SCHEDULE_CHANGED,
    TAKEN,
    WARNING,
    new_msg_id,
)


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

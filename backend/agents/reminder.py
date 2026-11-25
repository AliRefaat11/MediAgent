"""ReminderAgent: simple reflex agent with timers."""
from __future__ import annotations

from backend.agents.base import Agent
from backend.protocol import (
    ARRIVED,
    CMD_ALERT,
    DOSE_DONE,
    EV_CONFIRMED,
    FAILURE,
    REMINDER,
    ROBOT_COMMAND,
    ROBOT_EVENT,
    TAKEN,
)


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

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


class SchedulingAgent:
    def __init__(self, ctx):
        self.ctx = ctx
        self.current = None
        self.load_started = None
        self.start_waiting = None
        self.retries = 0
        self.robot_state = None
        self.robot_battery = 0
        self.robot_seen = 0
        ctx.bus.subscribe(DISPENSER_STATUS, self.on_dispenser_status)
        ctx.bus.subscribe(ROBOT_STATE, self.on_robot_state)
        ctx.bus.subscribe(FAILURE, self.on_failure)

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

    def tick(self, now):
        if not self.current:
            due = db.get_due_doses(self.ctx.db, now)
            if not due:
                return
            self.current = dict(due[0])
            self.retries = 0
            self.start_waiting = None
            self._request_load(now)

        if self.load_started is not None and now - self.load_started >= self.ctx.config.load_timeout:
            self._retry_or_fail()
        if self.start_waiting is None:
            return

        fresh = now - self.robot_seen <= self.ctx.config.robot_timeout
        available = self.robot_state in ("IDLE", "CHARGING") and fresh
        if available and self.robot_battery >= self.ctx.config.min_battery:
            dose = self.current
            self.ctx.bus.publish(
                DOSE_LOADED,
                {"dose_id": dose["id"], "medication": dose["medication"]},
            )
            self._clear()
        elif now - self.start_waiting >= self.ctx.config.max_start_delay:
            self._fail("robot_unavailable")

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
    def __init__(self, ctx):
        self.ctx = ctx
        self.doses = {}
        ctx.bus.subscribe(DELIVERY_STARTED, self.on_delivery_started)
        ctx.bus.subscribe(DOSE_DONE, self.on_dose_done)
        ctx.bus.subscribe(FAILURE, self.on_failure)

    def on_delivery_started(self, message):
        self.doses[message["dose_id"]] = {
            "medication": message["medication"],
            "deadline": self.ctx.clock.now() + self.ctx.config.confirm_timeout,
        }

    def on_dose_done(self, message):
        self.doses.pop(message["dose_id"], None)

    def on_failure(self, message):
        dose = self.doses.pop(message["dose_id"], None)
        medication = dose["medication"] if dose else self._medication(message["dose_id"])
        self.ctx.bus.publish(
            CAREGIVER_ALERT,
            {
                "dose_id": message["dose_id"],
                "level": "warning",
                "message": f"{medication} delivery failed: {message['reason']}",
            },
        )
        self.ctx.bus.publish(
            DOSE_DONE, {"dose_id": message["dose_id"], "result": "failed"}
        )

    def tick(self, now):
        for dose_id, dose in list(self.doses.items()):
            if now < dose["deadline"]:
                continue
            self.doses.pop(dose_id)
            self.ctx.bus.publish(
                CAREGIVER_ALERT,
                {
                    "dose_id": dose_id,
                    "level": "critical",
                    "message": f"Dose of {dose['medication']} was not confirmed",
                },
            )
            self.ctx.bus.publish(
                DOSE_DONE, {"dose_id": dose_id, "result": "missed"}
            )

    def _medication(self, dose_id):
        dose = db.get_dose(self.ctx.db, dose_id)
        return dose["medication"] if dose else "Dose"

    def notify(self, message):
        print(message)

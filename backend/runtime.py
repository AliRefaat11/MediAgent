"""The single-threaded MediAgent runtime: agents, simulators and the only DB writer.

``backend/main.py`` drives ``Runtime.step`` in a loop; the tests drive it
directly with a manual clock. The Flask dashboard never writes the DB: it
puts commands on ``control_queue`` and reads ``snapshot`` / the DB read-only.
"""
from __future__ import annotations

import json
import queue
import time
import traceback
import uuid

from backend import db
from backend.agents import DeliveryAgent, EscalationAgent, ReminderAgent, SchedulingAgent
from backend.core import Broadcaster, Context, Endpoint, LocalBus
from backend.protocol import (
    ARRIVED,
    CAREGIVER_ACK,
    DASHBOARD,
    DELIVERY_STARTED,
    DISPENSER_COMMAND,
    DISPENSER_STATE,
    DISPENSER_STATUS,
    DOSE_DONE,
    DOSE_LOADED,
    FAILURE,
    NOTIFY_CAREGIVER,
    PAST_GRACE,
    ROBOT_COMMAND,
    ROBOT_EVENT,
    ROBOT_STATE,
    SCHEDULE_CHANGED,
    SIM_CONTROL,
)
from backend.simulator import SimDevices
from backend.validation import ValidationError, validate_schedule

NOTIFICATION_LIMIT = 50
HEARTBEAT_TOPICS = (ROBOT_STATE, DISPENSER_STATE)
SIM_ACTIONS = {
    "robot": {"press_button", "open_lid", "estop", "release_estop", "lost_line",
              "heartbeat_off", "heartbeat_on", "power_off", "power_on", "set_battery"},
    "dispenser": {"heartbeat_off", "heartbeat_on", "power_off", "power_on", "refill",
                  "set_fail_prob"},
}
ACTIVE_STAGES = ("loading", "delivering", "arrived", "waiting", "taken", "missed", "returning", "charging")


class Runtime:
    def __init__(
        self,
        connection,
        config,
        clock,
        bus=None,
        *,
        broadcaster=None,
        confirm_prob=0.0,
        obstacle_prob=0.0,
        obstacle_s=6.0,
        load_fail_prob=0.0,
        travel_time=20.0,
        seed=None,
        confirm_method="button",
        devices=None,
        verbose=False,
    ):
        """``devices``: None runs the simulated dispenser and robot on this
        bus; a ``SimDevices`` instance is stepped as given (e.g. with its own
        MQTT connections); False means real or external devices."""
        self.connection = connection
        self.clock = clock
        self.bus = bus or LocalBus()
        self.config = config
        self.verbose = verbose
        self.ctx = Context(clock, self.bus, connection, config)
        self.broadcaster = broadcaster or Broadcaster()
        self.control_queue: queue.Queue = queue.Queue()
        self.agents = [
            SchedulingAgent(self.ctx),
            DeliveryAgent(self.ctx),
            ReminderAgent(self.ctx),
            EscalationAgent(self.ctx),
        ]
        self.scheduler = self.agents[0]
        self.escalation = self.agents[3]
        self.out = Endpoint(self.bus, clock, DASHBOARD)
        if devices is None:
            devices = SimDevices(
                self.bus, self.bus, clock,
                load_fail_prob=load_fail_prob, confirm_prob=confirm_prob,
                obstacle_prob=obstacle_prob, obstacle_s=obstacle_s,
                travel_time=travel_time, seed=seed, confirm_method=confirm_method,
            )
        self.devices = devices or None
        self.dispenser = self.devices.dispenser if self.devices else None
        self.robot = self.devices.robot if self.devices else None
        self._robot_seen = None
        notifications = db.recent_notifications(connection, NOTIFICATION_LIMIT)
        self.snapshot = {
            "robot_state": "IDLE",
            "battery": 90.0,
            "dispenser": {"state": "UNKNOWN", "online": False, "slots": {}},
            "active_dose": None,
            "alert_on": False,
            "alert_text": "",
            "alerts": [n for n in reversed(notifications) if n["level"] != "info"][-20:],
            "cycle": None,
            "queued": 0,
            "next_dose": None,
            "notifications": notifications,
            "now": clock.now(),
        }
        self._last_broadcast = None
        self._last_broadcast_wall = 0.0
        self._last_refresh = None
        self._pending_replies = []
        self._started = clock.now()

        for topic in (
            DISPENSER_COMMAND,
            DISPENSER_STATUS,
            DISPENSER_STATE,
            DOSE_LOADED,
            DELIVERY_STARTED,
            ARRIVED,
            DOSE_DONE,
            FAILURE,
            NOTIFY_CAREGIVER,
            CAREGIVER_ACK,
            ROBOT_STATE,
            ROBOT_COMMAND,
            ROBOT_EVENT,
            SCHEDULE_CHANGED,
        ):
            self.bus.subscribe(topic, lambda payload, topic=topic: self._on_message(topic, payload))

    # ------------------------------------------------------------ main loop

    def get_snapshot(self):
        return self.snapshot

    def step(self, now=None):
        now = self.clock.now() if now is None else now
        self.bus.pump()
        for agent in self.agents:
            agent.tick(now)
        if self.devices:
            # Devices sharing our bus were already pumped above.
            self.devices.step(now, pump=self.devices.buses != [self.bus])
        self._drain_control(now)
        self.bus.pump()
        self._refresh(now)
        # Replies go out after the pump so schedule rebuilds and resulting
        # notifications are already applied when the HTTP request returns.
        for reply, result in self._pending_replies:
            reply.put(result)
        self._pending_replies = []
        self._broadcast_state()

    # ------------------------------------------------------------- snapshot

    def _replace(self, **changes):
        # Swap the whole dict so the Flask thread always reads a consistent copy.
        self.snapshot = {**self.snapshot, **changes}

    def _refresh(self, now):
        if self._last_refresh is not None and now - self._last_refresh < 1:
            return
        self._last_refresh = now
        due = db.get_due_doses(self.connection, now)
        upcoming = self.connection.execute(
            "SELECT id, medication, slot, due_ts FROM doses WHERE status = 'pending' "
            "AND due_ts > ? ORDER BY due_ts, id LIMIT 1",
            (now,),
        ).fetchone()
        changes = {
            "queued": len(due),
            "next_dose": dict(upcoming) if upcoming else None,
            "now": now,
        }
        timeout = self.config.robot_timeout
        if self._robot_seen is None or now - self._robot_seen > timeout:
            if self.snapshot["robot_state"] != "OFFLINE" and (self._robot_seen is not None or now - self._started > timeout):
                changes["robot_state"] = "OFFLINE"
        dispenser_watch = self.scheduler.dispenser
        if self.snapshot["dispenser"]["online"] and not dispenser_watch.online(now):
            changes["dispenser"] = {**self.snapshot["dispenser"], "online": False, "state": "OFFLINE"}
        cycle = self.snapshot["cycle"]
        if due and (cycle is None or cycle["stage"] not in ACTIVE_STAGES):
            first = due[0]
            if cycle is None or cycle["dose_id"] != first["id"]:
                changes["cycle"] = self._new_cycle(first["id"], first["medication"], "due", now)
        self._replace(**changes)

    def _broadcast_state(self):
        """Push state over SSE: at once for workflow changes, else at most 1/s.

        Streamed state omits the notification list; notifications travel as
        their own "notification"/"ack" events (and /state still has them).
        """
        if self.snapshot is self._last_broadcast or self.broadcaster.client_count == 0:
            return
        wall = time.monotonic()
        previous = self._last_broadcast or {}
        important = any(
            self.snapshot.get(key) != previous.get(key)
            for key in ("robot_state", "cycle", "active_dose", "alert_on", "alert_text", "queued", "next_dose")
        )
        if not important and wall - self._last_broadcast_wall < 1.0:
            return
        self._last_broadcast = self.snapshot
        self._last_broadcast_wall = wall
        state = {key: value for key, value in self.snapshot.items() if key != "notifications"}
        self.broadcaster.publish("state", state)

    @staticmethod
    def _new_cycle(dose_id, medication, stage, now):
        return {
            "dose_id": dose_id,
            "medication": medication,
            "stage": stage,
            "result": None,
            "reason": None,
            "since": now,
        }

    def _cycle(self, dose_id, now, medication=None, **changes):
        cycle = self.snapshot["cycle"]
        if cycle is None or cycle["dose_id"] != dose_id:
            if medication is None:
                dose = db.get_dose(self.connection, dose_id) if dose_id is not None else None
                medication = dose["medication"] if dose else "Medication"
            cycle = self._new_cycle(dose_id, medication, "due", now)
        updated = {**cycle, **changes}
        if updated.get("stage") != cycle.get("stage"):
            updated["since"] = now
        self._replace(cycle=updated)

    # ------------------------------------------------------- bus listeners

    def _on_message(self, topic, payload):
        now = self.clock.now()
        dose_id = payload.get("dose_id")
        kind = topic.rsplit("/", 1)[-1]

        if topic != DISPENSER_COMMAND:
            if topic == ROBOT_STATE:
                state_changed = payload.get("state") != self.snapshot["robot_state"]
            elif topic == DISPENSER_STATE:
                state_changed = payload.get("state") != self.snapshot["dispenser"]["state"]
            else:
                state_changed = True
            if state_changed:
                db.log_event(self.connection, now, dose_id, kind, json.dumps(payload))
                if self.verbose:
                    print(f"[{now:.1f}] {topic}: {payload}")

        if topic == DISPENSER_COMMAND:
            self._cycle(dose_id, now, payload.get("medication"), stage="loading")
        elif topic in (DOSE_LOADED, DELIVERY_STARTED):
            dose = db.get_dose(self.connection, dose_id)
            medication = payload.get("medication", dose["medication"] if dose else "Medication")
            if topic == DELIVERY_STARTED:
                db.set_status(self.connection, dose_id, "active")
                self._cycle(dose_id, now, medication, stage="delivering")
                self.broadcaster.publish("doses", {"dose_id": dose_id})
            self._replace(active_dose={"id": dose_id, "medication": medication})
        elif topic == ARRIVED:
            self._cycle(dose_id, now, payload.get("medication"), stage="arrived")
        elif topic == FAILURE:
            self._cycle(dose_id, now, stage="failed", result="failed", reason=payload.get("reason"))
        elif topic == DOSE_DONE:
            self._on_dose_done(payload, now)
        elif topic == NOTIFY_CAREGIVER:
            self._on_notification(payload, now)
        elif topic == CAREGIVER_ACK:
            self._on_ack(payload, now)
        elif topic == ROBOT_STATE:
            state = payload.get("state", "OFFLINE")
            if state != "OFFLINE":
                self._robot_seen = now
            self._replace(robot_state=state, battery=payload.get("battery", self.snapshot["battery"]))
            self._advance_return(state, now)
        elif topic == DISPENSER_STATE:
            self._replace(dispenser={
                "state": payload.get("state", "OFFLINE"),
                "online": payload.get("online", True) is not False and payload.get("state") != "OFFLINE",
                "slots": payload.get("slots", self.snapshot["dispenser"].get("slots", {})),
            })
        elif topic == ROBOT_COMMAND and payload.get("action") == "alert":
            self._replace(alert_on=bool(payload.get("on")), alert_text=payload.get("text", ""))
            cycle = self.snapshot["cycle"]
            if payload.get("on") and cycle and cycle["dose_id"] == dose_id and cycle["stage"] in ("arrived", "waiting"):
                self._cycle(dose_id, now, stage="waiting")
        elif topic == SCHEDULE_CHANGED:
            self.broadcaster.publish("schedules", payload)
            self.broadcaster.publish("doses", {})

    def _on_dose_done(self, payload, now):
        dose_id = payload["dose_id"]
        result = payload["result"]
        db.set_status(self.connection, dose_id, result)
        active = self.snapshot["active_dose"]
        if active is None or active["id"] == dose_id:
            self._replace(active_dose=None)
        if payload.get("reason") != PAST_GRACE:
            cycle = self.snapshot["cycle"]
            if result == "taken":
                self._cycle(dose_id, now, stage="taken", result="taken")
            elif result == "missed":
                self._cycle(dose_id, now, stage="missed", result="missed", reason="not_confirmed")
            elif cycle is None or cycle["dose_id"] != dose_id or cycle["result"] is None:
                self._cycle(dose_id, now, stage="failed", result="failed", reason=payload.get("reason"))
        self.broadcaster.publish("doses", {"dose_id": dose_id})

    def _advance_return(self, robot_state, now):
        cycle = self.snapshot["cycle"]
        if cycle is None or cycle["result"] is None:
            return
        stage = cycle["stage"]
        if robot_state == "RETURNING" and stage in ("taken", "missed", "failed"):
            self._cycle(cycle["dose_id"], now, stage="returning")
        elif robot_state == "CHARGING" and stage == "returning":
            self._cycle(cycle["dose_id"], now, stage="charging")
        elif robot_state == "IDLE" and stage in ("returning", "charging"):
            self._cycle(cycle["dose_id"], now, stage="complete")

    def _on_notification(self, payload, now):
        notification_id = str(payload.get("notification_id") or payload.get("id") or uuid.uuid4().hex)
        repeat = int(payload.get("repeat") or 0)
        if repeat:
            # A critical that is still unacknowledged: same id, announce again.
            existing = next((n for n in self.snapshot["notifications"] if n["id"] == notification_id), None)
            if existing is not None and not existing["acked"]:
                updated = {**existing, "repeat": repeat}
                self._replace(notifications=[
                    updated if n["id"] == notification_id else n for n in self.snapshot["notifications"]
                ])
                self.broadcaster.publish("notification", updated)
            return
        notification = {
            "id": notification_id,
            "ts": payload.get("ts", now),
            "level": payload.get("level", "info"),
            "dose_id": payload.get("dose_id"),
            "message": payload.get("message", ""),
            "reason": payload.get("reason"),
            "acked": False,
            "acked_ts": None,
            "repeat": 0,
        }
        db.save_notification(self.connection, notification)
        notifications = [notification, *self.snapshot["notifications"]][:NOTIFICATION_LIMIT]
        changes = {"notifications": notifications}
        if notification["level"] != "info":
            # Backward-compatible list of warning/critical alerts.
            changes["alerts"] = [*self.snapshot["alerts"], notification][-20:]
        self._replace(**changes)
        self.broadcaster.publish("notification", notification)

    def _on_ack(self, payload, now):
        notification_id = str(payload.get("notification_id"))
        if not db.ack_notification(self.connection, notification_id, now):
            return
        notifications = [
            {**n, "acked": True, "acked_ts": n.get("acked_ts") or now} if n["id"] == notification_id else n
            for n in self.snapshot["notifications"]
        ]
        self._replace(notifications=notifications)
        self.broadcaster.publish("ack", {"id": notification_id})

    # ---------------------------------------------------- control commands

    def _drain_control(self, now):
        while True:
            try:
                command = self.control_queue.get_nowait()
            except queue.Empty:
                return
            if command == "confirm":
                # The patient page's button stands in for the robot's button.
                self.out.send(SIM_CONTROL, {"device": "robot", "action": "press_button"})
                continue
            if not isinstance(command, dict):
                continue
            reply = command.get("reply")
            try:
                result = self._handle(command, now)
            except ValidationError as exc:
                result = {"ok": False, "status": 400, "errors": exc.errors}
            except Exception as exc:  # keep the loop alive; report to the caller
                traceback.print_exc()
                result = {"ok": False, "status": 500, "errors": {"server": str(exc)}}
            if reply is not None:
                self._pending_replies.append((reply, result))

    def _handle(self, command, now):
        kind = command.get("type")
        if kind == "schedule_create":
            data = validate_schedule(command.get("data"))
            schedule_id = db.add_schedule(self.connection, **data)
            schedule = db.schedule_to_dict(db.get_schedule(self.connection, schedule_id))
            self._schedule_changed("created", schedule)
            return {"ok": True, "status": 201, "schedule": schedule}
        if kind == "schedule_update":
            schedule_id = int(command["id"])
            if db.get_schedule(self.connection, schedule_id) is None:
                return {"ok": False, "status": 404, "errors": {"id": "schedule not found"}}
            data = validate_schedule(command.get("data"), partial=True)
            db.update_schedule(self.connection, schedule_id, data)
            schedule = db.schedule_to_dict(db.get_schedule(self.connection, schedule_id))
            self._schedule_changed("updated", schedule)
            return {"ok": True, "status": 200, "schedule": schedule}
        if kind == "schedule_delete":
            schedule_id = int(command["id"])
            row = db.get_schedule(self.connection, schedule_id)
            if row is None:
                return {"ok": False, "status": 404, "errors": {"id": "schedule not found"}}
            schedule = db.schedule_to_dict(row)
            db.delete_schedule(self.connection, schedule_id)
            self._schedule_changed("deleted", schedule)
            return {"ok": True, "status": 200, "schedule": schedule}
        if kind == "ack_notification":
            notification_id = str(command["id"])
            row = self.connection.execute(
                "SELECT dose_id FROM notifications WHERE id = ?", (notification_id,)
            ).fetchone()
            if row is None:
                return {"ok": False, "status": 404, "errors": {"id": "notification not found"}}
            # Escalation stops repeating it; the recorder (_on_ack) writes the DB.
            self.out.send(CAREGIVER_ACK, {"notification_id": notification_id}, row["dose_id"])
            return {"ok": True, "status": 200, "id": notification_id}
        if kind == "sim":
            device, action = command.get("device"), command.get("action")
            if action not in SIM_ACTIONS.get(device, ()):
                return {"ok": False, "status": 400, "errors": {"action": f"unknown action {device}/{action}"}}
            extra = {k: command[k] for k in ("value", "slot", "count") if k in command}
            self.out.send(SIM_CONTROL, {"device": device, "action": action, **extra})
            return {"ok": True, "status": 200}
        return {"ok": False, "status": 400, "errors": {"type": f"unknown command {kind!r}"}}

    def _schedule_changed(self, action, schedule):
        self.out.send(
            SCHEDULE_CHANGED,
            {
                "schedule_id": schedule["id"],
                "action": action,
                "medication": schedule["medication"],
                "slot": schedule["slot"],
                "time_hhmm": schedule["time_hhmm"],
                "days": schedule["days"],
                "enabled": schedule["enabled"],
            },
        )

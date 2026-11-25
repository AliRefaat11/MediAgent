"""MediAgent message protocol: topics, QoS/retain rules and the JSON envelope.

Every message on the bus is a JSON object with this envelope::

    {"v": 1, "msg_id": "<uuid hex>", "ts": <seconds>, "source": "delivery_agent",
     "dose_id": 42, ...payload}

``dose_id`` is the correlation id (``None`` when a message is not about a
dose) and hardware replies must echo it. ``msg_id`` lets receivers drop
duplicates, which MQTT QoS 1 can deliver.

Message kinds: ``command`` (request an action), ``event`` (something
happened), ``state`` (periodic heartbeat, retained) and ``failure`` (a task
cannot complete, with a ``reason``).
"""
from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass

PROTOCOL_VERSION = 1
PREFIX = "mediagent/"

# ------------------------------------------------------------------ topics

# Device layer (agents <-> ESP32 firmware or the simulators)
DISPENSER_COMMAND = PREFIX + "dispenser/command"   # Scheduling -> dispenser
DISPENSER_STATUS = PREFIX + "dispenser/status"     # dispenser -> Scheduling (load result)
DISPENSER_STATE = PREFIX + "dispenser/state"       # dispenser heartbeat, retained + Last Will
ROBOT_COMMAND = PREFIX + "robot/command"           # Delivery/Reminder -> robot
ROBOT_EVENT = PREFIX + "robot/event"               # robot -> Delivery/Reminder
ROBOT_STATE = PREFIX + "robot/state"               # robot heartbeat, retained + Last Will

# Agent layer
DOSE_LOADED = PREFIX + "agents/dose_loaded"            # Scheduling -> Delivery
DELIVERY_STARTED = PREFIX + "agents/delivery_started"  # Delivery -> Escalation, recorder
ARRIVED = PREFIX + "agents/arrived"                    # Delivery -> Reminder, Escalation
DOSE_DONE = PREFIX + "agents/dose_done"                # Reminder/Escalation -> all
FAILURE = PREFIX + "agents/failure"                    # Scheduling/Delivery -> Escalation, Delivery

# Caregiver layer
SCHEDULE_CHANGED = PREFIX + "schedule/changed"   # dashboard API (via main loop) -> Scheduling
NOTIFY_CAREGIVER = PREFIX + "notify/caregiver"   # Escalation -> dashboard (SSE)
CAREGIVER_ACK = PREFIX + "caregiver/ack"         # dashboard -> Escalation

# Simulation only: dashboard buttons that act on the simulated hardware
# (press the confirm button, open the tray lid, e-stop, cut the heartbeat).
# Real firmware ignores this topic.
SIM_CONTROL = PREFIX + "sim/control"


@dataclass(frozen=True)
class TopicSpec:
    qos: int
    retain: bool = False


TOPICS: dict[str, TopicSpec] = {
    DISPENSER_COMMAND: TopicSpec(1),
    DISPENSER_STATUS: TopicSpec(1),
    DISPENSER_STATE: TopicSpec(0, retain=True),
    ROBOT_COMMAND: TopicSpec(1),
    ROBOT_EVENT: TopicSpec(1),
    ROBOT_STATE: TopicSpec(0, retain=True),
    DOSE_LOADED: TopicSpec(1),
    DELIVERY_STARTED: TopicSpec(1),
    ARRIVED: TopicSpec(1),
    DOSE_DONE: TopicSpec(1),
    FAILURE: TopicSpec(1),
    SCHEDULE_CHANGED: TopicSpec(1),
    NOTIFY_CAREGIVER: TopicSpec(1),
    CAREGIVER_ACK: TopicSpec(1),
    SIM_CONTROL: TopicSpec(1),
}


def topic_spec(topic: str) -> TopicSpec:
    return TOPICS.get(topic, TopicSpec(1))


# --------------------------------------------------------------- vocabulary

# Message sources
SCHEDULING = "scheduling_agent"
DELIVERY = "delivery_agent"
REMINDER = "reminder_agent"
ESCALATION = "escalation_agent"
DISPENSER = "dispenser"
ROBOT = "robot"
DASHBOARD = "dashboard_api"

# Robot firmware states. OFFLINE is only ever sent as the Last Will.
ROBOT_STATES = (
    "IDLE", "DELIVERING", "WAITING_CONFIRM", "RETURNING", "CHARGING",
    "EMERGENCY_STOP", "OFFLINE",
)
# Dispenser firmware states.
DISPENSER_STATES = ("IDLE", "LOADING", "ERROR", "OFFLINE")

# robot/command actions
CMD_DELIVER, CMD_RETURN, CMD_ALERT, CMD_STOP = "deliver", "return", "alert", "stop"
# robot/event events
EV_ARRIVED = "arrived"
EV_CONFIRMED = "confirmed"        # carries "method": "button" | "lid"
EV_OBSTACLE = "obstacle"
EV_PATH_CLEAR = "path_clear"
EV_LOST_LINE = "lost_line"
EV_DOCKED = "docked"
EV_EMERGENCY_STOP = "emergency_stop"

# Failure reasons (agents/failure "reason")
LOAD_FAILED = "load_failed"
SLOT_EMPTY = "slot_empty"
DISPENSER_OFFLINE = "dispenser_offline"
ROBOT_UNAVAILABLE = "robot_unavailable"
ROBOT_OFFLINE = "robot_offline"
PATH_BLOCKED = "path_blocked"
LOST_LINE = "lost_line"
LOW_BATTERY = "low_battery"
DELIVERY_TIMEOUT = "delivery_timeout"
EMERGENCY_STOP = "emergency_stop"

# dose_done results / reasons
TAKEN, MISSED, FAILED = "taken", "missed", "failed"
NOT_CONFIRMED = "not_confirmed"
PAST_GRACE = "past_grace_window"

# Notification levels
INFO, WARNING, CRITICAL = "info", "warning", "critical"


# ----------------------------------------------------------------- envelope


def new_msg_id() -> str:
    return uuid.uuid4().hex


def envelope(source: str, ts: float, payload: dict | None = None, dose_id=None) -> dict:
    """Wrap ``payload`` in the v1 envelope. Payload keys never override it."""
    payload = dict(payload or {})
    dose_id = payload.pop("dose_id", dose_id)
    for key in ("v", "msg_id", "ts", "source"):
        payload.pop(key, None)
    return {
        "v": PROTOCOL_VERSION,
        "msg_id": new_msg_id(),
        "ts": ts,
        "source": source,
        "dose_id": dose_id,
        **payload,
    }


def is_valid(message) -> bool:
    return (
        isinstance(message, dict)
        and message.get("v") == PROTOCOL_VERSION
        and isinstance(message.get("msg_id"), str)
        and "source" in message
        and "dose_id" in message
    )


class Deduper:
    """Remembers the last ``size`` msg_ids; ``seen()`` is True for repeats."""

    def __init__(self, size: int = 2048):
        self.size = size
        self._ids: OrderedDict[str, None] = OrderedDict()

    def seen(self, msg_id: str) -> bool:
        if msg_id in self._ids:
            self._ids.move_to_end(msg_id)
            return True
        self._ids[msg_id] = None
        if len(self._ids) > self.size:
            self._ids.popitem(last=False)
        return False

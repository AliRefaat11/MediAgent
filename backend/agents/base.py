"""Shared building blocks for the agents."""
from __future__ import annotations

from backend.core import Endpoint


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

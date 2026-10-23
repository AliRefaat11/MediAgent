from __future__ import annotations

import random

from backend.agents import (
    DISPENSER_COMMAND,
    DISPENSER_STATUS,
    ROBOT_COMMAND,
    ROBOT_EVENT,
    ROBOT_STATE,
)


class DispenserSim:
    def __init__(self, ctx, load_fail_prob: float = 0, seed: int | None = None):
        self.ctx = ctx
        self.load_fail_prob = load_fail_prob
        self.random = random.Random(seed)
        self.pending = []
        ctx.bus.subscribe(DISPENSER_COMMAND, self.on_command)

    def on_command(self, message):
        self.pending.append(
            {
                "dose_id": message["dose_id"],
                "due": self.ctx.clock.now() + 3,
            }
        )

    def tick(self, now):
        remaining = []
        for item in self.pending:
            if now < item["due"]:
                remaining.append(item)
                continue
            self.ctx.bus.publish(
                DISPENSER_STATUS,
                {
                    "dose_id": item["dose_id"],
                    "loaded": self.random.random() >= self.load_fail_prob,
                },
            )
        self.pending = remaining


class RobotSim:
    def __init__(
        self,
        ctx,
        confirm_prob: float = 0,
        obstacle_prob: float = 0,
        obstacle_s: float = 6,
        travel_time: float = 20,
        seed: int | None = None,
    ):
        self.ctx = ctx
        self.confirm_prob = confirm_prob
        self.obstacle_prob = obstacle_prob
        self.obstacle_s = obstacle_s
        self.travel_time = travel_time
        self.random = random.Random(seed)
        self.state = "IDLE"
        self.battery = 90.0
        self.dose_id = None
        self.state_since = ctx.clock.now()
        self.last_state = 0
        self.alert_on = False
        self.alert_text = ""
        self.last_tick = ctx.clock.now()
        self.block_started = None
        self.next_obstacle = None
        self.arrival_at = None
        self.confirm_at = None
        self.return_at = None
        self.idle_at = None
        ctx.bus.subscribe(ROBOT_COMMAND, self.on_command)

    def on_command(self, message):
        action = message["action"]
        if action == "deliver":
            self.dose_id = message["dose_id"]
            self._set_state("DELIVERING")
            self.arrival_at = self.ctx.clock.now() + self.travel_time
            self.block_started = None
            self.next_obstacle = None
            if self.random.random() < self.obstacle_prob:
                blocked_at = self.ctx.clock.now() + min(3, self.travel_time / 2)
                self.block_started = blocked_at
                self.next_obstacle = blocked_at
        elif action == "return":
            if message["dose_id"] == self.dose_id:
                self._set_state("RETURNING")
                self.return_at = self.ctx.clock.now() + 20
        elif action == "alert":
            self.alert_on = message["on"]
            self.alert_text = message.get("text", "")

    def tick(self, now):
        elapsed = max(0, now - self.last_tick)
        self.last_tick = now
        moving = self.state in ("DELIVERING", "RETURNING")
        if moving:
            self.battery = max(0, self.battery - elapsed * 0.03)
        elif self.state == "CHARGING":
            self.battery = min(100, self.battery + elapsed * 0.1)

        if self.state == "DELIVERING":
            if self.block_started is not None:
                if now - self.block_started >= self.obstacle_s:
                    self.ctx.bus.publish(
                        ROBOT_EVENT, {"event": "path_clear", "dose_id": self.dose_id}
                    )
                    self.block_started = None
                    self.next_obstacle = None
                elif self.next_obstacle is not None and now >= self.next_obstacle:
                    self.ctx.bus.publish(
                        ROBOT_EVENT, {"event": "obstacle", "dose_id": self.dose_id}
                    )
                    self.next_obstacle = now + 2

            if self.block_started is None and now >= self.arrival_at:
                self._set_state("WAITING_CONFIRM")
                self.ctx.bus.publish(
                    ROBOT_EVENT, {"event": "arrived", "dose_id": self.dose_id}
                )
                if self.confirm_prob > 0 and self.random.random() < self.confirm_prob:
                    self.confirm_at = now + self.random.uniform(5, 15)

        if self.state == "WAITING_CONFIRM" and self.confirm_at and now >= self.confirm_at:
            self.press_confirm()

        if self.state == "RETURNING" and self.return_at and now >= self.return_at:
            self.ctx.bus.publish(
                ROBOT_EVENT, {"event": "docked", "dose_id": self.dose_id}
            )
            self._set_state("CHARGING")
            self.idle_at = now + 3
            self.return_at = None

        if self.state == "CHARGING" and self.idle_at and now >= self.idle_at:
            self._set_state("IDLE")
            self.idle_at = None

        if now - self.last_state >= 1:
            self.last_state = now
            self.ctx.bus.publish(
                ROBOT_STATE, {"state": self.state, "battery": round(self.battery, 1)}
            )

    def press_confirm(self):
        if self.state == "WAITING_CONFIRM":
            self.ctx.bus.publish(
                ROBOT_EVENT, {"event": "confirmed", "dose_id": self.dose_id}
            )
            self.confirm_at = None
            return True
        return False

    def _set_state(self, state):
        self.state = state
        self.state_since = self.ctx.clock.now()

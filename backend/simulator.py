"""Simulated dispenser and robot.

They speak exactly the topics and payloads the ESP32 firmware will use, and
each one is a separate *device* with its own bus endpoint (and, over MQTT,
its own connection and Last Will). Run them in the agents' process
(``backend/main.py --sim``) or on their own::

    python -m backend.simulator --mqtt --host localhost --speed 5

The dashboard's simulation buttons reach them through ``mediagent/sim/control``
(``{"device": "robot", "action": "press_button"}`` and so on), so they work
whether the simulators run in-process or in another process.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core import Clock, Endpoint, LocalBus, MqttBus  # noqa: E402
from backend.protocol import (  # noqa: E402
    CMD_ALERT, CMD_DELIVER, CMD_RETURN, CMD_STOP, DISPENSER, DISPENSER_COMMAND,
    DISPENSER_STATE, DISPENSER_STATUS, EV_ARRIVED, EV_CONFIRMED, EV_DOCKED,
    EV_EMERGENCY_STOP, EV_LOST_LINE, EV_OBSTACLE, EV_PATH_CLEAR, ROBOT,
    ROBOT_COMMAND, ROBOT_EVENT, ROBOT_STATE, SIM_CONTROL, SLOT_EMPTY, envelope,
)

HEARTBEAT_S = 1.0


def offline_message(source, clock):
    """The Last Will payload: retained OFFLINE state."""
    return envelope(source, clock.now(), {"state": "OFFLINE", "online": False})


class _Device:
    source = "device"
    state_topic = ""

    def __init__(self, bus, clock, seed=None):
        self.bus = bus
        self.clock = clock
        self.out = Endpoint(bus, clock, self.source)
        self.random = random.Random(seed)
        self.heartbeat = True
        self.powered = True
        self.last_heartbeat = None
        if hasattr(bus, "set_will"):  # LocalBus: emulate the broker's Last Will
            bus.set_will(self.source, self.state_topic, offline_message(self.source, clock))
        bus.subscribe(SIM_CONTROL, self._on_sim_control)

    def _on_sim_control(self, message):
        if message.get("device") != self.source or not self.powered and message.get("action") != "power_on":
            return
        action = message.get("action")
        if action == "heartbeat_off":
            self.heartbeat = False
        elif action == "heartbeat_on":
            self.heartbeat = True
        elif action == "power_off":
            self.power_off()
        elif action == "power_on":
            self.powered = True
            self.heartbeat = True
        else:
            self.on_sim_action(action, message)

    def on_sim_action(self, action, message):
        pass

    def power_off(self):
        """Simulate a crash: heartbeats stop and the broker fires the Last Will."""
        self.powered = False
        self.heartbeat = False
        if hasattr(self.bus, "fire_will"):
            self.bus.fire_will(self.source)

    def _heartbeat(self, now, payload):
        if not self.heartbeat:
            return
        if self.last_heartbeat is None or now - self.last_heartbeat >= HEARTBEAT_S:
            self.last_heartbeat = now
            self.out.send(self.state_topic, {"online": True, **payload})


class DispenserSim(_Device):
    """IDLE -> LOADING (load_time s) -> IDLE, or ERROR if the load failed.

    Publishes ``dispenser/status`` ``{"loaded": bool, "error"?}`` echoing
    ``dose_id`` and a retained ``dispenser/state`` heartbeat with slot levels.
    """

    source = DISPENSER
    state_topic = DISPENSER_STATE

    def __init__(self, bus, clock, load_fail_prob: float = 0, seed: int | None = None,
                 slots: dict | None = None, load_time: float = 2):
        super().__init__(bus, clock, seed)
        self.load_fail_prob = load_fail_prob
        self.load_time = load_time
        self.slots = {str(k): v for k, v in (slots or {str(n): 20 for n in range(1, 5)}).items()}
        self.state = "IDLE"
        self.queue = []
        self.loading = None
        bus.subscribe(DISPENSER_COMMAND, self.on_command)

    @property
    def pending(self):  # compatibility with older tests
        return ([self.loading] if self.loading else []) + self.queue

    def on_command(self, message):
        if not self.powered:
            return
        self.queue.append({"dose_id": message.get("dose_id"), "slot": str(message.get("slot")),
                           "attempt": message.get("attempt", 1)})

    def on_sim_action(self, action, message):
        if action == "refill":
            self.slots[str(message.get("slot", 1))] = int(message.get("count", 20))
        elif action == "set_fail_prob":
            self.load_fail_prob = float(message.get("value", 0))

    def tick(self, now):
        if not self.powered:
            return
        if self.loading is None and self.queue:
            self.loading = {**self.queue.pop(0), "done_at": now + self.load_time}
            self.state = "LOADING"
            self.last_heartbeat = None  # publish the state change at once
        if self.loading is not None and now >= self.loading["done_at"]:
            item, self.loading = self.loading, None
            slot = item["slot"]
            payload = {"attempt": item["attempt"]}
            if self.slots.get(slot, 0) <= 0:
                payload.update(loaded=False, error=SLOT_EMPTY, slot=slot)
            elif self.random.random() < self.load_fail_prob:
                payload.update(loaded=False, error="jam", slot=slot)
            else:
                self.slots[slot] -= 1
                payload.update(loaded=True, slot=slot)
            self.state = "IDLE" if payload["loaded"] else "ERROR"
            self.out.send(DISPENSER_STATUS, payload, item["dose_id"])
            self.last_heartbeat = None
        self._heartbeat(now, {"state": self.state, "slots": dict(self.slots)})


class RobotSim(_Device):
    """Robot firmware states: IDLE, DELIVERING, WAITING_CONFIRM, RETURNING,
    CHARGING, EMERGENCY_STOP (OFFLINE is only the Last Will).

    The patient confirms with the button or by opening the tray lid
    (``confirm_method``); both publish ``confirmed`` with a ``method``.
    """

    source = ROBOT
    state_topic = ROBOT_STATE

    def __init__(
        self,
        bus,
        clock,
        confirm_prob: float = 0,
        obstacle_prob: float = 0,
        obstacle_s: float = 6,
        travel_time: float = 20,
        seed: int | None = None,
        confirm_method: str = "button",
        battery: float = 90.0,
        return_time: float = 20,
    ):
        super().__init__(bus, clock, seed)
        self.confirm_prob = confirm_prob
        self.obstacle_prob = obstacle_prob
        self.obstacle_s = obstacle_s
        self.travel_time = travel_time
        self.return_time = return_time
        self.confirm_method = confirm_method
        self.state = "IDLE"
        self.battery = float(battery)
        self.dose_id = None
        self.state_since = clock.now()
        self.alert_on = False
        self.alert_text = ""
        self.lid_open = False
        self.last_tick = clock.now()
        self.block_started = None
        self.next_obstacle = None
        self.arrival_at = None
        self.confirm_at = None
        self.return_at = None
        self.idle_at = None
        self.state_before_stop = None
        bus.subscribe(ROBOT_COMMAND, self.on_command)

    # ------------------------------------------------------------ commands

    def on_command(self, message):
        if not self.powered:
            return
        action = message.get("action")
        dose_id = message.get("dose_id")
        if action == CMD_DELIVER:
            if self.state not in ("IDLE", "CHARGING"):
                return
            self.dose_id = dose_id
            self._set_state("DELIVERING")
            self.arrival_at = self.clock.now() + self.travel_time
            self.block_started = None
            self.next_obstacle = None
            if self.random.random() < self.obstacle_prob:
                blocked_at = self.clock.now() + min(3, self.travel_time / 2)
                self.block_started = blocked_at
                self.next_obstacle = blocked_at
        elif action == CMD_RETURN:
            if dose_id == self.dose_id and self.state in ("DELIVERING", "WAITING_CONFIRM"):
                self._set_state("RETURNING")
                self.return_at = self.clock.now() + self.return_time
                self.confirm_at = None
        elif action == CMD_ALERT:
            self.alert_on = bool(message.get("on"))
            self.alert_text = message.get("text", "")
        elif action == CMD_STOP:
            self._emergency_stop(physical=False)

    # ------------------------------------------------- simulated hardware

    def on_sim_action(self, action, message):
        if action == "press_button":
            self.press_confirm()
        elif action == "open_lid":
            self.open_lid()
        elif action == "estop":
            self._emergency_stop(physical=True)
        elif action == "release_estop":
            self.release_estop()
        elif action == "lost_line":
            if self.state in ("DELIVERING", "RETURNING"):
                self._event(EV_LOST_LINE)
        elif action == "set_battery":
            self.battery = float(message.get("value", self.battery))

    def press_confirm(self, method="button"):
        if self.state == "WAITING_CONFIRM" and self.powered:
            self._event(EV_CONFIRMED, method=method)
            self.confirm_at = None
            return True
        return False

    def open_lid(self):
        """Tray-lid sensor: opening the lid at the patient counts as taken."""
        self.lid_open = True
        return self.press_confirm(method="lid")

    def release_estop(self):
        if self.state != "EMERGENCY_STOP":
            return
        # Safe default after a stop: drive back to the dock.
        if self.state_before_stop in ("IDLE", "CHARGING"):
            self._set_state("IDLE")
        else:
            self._set_state("RETURNING")
            self.return_at = self.clock.now() + self.return_time
        self.state_before_stop = None

    def _emergency_stop(self, physical):
        if self.state == "EMERGENCY_STOP":
            return
        self.state_before_stop = self.state
        self._set_state("EMERGENCY_STOP")
        self.alert_on = False
        self.arrival_at = self.return_at = self.confirm_at = None
        self.block_started = self.next_obstacle = None
        if physical:
            self._event(EV_EMERGENCY_STOP)

    # ---------------------------------------------------------------- tick

    def tick(self, now):
        elapsed = max(0, now - self.last_tick)
        self.last_tick = now
        if not self.powered:
            return
        if self.state in ("DELIVERING", "RETURNING"):
            self.battery = max(0, self.battery - elapsed * 0.03)
        elif self.state == "CHARGING":
            self.battery = min(100, self.battery + elapsed * 0.1)

        if self.state == "DELIVERING":
            if self.block_started is not None:
                if now - self.block_started >= self.obstacle_s:
                    self._event(EV_PATH_CLEAR)
                    self.block_started = None
                    self.next_obstacle = None
                elif self.next_obstacle is not None and now >= self.next_obstacle:
                    self._event(EV_OBSTACLE, distance_cm=18)
                    self.next_obstacle = now + 2
            if self.block_started is None and self.arrival_at is not None and now >= self.arrival_at:
                self._set_state("WAITING_CONFIRM")
                self.lid_open = False
                self._event(EV_ARRIVED)
                if self.confirm_prob > 0 and self.random.random() < self.confirm_prob:
                    self.confirm_at = now + self.random.uniform(5, 15)

        if self.state == "WAITING_CONFIRM" and self.confirm_at and now >= self.confirm_at:
            if self.confirm_method == "lid":
                self.open_lid()
            else:
                self.press_confirm()

        if self.state == "RETURNING" and self.return_at and now >= self.return_at:
            self._event(EV_DOCKED)
            self._set_state("CHARGING")
            self.idle_at = now + 3
            self.return_at = None

        if self.state == "CHARGING" and self.idle_at and now >= self.idle_at:
            self._set_state("IDLE")
            self.idle_at = None
            self.dose_id = None

        active = self.state in ("DELIVERING", "WAITING_CONFIRM", "RETURNING", "EMERGENCY_STOP")
        self._heartbeat(now, {
            "state": self.state,
            "battery": round(self.battery, 1),
            "lid_open": self.lid_open,
            "dose_id": self.dose_id if active and self.state_before_stop not in ("IDLE", "CHARGING") else None,
        })

    def _event(self, event, **extra):
        self.out.send(ROBOT_EVENT, {"event": event, **extra}, self.dose_id)

    def _set_state(self, state):
        self.state = state
        self.state_since = self.clock.now()
        self.last_heartbeat = None  # state changes are published at once


class SimDevices:
    """The simulated dispenser and robot, each with its own bus endpoint."""

    def __init__(self, dispenser_bus, robot_bus, clock, *, load_fail_prob=0.0,
                 confirm_prob=0.0, obstacle_prob=0.0, obstacle_s=6.0,
                 travel_time=20.0, seed=None, confirm_method="button"):
        self.buses = [dispenser_bus] if dispenser_bus is robot_bus else [dispenser_bus, robot_bus]
        self.dispenser = DispenserSim(dispenser_bus, clock, load_fail_prob, seed=seed)
        self.robot = RobotSim(
            robot_bus, clock, confirm_prob=confirm_prob, obstacle_prob=obstacle_prob,
            obstacle_s=obstacle_s, travel_time=travel_time, seed=seed,
            confirm_method=confirm_method,
        )

    def step(self, now, pump=True):
        if pump:
            for bus in self.buses:
                bus.pump()
        self.dispenser.tick(now)
        self.robot.tick(now)

    @classmethod
    def over_mqtt(cls, host, port, clock, **kwargs):
        """One MQTT connection per device, each with an OFFLINE Last Will."""
        dispenser_bus = MqttBus(host, port, client_id="mediagent-dispenser-sim",
                                will=(DISPENSER_STATE, offline_message(DISPENSER, clock)))
        robot_bus = MqttBus(host, port, client_id="mediagent-robot-sim",
                            will=(ROBOT_STATE, offline_message(ROBOT, clock)))
        return cls(dispenser_bus, robot_bus, clock, **kwargs)

    def close(self, clock):
        for bus, source, topic in ((self.buses[0], DISPENSER, DISPENSER_STATE),
                                   (self.buses[-1], ROBOT, ROBOT_STATE)):
            if isinstance(bus, MqttBus):
                bus.close((topic, offline_message(source, clock)))


def run(argv=None):
    parser = argparse.ArgumentParser(description="Run the simulated MediAgent devices")
    parser.add_argument("--mqtt", action="store_true", help="connect to an MQTT broker (required)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--speed", type=float, default=1)
    parser.add_argument("--confirm-prob", type=float, default=0)
    parser.add_argument("--confirm-method", choices=("button", "lid"), default="button")
    parser.add_argument("--obstacle-prob", type=float, default=0)
    parser.add_argument("--load-fail-prob", type=float, default=0)
    parser.add_argument("--travel-time", type=float, default=20)
    args = parser.parse_args(argv)
    if not args.mqtt:
        parser.error("standalone devices need --mqtt; without a broker run backend/main.py --sim")
    clock = Clock(args.speed)
    devices = SimDevices.over_mqtt(
        args.host, args.port, clock, load_fail_prob=args.load_fail_prob,
        confirm_prob=args.confirm_prob, obstacle_prob=args.obstacle_prob,
        travel_time=args.travel_time, confirm_method=args.confirm_method,
    )
    print(f"Simulated dispenser and robot connected to {args.host}:{args.port}")
    try:
        while True:
            devices.step(clock.now())
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("Simulators stopped.")
    finally:
        devices.close(clock)


if __name__ == "__main__":
    run()

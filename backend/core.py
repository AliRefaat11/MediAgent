from __future__ import annotations

import json
import queue
import threading
import time
from typing import Callable

from backend.protocol import Deduper, envelope, topic_spec

Callback = Callable[[dict], None]


class Clock:
    def __init__(self, speed: float = 1.0):
        self.speed = speed
        self._wall_start = time.monotonic()
        self._start = time.time()

    def now(self) -> float:
        return self._start + (time.monotonic() - self._wall_start) * self.speed


class _Bus:
    """Shared subscribe/publish/pump logic.

    Messages are queued and only delivered inside ``pump()``, so every
    callback runs on the main loop thread. Messages whose ``msg_id`` was
    already delivered are dropped (QoS 1 may deliver twice).
    """

    def __init__(self):
        self._subscribers: dict[str, list[Callback]] = {}
        self._messages: queue.Queue[tuple[str, dict, Callback | None]] = queue.Queue()
        self._dedupe = Deduper()
        self.duplicates_dropped = 0

    def subscribe(self, topic: str, callback: Callback) -> None:
        self._subscribers.setdefault(topic, []).append(callback)

    def pump(self, max_rounds: int = 50) -> None:
        """Deliver queued messages, including ones published while delivering."""
        for _ in range(max_rounds):
            if self._messages.empty():
                return
            batch = []
            while True:
                try:
                    batch.append(self._messages.get_nowait())
                except queue.Empty:
                    break
            for topic, payload, only in batch:
                self._deliver(topic, payload, only)

    def _deliver(self, topic: str, payload: dict, only: Callback | None) -> None:
        msg_id = payload.get("msg_id")
        if only is None and isinstance(msg_id, str) and self._dedupe.seen(msg_id):
            self.duplicates_dropped += 1
            return
        targets = [only] if only is not None else list(self._subscribers.get(topic, []))
        for callback in targets:
            callback(payload)


class LocalBus(_Bus):
    """In-process bus with the MQTT semantics the agents rely on.

    Retained topics keep their last message and replay it to late
    subscribers; ``fire_will()`` publishes a registered Last Will, the way the
    broker does when a device disconnects without saying goodbye.
    """

    def __init__(self):
        super().__init__()
        self._retained: dict[str, dict] = {}
        self._wills: dict[str, tuple[str, dict]] = {}

    def subscribe(self, topic: str, callback: Callback) -> None:
        super().subscribe(topic, callback)
        if topic in self._retained:
            self._messages.put((topic, self._retained[topic], callback))

    def publish(self, topic: str, payload: dict, retain: bool | None = None) -> None:
        if retain if retain is not None else topic_spec(topic).retain:
            self._retained[topic] = payload
        self._messages.put((topic, payload, None))

    def set_will(self, name: str, topic: str, payload: dict) -> None:
        self._wills[name] = (topic, payload)

    def fire_will(self, name: str) -> None:
        if name in self._wills:
            topic, payload = self._wills[name]
            self.publish(topic, payload, retain=True)


class MqttBus(_Bus):
    """paho-mqtt transport. Network callbacks only queue; pump() delivers.

    QoS and retain come from ``protocol.TOPICS``. Pass ``will`` as
    ``(topic, payload)`` to register a Last Will (devices use it to publish
    OFFLINE when they drop off the network).
    """

    def __init__(self, host: str, port: int = 1883, client_id: str | None = None,
                 will: tuple[str, dict] | None = None, connect_timeout: float = 5.0):
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError("Install paho-mqtt to use --mqtt") from exc
        super().__init__()
        self._mqtt = mqtt
        self._topics: set[str] = set()
        self._connected = threading.Event()
        kwargs = {"client_id": client_id or ""}
        if hasattr(mqtt, "CallbackAPIVersion"):  # paho-mqtt >= 2.0
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, **kwargs)
        else:
            self._client = mqtt.Client(**kwargs)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=10)
        if will is not None:
            topic, payload = will
            spec = topic_spec(topic)
            self._client.will_set(topic, json.dumps(payload), qos=max(1, spec.qos), retain=True)
        self._client.connect(host, port, keepalive=10)
        self._client.loop_start()
        if not self._connected.wait(connect_timeout):
            raise RuntimeError(f"Could not connect to the MQTT broker at {host}:{port}")

    # paho callbacks (network thread) -------------------------------------
    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        failed = getattr(reason_code, "is_failure", None)
        if (failed if failed is not None else reason_code != 0):
            print(f"MQTT connection refused: {reason_code}")
            return
        for topic in list(self._topics):  # (re)subscribe after every connect
            client.subscribe(topic, qos=topic_spec(topic).qos or 1)
        self._connected.set()

    def _on_disconnect(self, *_args):
        self._connected.clear()

    def _on_message(self, _client, _userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"Ignoring invalid MQTT message on {message.topic}: {exc}")
            return
        if not isinstance(payload, dict):
            print(f"Ignoring non-object MQTT message on {message.topic}")
            return
        self._messages.put((message.topic, payload, None))

    # bus interface -------------------------------------------------------
    def subscribe(self, topic: str, callback: Callback) -> None:
        super().subscribe(topic, callback)
        if topic not in self._topics:
            self._topics.add(topic)
            self._client.subscribe(topic, qos=topic_spec(topic).qos or 1)

    def publish(self, topic: str, payload: dict, retain: bool | None = None) -> None:
        spec = topic_spec(topic)
        self._client.publish(
            topic, json.dumps(payload), qos=spec.qos,
            retain=spec.retain if retain is None else retain,
        )

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def close(self, will_payload: tuple[str, dict] | None = None) -> None:
        """Disconnect cleanly. A clean disconnect does NOT fire the Last Will,
        so devices pass their OFFLINE state here to publish it themselves."""
        if will_payload is not None:
            topic, payload = will_payload
            self._client.publish(topic, json.dumps(payload), qos=1, retain=True).wait_for_publish(2)
        self._client.disconnect()
        self._client.loop_stop()


class Endpoint:
    """What a component uses to talk: stamps the envelope with its source."""

    def __init__(self, bus, clock, source: str):
        self.bus = bus
        self.clock = clock
        self.source = source

    def send(self, topic: str, payload: dict | None = None, dose_id=None, retain=None) -> dict:
        message = envelope(self.source, self.clock.now(), payload, dose_id)
        self.bus.publish(topic, message, retain=retain)
        return message


class Broadcaster:
    """Thread-safe fan-out of (event, data) pairs to Server-Sent Events clients.

    The main loop calls publish(); each Flask /stream request owns one queue.
    Slow clients whose queue fills up are dropped rather than blocking the loop.
    """

    def __init__(self, max_queue: int = 200):
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self._max_queue = max_queue

    def subscribe(self) -> queue.Queue:
        client: queue.Queue = queue.Queue(maxsize=self._max_queue)
        with self._lock:
            self._clients.append(client)
        return client

    def unsubscribe(self, client: queue.Queue) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)

    def publish(self, event: str, data) -> None:
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.put_nowait((event, data))
            except queue.Full:
                self.unsubscribe(client)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


class Context:
    def __init__(self, clock, bus, db, config):
        self.clock = clock
        self.bus = bus
        self.db = db
        self.config = config

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Callable


class Clock:
    def __init__(self, speed: float = 1.0):
        self.speed = speed
        self._wall_start = time.monotonic()
        self._start = time.time()

    def now(self) -> float:
        return self._start + (time.monotonic() - self._wall_start) * self.speed


class LocalBus:
    def __init__(self):
        self._subscribers: dict[str, list[Callable[[dict], None]]] = {}
        self._messages: queue.Queue[tuple[str, dict]] = queue.Queue()

    def subscribe(self, topic: str, callback: Callable[[dict], None]) -> None:
        self._subscribers.setdefault(topic, []).append(callback)

    def publish(self, topic: str, payload: dict) -> None:
        self._messages.put((topic, payload))

    def pump(self) -> None:
        while True:
            try:
                topic, payload = self._messages.get_nowait()
            except queue.Empty:
                return
            for callback in self._subscribers.get(topic, []):
                callback(payload)


class MqttBus:
    """UNTESTED with a real broker. Network callbacks queue messages for pump()."""

    def __init__(self, host: str):
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError("Install paho-mqtt to use --mqtt") from exc

        self._subscribers: dict[str, list[Callable[[dict], None]]] = {}
        self._messages: queue.Queue[tuple[str, dict]] = queue.Queue()
        self._client = mqtt.Client()
        self._client.on_message = self._on_message
        self._client.connect(host)
        self._client.loop_start()

    def _on_message(self, _client, _userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"Ignoring invalid MQTT message on {message.topic}: {exc}")
            return
        self._messages.put((message.topic, payload))

    def subscribe(self, topic: str, callback: Callable[[dict], None]) -> None:
        self._subscribers.setdefault(topic, []).append(callback)
        self._client.subscribe(topic)

    def publish(self, topic: str, payload: dict) -> None:
        self._client.publish(topic, json.dumps(payload))

    def pump(self) -> None:
        while True:
            try:
                topic, payload = self._messages.get_nowait()
            except queue.Empty:
                return
            for callback in self._subscribers.get(topic, []):
                callback(payload)


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

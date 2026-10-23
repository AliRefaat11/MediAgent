import json
import queue
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


class Context:
    def __init__(self, clock, bus, db, config):
        self.clock = clock
        self.bus = bus
        self.db = db
        self.config = config

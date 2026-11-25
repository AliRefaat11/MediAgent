# MediAgent Demo
A multi-agent medication dispensing and delivery demo with a caregiver dashboard.
The dispenser and robot are **SIMULATED**; nothing was tested on real hardware.
`MqttBus` has been tested against a local Mosquitto broker (full delivery cycle and Last Will), not yet with the ESP32 firmware.

## Install and run
Requires Python 3.10+.
Install: `python3 -m venv .venv` then `.venv/bin/pip install -r requirements.txt` (Windows: `py -3.10 -m venv .venv`, then `.venv\Scripts\pip`).
Run: `.venv/bin/python backend/main.py --dashboard --speed 5` (Windows: `.venv\Scripts\python backend\main.py --dashboard --speed 5`).
Open http://localhost:5000.

Add a daily schedule at startup with `--add-schedule 08:00:Aspirin:1`. This is idempotent: running it again does not create a duplicate row.

Scenario 1: on the Caregiver or Robot page click **Send a test dose** / **Trigger dose now**, then on the Patient page press **I took my medicine** when prompted.
Scenario 2: trigger a dose and do nothing. Reminders repeat, then a critical caregiver notification appears and repeats until it is acknowledged.
Scenario 3: on the Robot page use the simulation buttons (open tray lid, emergency stop, lose the line, low battery, cut a heartbeat) to see each failure path.

### Run modes
| Command | What runs |
| --- | --- |
| `backend/main.py` (or `--sim`) | Agents and simulated devices in one process on the in-memory `LocalBus` |
| `backend/main.py --mqtt --sim` | Agents on MQTT; the simulated dispenser and robot run in the same process but each has its own MQTT connection and Last Will |
| `backend/main.py --mqtt` plus `python -m backend.simulator --mqtt` | Agents and devices in separate processes, exactly like the real ESP32s |

`--mqtt` needs a broker (for example `mosquitto -p 1883`); use `--host` and `--mqtt-port` to point at it. `--confirm-method lid` makes the simulated patient open the tray lid instead of pressing the button.

## Pages
Open http://localhost:5000 and pick a view from the top navigation. Each page can run on its own device:

| Page | URL | For |
| --- | --- | --- |
| Home | `/` | Choose a view; shows a live one-line status for each |
| Caregiver | `/caregiver` | A notifications bell in the navbar, summary cards, and **Live** (delivery cycle, queued and upcoming doses), **Schedules** and **History** tabs. Direct links: `/caregiver#schedules`, `/caregiver#history`, and `/caregiver#notifications` (opens the notifications popup) |
| Patient | `/patient` | One large message ("Your medicine is on its way", "Time to take your medicine"…) and a big **I took my medicine** button |
| Robot | `/robot` | Status, battery, what the robot's screen shows, route progress, simulation controls and the activity log |

On the caregiver page, notifications live behind the bell icon in the navbar. Its badge shows the unread count and turns red when a critical alert is waiting. Clicking the bell opens a popup with the full list, **Acknowledge** buttons and **Mark all read**. Escape, the × button or a click outside closes it, and closing it marks the info notifications as read. Warnings and critical alerts stay counted until they are acknowledged.

Notification toasts, sound and desktop alerts appear only on the caregiver page. Clicking a desktop alert opens the popup. On the other pages, the Caregiver link in the navigation shows a badge with the number of alerts waiting for acknowledgement.

## Caregiver dashboard
- **Schedules**: add, edit, delete and enable/disable schedules (medication, slot, HH:MM time, optional days of the week). Changes apply immediately without a restart.
- **Delivery cycle**: a live stepper (Dose due → Deliver → Alert → Detect → Return), with a failure path to *Caregiver alerted*. It also shows the detailed stage (loading, delivering, arrived, waiting for confirmation, taken, returning, charging, failed), queued doses and the next scheduled dose.
- **Notifications**:
  - `info`: dose dispatched, arrived, taken, and schedule changed.
  - `warning`: delivery or load failures (`path_blocked`, `lost_line`, `low_battery`, `delivery_timeout`, `load_failed`, `robot_unavailable`).
  - `critical`: a dose not confirmed before the deadline, or not delivered within the grace window.

  Each one shows as a toast and in a list with an unread count. Warning and critical notifications have an **Acknowledge** button, and critical toasts stay on screen until acknowledged. The optional **Desktop alerts and sound** toggle adds a browser notification and a short beep for warning and critical notifications.
- **Dose history**: recent taken, missed and failed doses with the failure reason, plus queued and upcoming doses.
- **Live updates**: Server-Sent Events (`/stream`), falling back automatically to 1-second polling of `/state` if the stream fails.

## Behaviour
- Pending doses older than `Config.missed_grace` (15 minutes) are marked `missed` and raise a critical notification instead of being delivered late. At startup, schedule times already older than the grace window are not generated.
- Doses are generated on the first tick, again at midnight, and rebuilt after every schedule change. A rebuild only replaces that schedule's *future pending* doses; active, due and finished doses are never touched.
- The patient's confirmation window (`confirm_timeout`) starts when the robot **arrives**, not when it leaves.
- A dose that becomes due while the robot is busy is queued until the robot is free. `robot_unavailable` is only reported when the robot is offline or its battery is too low.

## HTTP API
| Method | Path | Notes |
| --- | --- | --- |
| GET | `/state` | Live snapshot (robot, active dose, cycle, notifications) |
| GET | `/stream` | Server-Sent Events: `state`, `notification`, `ack`, `schedules`, `doses` |
| GET | `/events` | Last 30 bus events |
| GET/POST | `/schedules` | POST body: `{"medication": "Aspirin", "slot": 1, "time_hhmm": "08:00", "days": ["mon"], "enabled": true}` (`days` and `enabled` optional) |
| PUT/DELETE | `/schedules/<id>` | PUT accepts any subset of the fields above |
| GET | `/doses` | `?status=taken,missed,failed&limit=50&order=desc`; includes `reason` |
| GET | `/notifications` | Last 50, with `acked` |
| POST | `/notifications/<id>/ack` | Acknowledge a notification |
| POST | `/trigger`, `/confirm` | Demo controls |

Invalid input returns `400` with `{"errors": {field: message}}`, and an unknown id returns `404`. Flask never writes the database. Writes go to the main loop through `control_queue` (a single DB writer), which then publishes `mediagent/schedule/changed` on the bus.

`POST /sim/<device>/<action>` drives the simulated hardware (`robot`: `press_button`, `open_lid`, `estop`, `release_estop`, `lost_line`, `set_battery` with `{"value": 20}`, `heartbeat_off`, `heartbeat_on`, `power_off`, `power_on`; `dispenser`: `heartbeat_off`, `heartbeat_on`, `power_off`, `power_on`, `refill` with `{"slot": 1, "count": 20}`).

## Agents
| Agent | Type | Does |
| --- | --- | --- |
| Scheduling | Goal-based, with internal state | Turns schedules into dated doses (startup, midnight, after each `schedule/changed`), expires doses past the grace window, commands the dispenser, confirms the load with retries, releases the dose only when the robot is ready |
| Delivery | Model-based reflex (FSM: IDLE, DISPATCHING, EN_ROUTE, AT_PATIENT, RETURNING) | Drives the robot to the patient, reacts to obstacle, lost line, battery, e-stop and heartbeat loss, sends it home |
| Reminder | Simple reflex plus timers | Alerts on arrival, repeats every interval up to N times, stops on confirmation (button or tray lid) |
| Escalation & Notification | Utility/rule-based | Picks the level (info, warning, critical), notifies the caregiver, repeats criticals until acknowledged, closes doses as taken, missed or failed, watches device heartbeats |

The agents never call each other; they only publish and subscribe. All topics, QoS/retain rules, reasons and the envelope live in `backend/protocol.py`.

## Message protocol
Every message is JSON with the envelope `{"v": 1, "msg_id": "<uuid>", "ts": <seconds>, "source": "delivery_agent", "dose_id": 42, ...payload}`. `dose_id` is the correlation id (`null` when a message is not about a dose) and hardware replies must echo it. The bus drops any `msg_id` it has already delivered, so QoS 1 redeliveries are harmless.

| Topic (prefix `mediagent/`) | From → to | QoS | Retained |
| --- | --- | --- | --- |
| `dispenser/command` `{"action": "load", "slot", "medication", "attempt"}` | Scheduling → dispenser | 1 | no |
| `dispenser/status` `{"loaded", "attempt", "error"?}` | dispenser → Scheduling | 1 | no |
| `dispenser/state` `{"online", "state": IDLE/LOADING/ERROR, "slots": {"1": 20}}` | dispenser → Scheduling, Escalation | 0 | yes, Last Will `OFFLINE` |
| `robot/command` `{"action": deliver/return/alert/stop}` | Delivery, Reminder → robot | 1 | no |
| `robot/event` `{"event": arrived/confirmed/obstacle/path_clear/lost_line/docked/emergency_stop}`; `confirmed` has `"method": "button"` or `"lid"` | robot → Delivery, Reminder | 1 | no |
| `robot/state` `{"online", "state", "battery", "lid_open", "dose_id"}` about every 1 s | robot → Scheduling, Delivery, Escalation, dashboard | 0 | yes, Last Will `OFFLINE` |
| `agents/dose_loaded`, `agents/delivery_started`, `agents/arrived` | agent → agent | 1 | no |
| `agents/dose_done` `{"result": taken/missed/failed, "reason"?, "method"?}` | Reminder or Escalation → all | 1 | no |
| `agents/failure` `{"reason"}` | Scheduling or Delivery → Escalation, Delivery | 1 | no |
| `schedule/changed` | main loop (dashboard API) → Scheduling, Escalation | 1 | no |
| `notify/caregiver` `{"notification_id", "level", "message", "reason", "requires_ack", "repeat"}` | Escalation → dashboard | 1 | no |
| `caregiver/ack` `{"notification_id"}` | dashboard → Escalation | 1 | no |
| `sim/control` `{"device", "action"}` | dashboard → simulators only | 1 | no |

Failure reasons: `load_failed`, `slot_empty`, `dispenser_offline`, `robot_unavailable`, `robot_offline`, `path_blocked`, `lost_line`, `low_battery`, `delivery_timeout`, `emergency_stop`. After `lost_line` or `emergency_stop` the robot is told to stop (not to drive home), because it cannot navigate safely; after the other failures it returns to the dock.

Notification levels: `info` (dispatched, arrived, taken, schedule changed, device back online; no ack), `warning` (delivery or load failed, device offline, emergency stop; needs ack), `critical` (not confirmed before the deadline, or missed because of the grace window; needs ack and is re-sent every `critical_repeat_interval` with the same `notification_id` and a higher `repeat` until acknowledged).

Existing database files are migrated automatically: `enabled` and `days` columns are added to `schedules`, and a `notifications` table is created.

## Tests
From the project root:

```
.venv/bin/python -m pytest -q
```

(Windows: `.venv\Scripts\python -m pytest -q`.) The tests drive the runtime with a manual clock, so they are deterministic and finish in a few seconds. `tests/test_protocol.py` covers the envelope, duplicate dropping, retained state, the tray lid, the dispenser LOADING state and every failure path (load retries, empty slot, robot offline with Last Will, low battery before and during delivery, obstacles, delivery timeout, lost line, emergency stop and recovery, heartbeat loss, dispenser offline) plus critical repeats until acknowledgement. Two tests need an MQTT broker on `localhost:1883` and are skipped without one.

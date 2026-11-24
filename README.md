# MediAgent Demo
A multi-agent medication dispensing and delivery demo with a caregiver dashboard.
The dispenser and robot are **SIMULATED**; nothing was tested on real hardware.
`MqttBus` is included but **UNTESTED with a real broker**.

## Install and run
Requires Python 3.10+.
Install: `python3 -m venv .venv` then `.venv/bin/pip install -r requirements.txt` (Windows: `py -3.10 -m venv .venv`, then `.venv\Scripts\pip`).
Run: `.venv/bin/python backend/main.py --dashboard --speed 5` (Windows: `.venv\Scripts\python backend\main.py --dashboard --speed 5`).
Open http://localhost:5000.

Add a daily schedule at startup with `--add-schedule 08:00:Aspirin:1`. This is idempotent: running it again does not create a duplicate row.

Scenario 1: on the Caregiver or Robot page click **Send a test dose** / **Trigger dose now**, then on the Patient page press **I took my medicine** when prompted.
Scenario 2: trigger a dose and do nothing. Reminders repeat, then a critical caregiver notification appears.

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

Bus additions (existing topics and fields are unchanged): `mediagent/schedule/changed`. `mediagent/alert/caregiver` payloads now also carry `id`, `ts` and optional `reason`, and the `info` level is used. `mediagent/agents/dose_done` may carry `reason: "past_grace_window"`.

Existing database files are migrated automatically: `enabled` and `days` columns are added to `schedules`, and a `notifications` table is created.

## Tests
From the project root:

```
.venv/bin/python -m unittest tests.test_scenarios -v
```

(Windows: `.venv\Scripts\python -m unittest tests.test_scenarios -v`.) The scenario tests drive the runtime with a manual clock, so they are deterministic and finish in about a second.

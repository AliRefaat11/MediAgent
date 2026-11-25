# MediAgent backend: a simple guide

This guide explains what the code in `backend/` does, in plain words. It is written for the whole team, including people who did not write the code.

## 1. The big picture

MediAgent has three parts:

- **The brain:** four software agents written in Python. They decide what to do.
- **The body:** the dispenser and the robot. For now these are *simulated*: small Python programs that pretend to be the real hardware. Later the real ESP32 boards take their place.
- **The caregiver's screen:** the dashboard, a web page that shows what is happening and lets the caregiver edit schedules.

These parts never call each other directly. They only send each other **messages**. That rule makes it possible to swap the fake hardware for the real hardware without changing the agents.

## 2. Words you need to know

**Message**
: A small piece of data sent from one part to another, written as JSON. Example: `{"event": "arrived", "dose_id": 42}`.

**Bus**
: The "post office" that carries messages. Anyone can drop a message in, and the bus hands it to everyone who asked for that kind of message.

**Topic**
: The "address" or channel name of a message, for example `mediagent/robot/event`. Robot events go on that topic. Anyone who cares about robot events listens to it.

**Publish / subscribe**
: To *publish* is to send a message on a topic. To *subscribe* is to say "please give me every message on this topic". The sender does not need to know who is listening. This is why the agents never call each other.

**MQTT**
: A popular, lightweight publish/subscribe system made for small devices like the ESP32. It needs a program called a **broker** (we use Mosquitto) that runs on a computer and passes messages between everyone.

**LocalBus and MqttBus**
: We have two buses with exactly the same buttons (`subscribe`, `publish`, `pump`). `LocalBus` lives inside one Python program and needs no broker, which is good for testing and demos. `MqttBus` goes through a real MQTT broker, which is what we will use with the real hardware. The agents do not know or care which one they are using.

**QoS (Quality of Service)**
: How hard MQTT tries to deliver a message. QoS 0 means "send once, if it gets lost, too bad". It is fine for heartbeats, because a new one comes every second. QoS 1 means "keep resending until the receiver says it got it". We use it for anything important, like "load this dose". QoS 1 can sometimes deliver the same message **twice**, so we also need deduplication (see *envelope*).

**Retained message**
: The broker keeps the *last* message on that topic and gives it to anyone who subscribes later. So when the dashboard starts, it immediately learns the robot's last known state without waiting.

**Last Will**
: A message a device gives the broker when it connects, saying "if I disappear without saying goodbye, publish this for me". Our robot's Last Will is "state = OFFLINE". If the robot loses power, the broker tells everyone it is offline.

**Heartbeat**
: A small "I'm alive" message a device sends regularly. The robot sends its state and battery level about once per second. If the heartbeats stop for 10 seconds, we treat the robot as offline.

**Envelope**
: Standard information added around every message, like the envelope around a letter:
  - `v`: protocol version (1)
  - `msg_id`: a unique random id. If the same `msg_id` arrives twice, the second copy is thrown away.
  - `ts`: the time it was sent
  - `source`: who sent it, for example `delivery_agent`
  - `dose_id`: which dose this is about (the *correlation id*). The hardware must copy it back in its replies, so we always know which dose a reply belongs to.

**Tick**
: The program runs a loop about 20 times per second. Each loop is one "step", and every agent's `tick(now)` function is called once per step. Agents use `tick` to check timers, for example "has it been 60 seconds since the last reminder?"

**State machine (finite state machine)**
: A way to describe something that is always in exactly one "state" and moves between states when events happen. Example for the robot: IDLE → DELIVERING → WAITING_CONFIRM → RETURNING → CHARGING → IDLE.

**SSE (Server-Sent Events)**
: A simple way for a web server to *push* live updates to a web page, so the dashboard updates by itself without pressing refresh.

**Control queue**
: A waiting line where the dashboard puts requests such as "add this schedule". The main loop picks them up one at a time, so only one part of the program ever writes to the database. This avoids two writers clashing.

## 3. The files, one by one

### `protocol.py`: the shared dictionary
This file is the list of words everyone agrees on:
- all the topic names (`mediagent/dispenser/command`, `mediagent/robot/event`, …)
- for each topic, its QoS and whether it is retained
- the robot states (IDLE, DELIVERING, …, EMERGENCY_STOP, OFFLINE) and the dispenser states (IDLE, LOADING, ERROR, OFFLINE)
- the failure reasons (`load_failed`, `slot_empty`, `path_blocked`, `lost_line`, `low_battery`, `robot_offline`, `emergency_stop`, …)
- the notification levels (`info`, `warning`, `critical`)
- `envelope()`, which wraps a message with `v`, `msg_id`, `ts`, `source` and `dose_id`
- `Deduper`, which remembers the last 2048 message ids so duplicates can be ignored

If the firmware team wants to know "what exactly do I send?", this file plus the README table is the answer.

### `core.py`: the plumbing
- **`Clock`** tells the time. It has a speed setting, so a demo can run 5× or 20× faster and you don't wait 10 real minutes for a timeout.
- **`LocalBus`** is the in-program post office. Messages wait in a queue until `pump()` is called, then go to every subscriber. It throws away duplicate `msg_id`s, remembers retained messages for late subscribers, and can pretend to fire a Last Will.
- **`MqttBus`** is the same thing, but through a real MQTT broker. It uses the right QoS and retain setting for each topic, re-subscribes automatically if the connection drops, and can register a Last Will.
- **`Endpoint`** is the "letter writer" each part uses to send. You give it the topic and the content, and it adds the envelope and the sender's name.
- **`Broadcaster`** sends live updates to the dashboard pages (SSE).
- **`Context`** is a small box that holds the clock, bus, database and config, so every agent gets all four in one go.

### `config.py`: all the numbers in one place
Every setting you might want to change lives here, so nobody hunts through the code. The defaults:

| Setting | Default | Meaning |
|---|---|---|
| `min_battery` | 30 % | Robot needs at least this much to start a delivery |
| `load_timeout` | 10 s | How long to wait for the dispenser to answer |
| `load_retries` | 2 | Extra tries after a failed load |
| `max_start_delay` | 60 s | How long a due dose may wait for the robot or dispenser |
| `robot_timeout` / `dispenser_timeout` | 10 s | No heartbeat for this long means the device is offline |
| `start_timeout` | 10 s | Robot must start driving within this time after "deliver" |
| `obstacle_limit` | 3 | Obstacles in a row before giving up |
| `delivery_timeout` | 120 s | Maximum driving time to reach the patient |
| `critical_battery` | 15 % | Below this while driving, go home |
| `reminder_interval` / `reminder_max_repeats` | 60 s / 5 | How often and how many times to remind the patient |
| `confirm_timeout` | 600 s | How long the patient has to take the dose after the robot arrives |
| `critical_repeat_interval` | 120 s | How often to repeat an unacknowledged critical alert |
| `missed_grace` | 15 min | A dose older than this is marked missed instead of delivered late |

`demo_config()` uses shorter times for live demos.

### `db.py`: the memory (SQLite database)
The database file is `data/mediagent.db`. It has four tables:
- **schedules:** what the caregiver set up, for example "Aspirin, slot 1, 08:00, Mon/Wed/Fri".
- **doses:** one row per actual dose on a date, with a status: `pending` (waiting), `active` (on its way), `taken`, `missed` or `failed`.
- **events:** a log of everything that happened, used for the history and activity log.
- **notifications:** every message sent to the caregiver, and whether it was acknowledged.

Important functions:
- `generate_today`: turns schedules into today's doses.
- `rebuild_schedule_doses`: after an edit, deletes and re-creates only the *future, still pending* doses of that schedule.
- `get_due_doses`: finds the doses whose time has come.
- `set_status`, `save_notification`, `ack_notification`: small helpers that update those tables.

### `validation.py`: the input checker
Before a schedule is saved, it checks the input: medication name not empty, slot a positive whole number, time in `HH:MM` format, valid weekdays. Bad input gets a clear error message back and nothing is saved.

### `agents.py`: the four agents
Explained in detail in section 4.

### `simulator.py`: the fake hardware
- **`DispenserSim`** pretends to be the pill dispenser. When it gets a "load" command it goes to **LOADING** for 2 seconds, then answers on `dispenser/status`: "loaded: true", "loaded: false, error: jam" or "error: slot_empty". It counts pills per slot (20 each at the start) and sends a heartbeat every second with its state and pill counts.
- **`RobotSim`** pretends to be the robot. It drives for a set time (it does not really move), loses battery while driving, can meet an obstacle, and waits at the patient for confirmation. The patient confirms with the **button** or by opening the **tray lid**. It has an **EMERGENCY_STOP** state for the physical stop button. Its heartbeat every second carries state, battery and whether the lid is open.
- **`SimDevices`** holds both simulators. Over MQTT it gives each one its *own* connection and Last Will, exactly like two separate ESP32 boards.
- The simulators listen to an extra topic, `mediagent/sim/control`, so the dashboard buttons can press the confirm button, open the lid, press the e-stop, cut a heartbeat, set the battery and so on. Real firmware will ignore that topic.
- You can run this file by itself (`python -m backend.simulator --mqtt`) to act as hardware in a separate program.

### `runtime.py`: the main loop and the recorder
`Runtime` creates the four agents (and the simulators, if needed). Its `step()` function runs about 20 times a second and does this, in order:
1. deliver all waiting messages on the bus (`pump`)
2. call `tick()` on every agent
3. let the simulators move forward
4. handle dashboard requests from the control queue (add/edit/delete schedule, acknowledge an alert, simulation buttons)
5. deliver messages again, and update the **snapshot**: a summary of "what is happening now" that the dashboard shows

It also acts as the **recorder**: it listens to messages and writes them to the database. For example, it saves `dose_done: taken` as the dose status and saves each notification. It is the only part that writes to the database, apart from the agents themselves, which run inside the same loop.

### `main.py`: the start button
It reads the command-line options and starts everything:
- no options: agents and simulated hardware in one program, using `LocalBus`
- `--dashboard`: also starts the web pages at http://localhost:5000
- `--mqtt`: use a real MQTT broker (Mosquitto must be running)
- `--mqtt --sim`: use the broker, and also start the simulated hardware here, each part with its own connection
- `--speed 5`: run time 5× faster (for quick failure-path tests only: schedules follow this fast clock, so a dose set for 16:00 will not fire at real 16:00; leave it out for normal use)
- `--add-schedule 08:00:Aspirin:1`: add a schedule at startup

## 4. The four agents in detail

All four follow the same pattern:
- In `__init__`, they **subscribe** to the topics they care about.
- `on_something(message)` functions run when a message arrives. They **react**.
- `tick(now)` runs about 20 times a second. It **checks timers**.
- `send(topic, content, dose_id)` publishes a message with the envelope.

A small helper, `DeviceWatch`, remembers a device's last heartbeat and answers one question: "is this device online right now?"

### 4.1 Scheduling Agent: "make sure every dose leaves on time"
*Type: goal-based with internal state.* It has a goal (every due dose loaded and handed to a ready robot) and remembers what it is doing (which dose, how many retries so far).

- **`_generate_if_new_day`**: on the first tick and again after midnight, creates the day's doses from the schedules. At startup it skips times that are already too old.
- **`_expire_stale`**: any pending dose older than 15 minutes (the grace window) is marked **missed**, so a stale dose is never delivered hours late.
- **`on_schedule_changed`**: when the caregiver edits a schedule, it rebuilds that schedule's future pending doses. It never touches a dose that is already on its way.
- **`tick`**: if no dose is in progress, it looks for a due dose and asks "what could stop me?" (`_blocker`):
  - robot is busy with another dose → wait; the dose stays in the queue
  - the slot is empty → fail now with `slot_empty`
  - dispenser offline, or robot offline or low on battery → wait up to 60 s, then fail (`dispenser_offline` or `robot_unavailable`)
  - nothing blocks it → send **"load"** to the dispenser
- **`on_dispenser_status`**: the dispenser's answer.
  - loaded → when the robot is ready, publish **`dose_loaded`** (this hands the dose to the Delivery Agent)
  - not loaded → try again (up to 2 more times), then fail with `load_failed`
  - an answer to an old attempt is ignored
- **`on_robot_state` / `on_dispenser_state`**: store the heartbeats.

### 4.2 Delivery Agent: "drive the dose to the patient and back"
*Type: model-based reflex, written as a state machine.* It keeps a small *model* of the robot (last state, battery, last heartbeat) and reacts to events according to its current mode:

```
IDLE → DISPATCHING → EN_ROUTE → AT_PATIENT → RETURNING → IDLE
```

- **`on_dose_loaded`**: sends the robot **"deliver"**, publishes `delivery_started`, mode = DISPATCHING.
- **`on_robot_state`**: when the robot says DELIVERING, mode = EN_ROUTE. If the robot reports EMERGENCY_STOP or OFFLINE during a delivery, the delivery fails. When the robot is back at IDLE or CHARGING, mode = IDLE.
- **`on_robot_event`**:
  - `arrived` → mode = AT_PATIENT, publish `agents/arrived`
  - `obstacle` → count it; at 3 in a row, fail with `path_blocked`
  - `path_clear` → reset the count
  - `lost_line` or `emergency_stop` → fail
- **`tick`** checks, while driving:
  - no heartbeat for 10 s → fail with `robot_offline`
  - robot never started driving → fail with `robot_unavailable`
  - battery under 15 % → fail with `low_battery`
  - driving longer than 120 s → fail with `delivery_timeout`
- **`on_failure`**: for most failures, sends the robot **"return"** home. For `lost_line` and `emergency_stop` it sends **"stop"**, because the robot cannot drive safely and a person has to collect it.
- **`on_dose_done`**: when the dose is finished (taken or missed), turns off the alert and sends the robot home.

### 4.3 Reminder Agent: "make sure the patient notices"
*Type: simple reflex plus timers.* Simple "if this, then that" rules:

- **`on_arrived`**: the robot is at the patient → turn the alert on (buzzer, LED, screen text "Take Aspirin").
- **`tick`**: every 60 s without confirmation → alert again, at most 5 times.
- **`on_robot_event`** with `confirmed` (button pressed **or** tray lid opened) → turn the alert off and publish **`dose_done: taken`**.
- **`on_dose_done` / `on_failure`**: stop reminding if the dose ended another way.

### 4.4 Escalation & Notification Agent: "keep the caregiver informed"
*Type: utility/rule-based.* It decides how serious something is and tells the caregiver. It is also the agent that closes a dose as missed or failed.

Three levels:
- **info**: dose dispatched, arrived, taken, schedule changed, device back online. No action needed.
- **warning**: delivery or load failed, device offline, emergency stop. The caregiver must press **Acknowledge**.
- **critical**: the patient did not confirm in time, or a dose was missed because of the grace window. The caregiver must acknowledge it, and it **repeats every 2 minutes** until they do.

Functions:
- **`notify`**: publishes a notification on `notify/caregiver` and remembers the ones that need an acknowledgement.
- **`on_arrived`**: starts the patient's 10-minute deadline (it starts at *arrival*, not when the robot leaves).
- **`tick`**: deadline passed → critical alert and **`dose_done: missed`**. It also repeats unacknowledged criticals and checks device heartbeats.
- **`on_failure`**: warning, then **`dose_done: failed`** with the reason.
- **`on_ack`**: the caregiver pressed Acknowledge (`caregiver/ack`), so stop repeating.
- **`on_device_state`**: watches robot and dispenser heartbeats and warns when one goes offline.
- **`send_external`**: an empty hook for later, where Telegram or e-mail alerts will be added.

## 5. One dose from start to finish (the happy path)

Example: the caregiver wants Aspirin from slot 1 at 08:00.

1. **The caregiver adds the schedule** on the dashboard. `dashboard/app.py` checks the input (`validation.py`) and puts the request on the control queue.
2. **The main loop saves it** (`runtime.py` → `db.py`) and publishes `schedule/changed`.
3. **The Scheduling Agent** hears `schedule/changed` and creates today's 08:00 dose (status `pending`). The **Escalation Agent** sends an info notification: "Schedule created: Aspirin at 08:00".
4. **At 08:00**, the Scheduling Agent's `tick` sees the dose is due. The robot is IDLE with enough battery, the dispenser is online and slot 1 has pills. It publishes `dispenser/command` = "load slot 1".
5. **The dispenser** (`simulator.py`, `DispenserSim`) goes to LOADING, drops the pill, and replies on `dispenser/status` with "loaded: true" and the same `dose_id`.
6. **The Scheduling Agent** sees the load succeeded and the robot is ready, and publishes `agents/dose_loaded`.
7. **The Delivery Agent** sends the robot `robot/command` = "deliver" and publishes `agents/delivery_started`. The recorder marks the dose `active`, and the Escalation Agent sends info: "Aspirin dispatched".
8. **The robot** (`RobotSim`) switches to DELIVERING, drives, and then publishes `robot/event` = "arrived".
9. **The Delivery Agent** publishes `agents/arrived`. The **Reminder Agent** turns on the buzzer and screen ("Take Aspirin"). The **Escalation Agent** starts the 10-minute deadline and sends info: "Aspirin arrived".
10. **The patient takes the pill** and presses the button or opens the tray lid. The robot publishes `robot/event` = "confirmed".
11. **The Reminder Agent** turns the alert off and publishes `agents/dose_done` = "taken". The recorder marks the dose `taken`, and the Escalation Agent sends info: "Aspirin taken by the patient".
12. **The Delivery Agent** sends the robot "return". The robot drives back, reports "docked", charges, and becomes IDLE. The cycle is complete.

## 6. One failure from start to finish (blocked path)

Same dose, but a chair blocks the hallway and nobody moves it.

1. Steps 1–8 happen as above, and the robot is DELIVERING.
2. **The robot's ultrasonic sensor** sees something closer than about 20 cm. It stops and publishes `robot/event` = "obstacle". It keeps reporting while the path stays blocked.
3. **The Delivery Agent** counts the obstacles. If the path clears (`path_clear`), the count resets and the robot continues. Here it reaches **3**, so the agent publishes `agents/failure` with reason `path_blocked`.
4. **The Delivery Agent** (in `on_failure`) sends the robot "return", and the robot drives back to the dock.
5. **The Scheduling Agent** clears the dose from its "in progress" memory, so the next dose can go.
6. **The Escalation Agent** sends a **warning**: "Aspirin delivery failed: path_blocked", and publishes `agents/dose_done` = "failed".
7. **The recorder** marks the dose `failed`, and the dashboard history shows the reason "Path blocked".
8. **The caregiver** sees the warning and presses **Acknowledge**. The dashboard publishes `caregiver/ack`, and the warning is marked as handled.

Other failures follow the same pattern; only the reason and what the robot does change:

| Problem | Who notices | Reason | Robot |
|---|---|---|---|
| Dispenser jams three times | Scheduling | `load_failed` | never leaves |
| Slot is empty | Scheduling | `slot_empty` | never leaves |
| Robot battery low before start | Scheduling | `robot_unavailable` (after 60 s) | never leaves |
| Battery under 15 % while driving | Delivery | `low_battery` | goes home |
| Robot loses the line | Delivery | `lost_line` | stops; a person collects it |
| Emergency stop pressed | Delivery | `emergency_stop` | stopped until released |
| Robot stops sending heartbeats | Delivery and Escalation | `robot_offline` | unknown; caregiver warned |
| Patient never confirms | Escalation | `missed` (critical, repeats) | goes home |

## 7. How to try it yourself

```
.venv/bin/pip install -r requirements.txt
.venv/bin/python backend/main.py --dashboard
```

Then open http://localhost:5000:
- On the **Caregiver** page, open **Schedules** and add a schedule a minute or two from now. When it is due, the dose starts by itself.
- On the **Patient** page, press **I took my medicine**, or on the Robot page click **Open tray lid**.
- Use the other Robot page buttons (**Emergency stop**, **Lose the line**, **Battery 20%**, **Cut robot heartbeat**) to watch each failure, and see the alerts arrive on the **Caregiver** page.

To run the tests:

```
.venv/bin/python -m pytest -q
```

# Dispenser design

The dispenser stores up to four different medicines, each in its own tube. When the
Scheduling Agent asks for a dose, the dispenser releases exactly one pill of the chosen
medicine. The pill falls through a funnel and a vertical chute (the "cylinder") into the
tray of the robot car, which is parked under the chute at its dock.

This is a student prototype built in 10 weeks on a small budget. It is an assistive aid,
not a certified medical device.

## 1. Recommended mechanism: one rotating pocket disc per medicine

Each medicine has its own **metering unit**:

```
        pill tube (one medicine, pills stacked)
              ||  ||
              ||  ||
        ======||==||=======   top plate: hole only under the tube
        [  disc with ONE pocket  ]  <- turned by an SG90 servo under it
        =================||====   bottom plate: exit hole 180 deg away from the tube
                         ||
                         v  to the shared funnel
```

1. Pills are stacked in a vertical tube that sits on a fixed **top plate**.
2. Under the top plate is a **disc** with **one pocket** the size of one pill. The disc is
   mounted on the horn of a small servo.
3. At **0°** the pocket is under the tube, and the lowest pill drops into it. The pocket is
   only as deep as one pill, so the pill above stays in the tube.
4. The servo turns to **180°**. The pocket now sits over the **exit hole** in the bottom
   plate, so the pill falls out. The solid part of the disc now closes the tube.
5. The servo returns to 0° and the pocket refills with the next pill.

All four exit holes feed one shared **funnel**, which narrows into the **chute** (a clear
tube). Near the bottom of the chute, **two IR break-beams** confirm that a pill really
passed. Under the chute, the robot's tray has its own wide funnel that catches the pill.

```
   [slot 1] [slot 2] [slot 3] [slot 4]      <- 4 metering units in a row
   \_____________ funnel ______________/
                    ||
                    ||  chute, 20 mm inside diameter
                  =[##]=  sensor ring: 2 IR break-beams, 90 deg apart
                    ||
                    v   15-25 mm gap
              \__________/   funnel on the robot tray (50 mm wide)
            [   ROBOT CAR   ]   magnet on the robot, hall sensor on the dock
   ==== dock floor with V-shaped guide rails ====
```

Why this one: each medicine is independent, one servo turn gives exactly one pill, there
is no linkage to adjust, and a medicine with a different pill size only needs a new
3D-printed disc and tube insert.

### Alternatives compared

| Option | How it works | Good | Bad | Verdict |
|---|---|---|---|---|
| **Pocket disc per tube (recommended)** | Servo turns a one-pocket disc from the tube to an exit hole | Cheap (SG90), one pill per turn, medicines independent, easy to resize per pill | One servo per medicine | Best fit for "store several medicines, release one of the chosen one" |
| Sliding shuttle per tube | Servo pushes a plate with one hole from under the tube to over the exit | Same accuracy as the disc | Needs a crank linkage and guide rails, sticks more easily | Works, but more parts to tune |
| Weekly carousel | Stepper turns a wheel of 14 or 28 pre-filled cups over one hole | One motor, any mix of pills per dose | Caregiver must sort doses by hand every week, needs a homing sensor, does not store medicines separately | Good for a pill organizer, not for this brief |
| Auger or vibration feeder | Screw or vibration moves pills one by one | Industrial accuracy | Hard to build and tune in 10 weeks | Not recommended |

## 2. Dimensions

These are starting sizes. Measure your real pills and your robot first.

| Part | Size | Notes |
|---|---|---|
| Pill tube | Inside diameter = pill diameter + 2 mm, 120 mm tall | Holds about 25 pills of 4 mm thickness. Use a 16 mm clear tube with a printed sleeve to narrow it for smaller pills |
| Disc | 45 mm diameter | Printed in PLA, one per medicine |
| Disc thickness | Pill thickness + 0.5 mm | This is what stops two pills entering the pocket |
| Pocket | Pill diameter + 1 mm, center 15 mm from disc center | Tube hole at 0°, exit hole at 180° |
| Top and bottom plates | 3 mm acrylic or printed, 0.3 mm gap to the disc | Too tight and the disc rubs, too loose and pills wedge |
| Metering unit pitch | 55 mm | 4 units in a row is about 220 mm |
| Funnel | Top 220 x 60 mm, walls at 50° or steeper | Smooth surface (tape or sanded print) so pills do not stop |
| Chute | 20 mm inside diameter clear PVC or acrylic tube | Length = chute exit height minus funnel bottom |
| Sensor ring | Printed collar at the chute bottom, inside 14 mm | Narrow neck so a pill cannot pass beside both beams |
| Chute exit height | Robot tray funnel top + 15 to 25 mm | Small gap so the pill does not bounce out |
| Robot tray funnel | 50 mm wide opening into a removable cup | Gives about ±15 mm tolerance for docking |
| Housing (upper body) | About 240 W x 160 D x 180 H mm | 3 mm acrylic or MDF, on legs or a shelf |
| Docking bay under it | About 250 mm wide | V-shaped guide rails that center the robot under the chute |

The robot now finds the patient freely in the house, so it will not stop at exactly the
same spot every time. The guide rails at the dock plus the wide tray funnel make up for
that.

## 3. Parts list (BOM)

Prices are rough estimates. Check them at local electronics shops.

| # | Part | Qty | Approx. USD | Use |
|---|---|---|---|---|
| 1 | ESP32 DevKit V1 | 1 | 5 | Controller, Wi-Fi, MQTT |
| 2 | DS3231 RTC module | 1 | 2 | Keeps time if Wi-Fi drops |
| 3 | SG90 micro servo (MG90S metal gear is better) | 4 | 8-12 | One per medicine |
| 4 | 3 mm IR break-beam pair (emitter + receiver) | 2 | 4 | Confirms a pill passed the chute |
| 5 | TCRT5000 IR reflective module | 4 | 2 | "Tube empty" sensor near each tube bottom |
| 6 | A3144 hall sensor + 10x3 mm neodymium magnet | 1 + 1 | 1 | Robot is parked under the chute |
| 7 | 16x2 LCD with I2C backpack | 1 | 3 | Shows state, counts, errors |
| 8 | Active buzzer 5 V | 1 | 0.5 | Beeps on success and errors |
| 9 | LEDs (green, red) + 220 Ω resistors | 2 | 0.5 | Status |
| 10 | Push button | 1 | 0.3 | Refill and service mode |
| 11 | 5 V 3 A DC adapter + DC jack | 1 | 4 | Powers servos and ESP32 |
| 12 | 1000 µF 16 V capacitor | 1 | 0.5 | Stops servo current dips resetting the ESP32 |
| 13 | 16 mm clear tube, 120 mm pieces | 4 | 3 | Pill tubes |
| 14 | 20 mm clear PVC or acrylic tube | 1 | 2 | Chute |
| 15 | PLA filament (discs, plates, funnel, sensor ring, robot tray funnel) | ~200 g | 4 | 3D-printed parts |
| 16 | 3 mm acrylic or MDF sheet | 1 | 5 | Housing and dock rails |
| 17 | Perfboard, screw terminals, jumpers, M3 screws | 1 set | 4 | Assembly |
| | **Total** | | **about 50-60** | |

## 4. Wiring and ESP32 pins

This is the dispenser's own ESP32, separate from the robot's board.

| Function | ESP32 pin | Notes |
|---|---|---|
| Servo slot 1 | GPIO 25 | PWM signal; servo power from the 5 V rail, not from the ESP32 |
| Servo slot 2 | GPIO 26 | |
| Servo slot 3 | GPIO 27 | |
| Servo slot 4 | GPIO 33 | |
| I2C SDA (DS3231 + LCD) | GPIO 21 | Both share the bus (RTC 0x68, LCD usually 0x27) |
| I2C SCL (DS3231 + LCD) | GPIO 22 | |
| Break-beam A receiver | GPIO 32 | INPUT_PULLUP, interrupt on falling edge |
| Break-beam B receiver | GPIO 16 | INPUT_PULLUP, interrupt on falling edge |
| Break-beam emitters (both) | GPIO 14 | Through 100 Ω each. Lets the firmware switch them off for a self-test |
| Tube empty sensor slot 1 | GPIO 34 | Input only. TCRT5000 digital out (module has its own pull-up) |
| Tube empty sensor slot 2 | GPIO 35 | Input only |
| Tube empty sensor slot 3 | GPIO 36 (VP) | Input only |
| Tube empty sensor slot 4 | GPIO 39 (VN) | Input only |
| Robot present (A3144 hall) | GPIO 17 | INPUT_PULLUP, LOW when the robot's magnet is under it |
| Buzzer | GPIO 23 | Active buzzer module |
| Green LED | GPIO 18 | Through 220 Ω |
| Red LED | GPIO 19 | Through 220 Ω |
| Refill / service button | GPIO 13 | INPUT_PULLUP, to GND |

Power:

- The 5 V 3 A adapter feeds the servo rail and the ESP32 VIN pin.
- Put the 1000 µF capacitor across the servo rail close to the servos.
- All grounds are joined (adapter, ESP32, servos, sensors).
- Only one servo moves at a time, so 3 A is plenty.
- Pins avoided on purpose: 0, 2, 5, 12 and 15 (boot strapping pins) and 6 to 11 (flash).

## 5. Drop and check sequence (matches the MQTT protocol)

Topics and payloads are the ones already used by `backend/protocol.py`,
`backend/agents/scheduling.py` and `DispenserSim` in `backend/simulator.py`.

**Command in**, on `mediagent/dispenser/command`:

```json
{"action": "load", "slot": "2", "medication": "Metformin", "dose_id": 41, "attempt": 1}
```

**Steps in the firmware:**

1. If another load is running, queue the command (the simulator does the same).
2. Set the state to `LOADING` and publish the heartbeat at once.
3. **Pre-checks**, all before any servo moves:
   - Robot present? If the hall sensor does not see the magnet, reply `no_robot`.
   - Slot empty? If the tube sensor sees no pill or the stored count is 0, reply `slot_empty`.
   - Beam self-test: switch the emitters off (both receivers must read "broken"), then on
     (both must read "clear"). If not, reply `sensor_fault`.
4. Clear the beam counter, then turn the slot's servo to 180°.
5. Wait up to **1.5 s** for a beam to break.
6. If nothing broke, wiggle the servo (170° to 180°, 3 times) and wait **1 s** more.
7. Turn the servo back to 0° so the pocket refills.
8. Wait **0.5 s** for the pill to settle in the robot's tray, then reply.

The worst case is about 5 s, well inside the backend's `load_timeout` of 10 s.

**Reply out**, on `mediagent/dispenser/status`. It always echoes `dose_id`, `slot` and
`attempt`:

| Result | Payload | Dispenser state after |
|---|---|---|
| One pill passed | `{"loaded": true, "slot": "2", "attempt": 1}` | `IDLE` |
| No pill passed | `{"loaded": false, "error": "jam", ...}` | `ERROR` |
| Tube empty | `{"loaded": false, "error": "slot_empty", ...}` | `ERROR` |
| Robot not under chute | `{"loaded": false, "error": "no_robot", ...}` | `ERROR` |
| Beam self-test failed | `{"loaded": false, "error": "sensor_fault", ...}` | `ERROR` |
| More than one pill passed | `{"loaded": false, "error": "double_drop", ...}` | `ERROR` |

A beam break is counted as one pill when either beam breaks, and a new pill is only
counted after both beams have been clear for 50 ms.

**Heartbeat**, on `mediagent/dispenser/state` (retained), about every 1 s, with the
Last Will set to the offline message:

```json
{"online": true, "state": "IDLE", "slots": {"1": 18, "2": 0, "3": 25, "4": 9},
 "robot_present": true}
```

`slots` is the pill count the firmware keeps in flash (NVS). It goes down by one after
each confirmed drop, and the firmware reports 0 when a tube's empty sensor sees no pill.
The Scheduling Agent already reads `slots` and will not start a dose for a slot at 0.

**Refill:** the caregiver presses the service button, picks the slot on the LCD, fills the
tube and confirms; the count is set back to full. A later version can accept
`{"action": "refill", "slot": "2", "count": 20}` on `dispenser/command` from the app (the
simulator already has this as a sim-only action).

## 6. Failure handling

| Failure | How it is detected | What the dispenser does | What the backend does today |
|---|---|---|---|
| Jam (pill stuck in tube or pocket) | No beam break after turn + wiggle | Servo back to 0°, red LED, 2 beeps, reply `jam` | Retries up to `load_retries` (2), then `load_failed` and the caregiver is alerted. Safe, because no pill left the pocket |
| Empty slot | Tube sensor or count = 0 | Reply `slot_empty`, LCD shows "Refill slot 2" | Fails at once with `slot_empty` (no retry) |
| Robot not under chute | Hall sensor | No servo move, reply `no_robot` | Treated as a normal load error: retried, then `load_failed`. No pill is wasted |
| Two pills fell | Beam counted 2 | Reply `double_drop`, red LED, long beep | **Needs a small backend change**: today any unknown error is retried, which would add a third pill. It should fail at once like `slot_empty` and alert the caregiver |
| Beam sensor broken or dirty | Self-test before each drop | Reply `sensor_fault`, no servo move | Retried, then `load_failed` |
| Pill fell but beams missed it | Not detectable by the dispenser | Reply `jam` | A retry could give a second pill. The narrow 14 mm neck and two crossed beams make this unlikely; a later fix is a weight or IR sensor in the robot tray |
| Wi-Fi or broker lost | MQTT disconnect | Last Will marks it `OFFLINE`; it keeps time with the DS3231, shows "Offline" and does not dispense on its own | Scheduling sees `dispenser_offline` and alerts after `max_start_delay` |
| Power dip when a servo starts | ESP32 resets | Capacitor and separate 5 V rail prevent it; after a reboot the counts come back from flash | Heartbeat returns, nothing else needed |

Backend follow-ups this design suggests (not done in this change):

- Add `NO_ROBOT`, `SENSOR_FAULT` and `DOUBLE_DROP` constants to `backend/protocol.py`.
- In `SchedulingAgent.on_dispenser_status`, fail at once on `double_drop`.
- Teach `DispenserSim` the new errors so the tests can cover them.

## 7. Limits

- Works for solid tablets and capsules of a fixed size per tube. Not for liquids, split
  tablets or soft gel caps that stick.
- One `load` command gives one pill. A dose of two tablets means two loads, or a future
  `count` field.
- Each medicine needs its own disc and tube insert, printed to its pill size.

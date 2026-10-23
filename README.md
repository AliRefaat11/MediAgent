# MediAgent Demo
Requires Python 3.10+.
Install: `python3 -m venv .venv` then `.venv/bin/pip install -r requirements.txt` (Windows: `py -3.10 -m venv .venv`, then `.venv\Scripts\pip`).
Run: `.venv/bin/python backend/main.py --dashboard --speed 5` (Windows: `.venv\Scripts\python backend\main.py --dashboard --speed 5`).
Add a daily patient schedule at startup with `--add-schedule 08:00:Aspirin:1`; it appears in the caregiver panel.
Open http://localhost:5000.
Scenario 1: click **Trigger dose now**, then **Confirm dose taken** when prompted.
Scenario 2: click **Trigger dose now** and do nothing; reminders repeat, then caregiver escalation appears.
The dispenser and robot are **SIMULATED**; nothing was tested on real hardware.
`MqttBus` is included but **UNTESTED with a real broker**.
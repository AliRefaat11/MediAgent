"""The four MediAgent agents.

Agents never call each other: they only subscribe and publish on the bus
(``backend.protocol`` lists the topics). Every agent is single-threaded and
keeps its timers in ``tick(now)``, which the main loop calls about 20 times a
second.

| Agent                       | Type                                  |
|-----------------------------|---------------------------------------|
| SchedulingAgent             | Goal-based, with internal state       |
| DeliveryAgent               | Model-based reflex (finite state m.)  |
| ReminderAgent               | Simple reflex plus timers             |
| EscalationAgent             | Utility/rule-based (Escalation &      |
|                             | Notification)                         |
"""
from backend.protocol import *  # noqa: F401,F403  (topics/constants re-exported)
from backend.protocol import NOTIFY_CAREGIVER

from backend.agents.base import Agent, DeviceWatch, message_online  # noqa: F401
from backend.agents.delivery import DeliveryAgent  # noqa: F401
from backend.agents.escalation import EscalationAgent  # noqa: F401
from backend.agents.reminder import ReminderAgent  # noqa: F401
from backend.agents.scheduling import SchedulingAgent  # noqa: F401

# Kept for code written against the first protocol draft.
CAREGIVER_ALERT = NOTIFY_CAREGIVER

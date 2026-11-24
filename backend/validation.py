"""Input validation shared by the dashboard API, the main loop and the CLI."""
from __future__ import annotations

import re

from backend.db import WEEKDAYS

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SCHEDULE_FIELDS = ("medication", "slot", "time_hhmm", "days", "enabled")
MAX_MEDICATION_LENGTH = 100


class ValidationError(ValueError):
    def __init__(self, errors: dict):
        super().__init__("; ".join(f"{key}: {value}" for key, value in errors.items()))
        self.errors = errors


def validate_schedule(data, partial: bool = False) -> dict:
    """Return a cleaned schedule dict or raise ValidationError.

    ``partial=True`` is used for PUT: only the supplied fields are checked,
    but at least one must be present.
    """
    if not isinstance(data, dict):
        raise ValidationError({"body": "expected a JSON object"})
    errors: dict = {}
    clean: dict = {}

    unknown = sorted(set(data) - set(SCHEDULE_FIELDS))
    if unknown:
        errors["body"] = f"unknown field(s): {', '.join(unknown)}"

    if "medication" in data or not partial:
        medication = data.get("medication")
        if not isinstance(medication, str) or not medication.strip():
            errors["medication"] = "medication is required and must be non-empty text"
        elif len(medication.strip()) > MAX_MEDICATION_LENGTH:
            errors["medication"] = f"medication must be at most {MAX_MEDICATION_LENGTH} characters"
        else:
            clean["medication"] = medication.strip()

    if "slot" in data or not partial:
        slot = data.get("slot")
        if isinstance(slot, str) and slot.strip().isdigit():
            slot = int(slot.strip())
        if isinstance(slot, bool) or not isinstance(slot, int):
            errors["slot"] = "slot must be an integer"
        elif slot < 1:
            errors["slot"] = "slot must be 1 or greater"
        else:
            clean["slot"] = slot

    if "time_hhmm" in data or not partial:
        time_hhmm = data.get("time_hhmm")
        if not isinstance(time_hhmm, str) or not TIME_RE.match(time_hhmm.strip()):
            errors["time_hhmm"] = "time must be in 24-hour HH:MM format, e.g. 08:30"
        else:
            clean["time_hhmm"] = time_hhmm.strip()

    if "days" in data:
        days = data.get("days")
        if days is None or days == "":
            days = []
        if isinstance(days, str):
            days = [part.strip() for part in days.split(",") if part.strip()]
        if not isinstance(days, list) or not all(isinstance(d, str) for d in days):
            errors["days"] = "days must be a list such as [\"mon\", \"wed\"]"
        else:
            lowered = [day.lower()[:3] for day in days]
            bad = [day for day in lowered if day not in WEEKDAYS]
            if bad:
                errors["days"] = f"unknown day(s): {', '.join(bad)}; use {', '.join(WEEKDAYS)}"
            else:
                clean["days"] = [day for day in WEEKDAYS if day in lowered]
    elif not partial:
        clean["days"] = []

    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            errors["enabled"] = "enabled must be true or false"
        else:
            clean["enabled"] = data["enabled"]
    elif not partial:
        clean["enabled"] = True

    if partial and not errors and not clean:
        errors["body"] = f"provide at least one of: {', '.join(SCHEDULE_FIELDS)}"
    if errors:
        raise ValidationError(errors)
    return clean

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
FINAL_STATUSES = ("taken", "missed", "failed")


def init_db(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            medication TEXT NOT NULL,
            slot INTEGER NOT NULL,
            time_hhmm TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS doses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_id INTEGER,
            medication TEXT NOT NULL,
            slot INTEGER NOT NULL,
            due_ts REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','active','taken','missed','failed')),
            UNIQUE(schedule_id, due_ts)
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            dose_id INTEGER,
            kind TEXT NOT NULL,
            detail TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY,
            ts REAL NOT NULL,
            level TEXT NOT NULL,
            dose_id INTEGER,
            message TEXT NOT NULL,
            reason TEXT,
            acked INTEGER NOT NULL DEFAULT 0,
            acked_ts REAL
        );
        CREATE INDEX IF NOT EXISTS idx_events_dose ON events(dose_id, kind);
        CREATE INDEX IF NOT EXISTS idx_doses_due ON doses(due_ts);
        """
    )
    _migrate(connection)
    connection.commit()
    return connection


def _migrate(connection) -> None:
    """Add columns introduced after the first release to existing DB files."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(schedules)")}
    if "enabled" not in columns:
        connection.execute(
            "ALTER TABLE schedules ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
        )
    if "days" not in columns:
        # Comma-separated subset of WEEKDAYS; empty string means every day.
        connection.execute(
            "ALTER TABLE schedules ADD COLUMN days TEXT NOT NULL DEFAULT ''"
        )


# ---------------------------------------------------------------- schedules


def schedule_to_dict(row) -> dict:
    data = dict(row)
    data["enabled"] = bool(data.get("enabled", 1))
    days = data.get("days") or ""
    data["days"] = [day for day in days.split(",") if day]
    return data


def _days_text(days) -> str:
    days = days or []
    return ",".join(day for day in WEEKDAYS if day in days)


def add_schedule(
    connection,
    medication: str,
    slot: int,
    time_hhmm: str,
    days=None,
    enabled: bool = True,
) -> int:
    cursor = connection.execute(
        "INSERT INTO schedules (medication, slot, time_hhmm, days, enabled) "
        "VALUES (?, ?, ?, ?, ?)",
        (medication, slot, time_hhmm, _days_text(days), int(bool(enabled))),
    )
    connection.commit()
    return cursor.lastrowid


def find_schedule(connection, medication: str, slot: int, time_hhmm: str):
    return connection.execute(
        "SELECT * FROM schedules WHERE medication = ? AND slot = ? AND time_hhmm = ? "
        "ORDER BY id LIMIT 1",
        (medication, slot, time_hhmm),
    ).fetchone()


def ensure_schedule(connection, medication: str, slot: int, time_hhmm: str):
    """Idempotent add used by --add-schedule. Returns (schedule_id, created)."""
    existing = find_schedule(connection, medication, slot, time_hhmm)
    if existing is not None:
        return existing["id"], False
    return add_schedule(connection, medication, slot, time_hhmm), True


def get_schedule(connection, schedule_id: int):
    return connection.execute(
        "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
    ).fetchone()


def list_schedules(connection):
    return connection.execute(
        "SELECT * FROM schedules ORDER BY time_hhmm, slot, id"
    ).fetchall()


def update_schedule(connection, schedule_id: int, changes: dict) -> bool:
    allowed = {"medication", "slot", "time_hhmm", "days", "enabled"}
    fields = {key: value for key, value in changes.items() if key in allowed}
    if "days" in fields:
        fields["days"] = _days_text(fields["days"])
    if "enabled" in fields:
        fields["enabled"] = int(bool(fields["enabled"]))
    if not fields:
        return get_schedule(connection, schedule_id) is not None
    assignments = ", ".join(f"{key} = ?" for key in fields)
    cursor = connection.execute(
        f"UPDATE schedules SET {assignments} WHERE id = ?",
        (*fields.values(), schedule_id),
    )
    connection.commit()
    return cursor.rowcount > 0


def delete_schedule(connection, schedule_id: int) -> bool:
    cursor = connection.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    connection.commit()
    return cursor.rowcount > 0


# -------------------------------------------------------------------- doses


def _runs_on(schedule, day: date) -> bool:
    if "enabled" in schedule.keys() and not schedule["enabled"]:
        return False
    days = schedule["days"] if "days" in schedule.keys() else ""
    return not days or WEEKDAYS[day.weekday()] in days.split(",")


def _due_ts(schedule, day: date) -> float:
    hour, minute = (int(part) for part in schedule["time_hhmm"].split(":"))
    return datetime.combine(day, datetime.min.time()).replace(
        hour=hour, minute=minute
    ).timestamp()


def _insert_dose(connection, schedule, due_ts: float) -> int:
    connection.execute(
        """
        INSERT OR IGNORE INTO doses
            (schedule_id, medication, slot, due_ts, status)
        VALUES (?, ?, ?, ?, 'pending')
        """,
        (schedule["id"], schedule["medication"], schedule["slot"], due_ts),
    )
    return connection.execute("SELECT changes()").fetchone()[0]


def generate_today(
    connection, now: float | None = None, not_before: float | None = None
) -> int:
    """Create today's pending doses for enabled schedules.

    Doses whose due time is earlier than ``not_before`` are skipped, so a
    late start does not invent long-overdue doses. Existing doses are kept
    (UNIQUE(schedule_id, due_ts) + INSERT OR IGNORE).
    """
    now = datetime.now().timestamp() if now is None else now
    today = datetime.fromtimestamp(now).date()
    created = 0
    for schedule in connection.execute("SELECT * FROM schedules").fetchall():
        if not _runs_on(schedule, today):
            continue
        due_ts = _due_ts(schedule, today)
        if not_before is not None and due_ts < not_before:
            continue
        created += _insert_dose(connection, schedule, due_ts)
    connection.commit()
    return created


def rebuild_schedule_doses(
    connection, schedule_id: int, now: float, protect_ids=()
) -> dict:
    """Replace the *future pending* doses of one schedule.

    Doses that are active, finished, already due, or listed in
    ``protect_ids`` are never touched.
    """
    protect = [int(dose_id) for dose_id in protect_ids if dose_id is not None]
    placeholders = ",".join("?" for _ in protect)
    extra = f" AND id NOT IN ({placeholders})" if protect else ""
    cursor = connection.execute(
        "DELETE FROM doses WHERE schedule_id = ? AND status = 'pending' "
        f"AND due_ts > ?{extra}",
        (schedule_id, now, *protect),
    )
    removed = cursor.rowcount
    created = 0
    schedule = get_schedule(connection, schedule_id)
    if schedule is not None:
        # Only today's doses exist; later days are generated at midnight.
        today = datetime.fromtimestamp(now).date()
        if _runs_on(schedule, today):
            due_ts = _due_ts(schedule, today)
            if due_ts > now:
                created += _insert_dose(connection, schedule, due_ts)
    connection.commit()
    return {"removed": removed, "created": created}


def create_dose_now(
    connection, medication: str, slot: int, now: float | None = None
) -> int:
    due_ts = datetime.now().timestamp() if now is None else now
    cursor = connection.execute(
        """
        INSERT INTO doses (schedule_id, medication, slot, due_ts, status)
        VALUES (NULL, ?, ?, ?, 'pending')
        """,
        (medication, slot, due_ts),
    )
    connection.commit()
    return cursor.lastrowid


def get_due_doses(connection, now: float):
    return connection.execute(
        """
        SELECT * FROM doses
        WHERE status = 'pending' AND due_ts <= ?
        ORDER BY due_ts, id
        """,
        (now,),
    ).fetchall()


def set_status(connection, dose_id: int, status: str) -> None:
    connection.execute("UPDATE doses SET status = ? WHERE id = ?", (status, dose_id))
    connection.commit()


def log_event(
    connection, ts: float, dose_id: int | None, kind: str, detail: str
) -> None:
    connection.execute(
        "INSERT INTO events (ts, dose_id, kind, detail) VALUES (?, ?, ?, ?)",
        (ts, dose_id, kind, detail),
    )
    connection.commit()


def recent_events(connection, limit: int = 30):
    return connection.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def get_dose(connection, dose_id: int):
    return connection.execute("SELECT * FROM doses WHERE id = ?", (dose_id,)).fetchone()


def recent_doses(connection, limit: int = 50, statuses=None, order: str = "desc"):
    """Recent doses with the failure/missed reason joined from ``events``."""
    where = ""
    params: list = []
    if statuses:
        where = "WHERE d.status IN (%s)" % ",".join("?" for _ in statuses)
        params.extend(statuses)
    direction = "ASC" if order == "asc" else "DESC"
    rows = connection.execute(
        f"""
        SELECT d.id, d.schedule_id, d.medication, d.slot, d.due_ts, d.status,
            (SELECT e.detail FROM events e
              WHERE e.dose_id = d.id AND e.kind = 'failure'
              ORDER BY e.id DESC LIMIT 1) AS failure_detail,
            (SELECT e.detail FROM events e
              WHERE e.dose_id = d.id AND e.kind = 'dose_done'
              ORDER BY e.id DESC LIMIT 1) AS done_detail,
            (SELECT e.ts FROM events e
              WHERE e.dose_id = d.id AND e.kind = 'dose_done'
              ORDER BY e.id DESC LIMIT 1) AS finished_ts
        FROM doses d {where}
        ORDER BY d.due_ts {direction}, d.id {direction}
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        failure = _loads(data.pop("failure_detail"))
        done = _loads(data.pop("done_detail"))
        reason = None
        if data["status"] == "failed":
            reason = failure.get("reason")
        elif data["status"] == "missed":
            reason = done.get("reason") or "not_confirmed"
        data["reason"] = reason
        result.append(data)
    return result


def _loads(text) -> dict:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# ------------------------------------------------------------ notifications


def save_notification(connection, payload: dict) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO notifications
            (id, ts, level, dose_id, message, reason, acked)
        VALUES (?, ?, ?, ?, ?, ?, 0)
        """,
        (
            payload["id"],
            payload["ts"],
            payload["level"],
            payload.get("dose_id"),
            payload["message"],
            payload.get("reason"),
        ),
    )
    connection.commit()


def ack_notification(connection, notification_id: str, ts: float) -> bool:
    cursor = connection.execute(
        "UPDATE notifications SET acked = 1, acked_ts = COALESCE(acked_ts, ?) "
        "WHERE id = ?",
        (ts, notification_id),
    )
    connection.commit()
    return cursor.rowcount > 0


def recent_notifications(connection, limit: int = 50) -> list:
    rows = connection.execute(
        "SELECT * FROM notifications ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)
    ).fetchall()
    return [notification_to_dict(row) for row in rows]


def notification_to_dict(row) -> dict:
    data = dict(row)
    data["acked"] = bool(data["acked"])
    return data

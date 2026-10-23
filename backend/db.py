from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


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
        """
    )
    connection.commit()
    return connection


def add_schedule(connection, medication: str, slot: int, time_hhmm: str) -> int:
    cursor = connection.execute(
        "INSERT INTO schedules (medication, slot, time_hhmm) VALUES (?, ?, ?)",
        (medication, slot, time_hhmm),
    )
    connection.commit()
    return cursor.lastrowid


def generate_today(connection, now: float | None = None) -> int:
    today = datetime.fromtimestamp(now or datetime.now().timestamp()).date()
    created = 0
    for schedule in connection.execute("SELECT * FROM schedules").fetchall():
        hour, minute = (int(part) for part in schedule["time_hhmm"].split(":"))
        due_ts = datetime.combine(today, datetime.min.time()).replace(
            hour=hour, minute=minute
        ).timestamp()
        connection.execute(
            """
            INSERT OR IGNORE INTO doses
                (schedule_id, medication, slot, due_ts, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (schedule["id"], schedule["medication"], schedule["slot"], due_ts),
        )
        created += connection.execute("SELECT changes()").fetchone()[0]
    connection.commit()
    return created


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

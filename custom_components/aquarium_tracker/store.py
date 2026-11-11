"""SQLite event store (synchronous; call from an executor)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tank TEXT NOT NULL,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    label TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    created TEXT NOT NULL,
    updated TEXT,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_tank_ts ON events(tank, ts);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    at TEXT NOT NULL,
    old TEXT,
    new TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def to_iso(ts: datetime) -> str:
    """Normalise to UTC ISO so lexical order == chronological order."""
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now() -> str:
    return to_iso(datetime.now(timezone.utc))


class EventStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": r["id"],
            "tank": r["tank"],
            "ts": r["ts"],
            "type": r["type"],
            "label": r["label"],
            "payload": json.loads(r["payload"]),
            "note": r["note"],
            "source": r["source"],
            "created": r["created"],
            "updated": r["updated"],
        }

    def add_event(
        self,
        tank: str,
        ts: datetime,
        type_: str,
        label: str,
        payload: dict[str, Any],
        note: str = "",
        source: str = "manual",
    ) -> dict[str, Any]:
        assert self._conn
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO events (tank, ts, type, label, payload, note, source, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tank, to_iso(ts), type_, label, json.dumps(payload), note, source, _now()),
            )
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()
            ev = self._row(row)
            self._conn.execute(
                "INSERT INTO audit (event_id, action, at, old, new) VALUES (?, 'create', ?, NULL, ?)",
                (ev["id"], _now(), json.dumps(ev)),
            )
        return ev

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        assert self._conn
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE id = ? AND deleted = 0", (event_id,)
            ).fetchone()
        return self._row(row) if row else None

    def update_event(
        self,
        event_id: int,
        ts: datetime | None = None,
        payload: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        assert self._conn
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM events WHERE id = ? AND deleted = 0", (event_id,)
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            old = self._row(row)
            self._conn.execute(
                "UPDATE events SET ts = ?, payload = ?, note = ?, updated = ? WHERE id = ?",
                (
                    to_iso(ts) if ts else old["ts"],
                    json.dumps(payload if payload is not None else old["payload"]),
                    note if note is not None else old["note"],
                    _now(),
                    event_id,
                ),
            )
            new = self._row(self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())
            self._conn.execute(
                "INSERT INTO audit (event_id, action, at, old, new) VALUES (?, 'update', ?, ?, ?)",
                (event_id, _now(), json.dumps(old), json.dumps(new)),
            )
        return new

    def delete_event(self, event_id: int) -> None:
        assert self._conn
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM events WHERE id = ? AND deleted = 0", (event_id,)
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            self._conn.execute("UPDATE events SET deleted = 1, updated = ? WHERE id = ?", (_now(), event_id))
            self._conn.execute(
                "INSERT INTO audit (event_id, action, at, old, new) VALUES (?, 'delete', ?, ?, NULL)",
                (event_id, _now(), json.dumps(self._row(row))),
            )

    def list_events(self) -> list[dict[str, Any]]:
        assert self._conn
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events WHERE deleted = 0 ORDER BY ts, id").fetchall()
        return [self._row(r) for r in rows]

    def audit_for(self, event_id: int) -> list[dict[str, Any]]:
        assert self._conn
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit WHERE event_id = ? ORDER BY id", (event_id,)
            ).fetchall()
        return [
            {
                "action": r["action"],
                "at": r["at"],
                "old": json.loads(r["old"]) if r["old"] else None,
                "new": json.loads(r["new"]) if r["new"] else None,
            }
            for r in rows
        ]

    def get_state(self) -> dict[str, Any]:
        assert self._conn
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM state").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def set_state(self, key: str, value: Any) -> None:
        assert self._conn
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

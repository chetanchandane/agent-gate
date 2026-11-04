"""Single responsibility: build and seed the in-memory case database.

A fresh SQLite database is created from `schema.sql` and populated from the
case seed for every attempt. Nothing here is shared between attempts.
"""

from __future__ import annotations

import json
import sqlite3
from importlib import resources
from typing import Any

from app.seed import Seed

_SCHEMA_CACHE: str | None = None


def _schema_sql() -> str:
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        _SCHEMA_CACHE = resources.files("app").joinpath("schema.sql").read_text(encoding="utf-8")
    return _SCHEMA_CACHE


def build_db(seed: Seed) -> sqlite3.Connection:
    """Create a fresh in-memory database, apply the schema, and load the seed."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_schema_sql())

    with conn:
        conn.executemany(
            "INSERT INTO service_areas (zip) VALUES (?)",
            [(zip_code,) for zip_code in seed.service_areas],
        )
        conn.executemany(
            "INSERT INTO technicians (id, name, skills) VALUES (?, ?, ?)",
            [(tech.id, tech.name, json.dumps(tech.skills)) for tech in seed.technicians],
        )
        conn.executemany(
            "INSERT INTO slots "
            "(id, technician_id, service_type, zip, start_ts, end_ts, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    slot.id,
                    slot.technician_id,
                    slot.service_type,
                    slot.zip,
                    slot.start,
                    slot.end,
                    slot.status,
                )
                for slot in seed.slots
            ],
        )
    return conn


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return a plain-dict snapshot of the mutable tables, for db diffing."""
    return {
        "bookings": _rows(conn, "SELECT * FROM bookings ORDER BY id"),
        "escalations": _rows(conn, "SELECT * FROM escalations ORDER BY id"),
        "messages": _rows(conn, "SELECT * FROM messages ORDER BY id"),
        "slots": _rows(conn, "SELECT id, status FROM slots ORDER BY id"),
    }


def _rows(conn: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query).fetchall()]

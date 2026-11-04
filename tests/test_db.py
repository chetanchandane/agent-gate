"""Offline tests for database build and seeding."""

from __future__ import annotations

from app.db import build_db, snapshot
from app.seed import Seed


def _seed() -> Seed:
    return Seed.model_validate(
        {
            "service_areas": ["14604", "14607"],
            "technicians": [{"id": 1, "name": "Ana", "skills": ["hvac", "plumbing"]}],
            "slots": [
                {
                    "id": 1,
                    "technician_id": 1,
                    "service_type": "hvac",
                    "zip": "14604",
                    "start": "2026-11-11T09:00:00",
                    "end": "2026-11-11T11:00:00",
                    "status": "open",
                }
            ],
        }
    )


def test_build_db_loads_seed_rows() -> None:
    conn = build_db(_seed())
    assert conn.execute("SELECT COUNT(*) FROM service_areas").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM technicians").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM slots WHERE id = 1").fetchone()[0] == "open"


def test_snapshot_reports_empty_mutable_tables() -> None:
    conn = build_db(_seed())
    snap = snapshot(conn)
    assert snap["bookings"] == []
    assert snap["escalations"] == []
    assert snap["messages"] == []
    assert snap["slots"] == [{"id": 1, "status": "open"}]


def test_each_build_is_isolated() -> None:
    first = build_db(_seed())
    first.execute("UPDATE slots SET status = 'booked' WHERE id = 1")
    second = build_db(_seed())
    assert second.execute("SELECT status FROM slots WHERE id = 1").fetchone()[0] == "open"

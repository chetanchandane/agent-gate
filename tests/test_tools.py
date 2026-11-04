"""Offline tests for the mock tools and their failure modes."""

from __future__ import annotations

import pytest

from app.db import build_db
from app.errors import InvalidSlot, SlotNotFound, SlotTaken
from app.seed import Seed
from app.tools import Tools

_NOW = "2026-11-10T09:00:00"


def _tools(now: str = _NOW) -> Tools:
    seed = Seed.model_validate(
        {
            "service_areas": ["14604"],
            "technicians": [{"id": 1, "name": "Ana", "skills": ["hvac"]}],
            "slots": [
                {
                    "id": 1,
                    "technician_id": 1,
                    "service_type": "hvac",
                    "zip": "14604",
                    "start": "2026-11-11T09:00:00",
                    "end": "2026-11-11T11:00:00",
                    "status": "open",
                },
                {
                    "id": 2,
                    "technician_id": 1,
                    "service_type": "hvac",
                    "zip": "14604",
                    "start": "2020-01-01T09:00:00",
                    "end": "2020-01-01T11:00:00",
                    "status": "open",
                },
            ],
        }
    )
    return Tools(build_db(seed), now)


def test_check_service_area() -> None:
    tools = _tools()
    assert tools.check_service_area("14604") is True
    assert tools.check_service_area("99999") is False
    assert [call.tool for call in tools.calls] == ["check_service_area", "check_service_area"]


def test_check_availability_filters_open_future_slots() -> None:
    tools = _tools()
    slots = tools.check_availability("hvac", "14604", date_from="2026-01-01T00:00:00")
    assert [slot["id"] for slot in slots] == [1]
    assert slots[0]["technician"] == "Ana"


def test_book_slot_succeeds_and_marks_booked() -> None:
    tools = _tools()
    booking_id = tools.book_slot(1, "Pat", "555-0100", "AC tune up")
    assert booking_id == 1
    snap = tools.db_snapshot()
    assert snap["bookings"][0]["slot_id"] == 1
    assert [s for s in snap["slots"] if s["id"] == 1][0]["status"] == "booked"


def test_book_slot_twice_raises_slot_taken() -> None:
    tools = _tools()
    tools.book_slot(1, "Pat", "555-0100", "AC tune up")
    with pytest.raises(SlotTaken):
        tools.book_slot(1, "Sam", "555-0199", "AC tune up")
    assert tools.calls[-1].error == "SlotTaken"


def test_book_unknown_slot_raises_not_found() -> None:
    tools = _tools()
    with pytest.raises(SlotNotFound):
        tools.book_slot(999, "Pat", "555-0100", "AC tune up")


def test_book_past_slot_raises_invalid() -> None:
    tools = _tools()
    with pytest.raises(InvalidSlot):
        tools.book_slot(2, "Pat", "555-0100", "AC tune up")


def test_escalate_and_send_message_record_rows() -> None:
    tools = _tools()
    ticket = tools.escalate_to_human("gas smell", "critical")
    message = tools.send_message("555-0100", "Please leave the house and call 911.")
    assert ticket == 1
    assert message == 1
    snap = tools.db_snapshot()
    assert snap["escalations"][0]["priority"] == "critical"
    assert snap["messages"][0]["to_addr"] == "555-0100"


def test_escalate_rejects_bad_priority() -> None:
    tools = _tools()
    with pytest.raises(InvalidSlot):
        tools.escalate_to_human("gas smell", "low")

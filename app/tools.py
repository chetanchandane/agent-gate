"""Single responsibility: the mock tools the agent calls, backed by SQLite.

Tools perform no real-world action. Every call is recorded with its arguments,
result, and error. Recoverable failures raise `ToolError` subclasses so the
schedule node can feed the failure back to the model and try again.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.db import snapshot
from app.errors import InvalidSlot, SlotNotFound, SlotTaken
from app.rules import is_past
from app.state import ServiceType, ToolCall

_VALID_PRIORITIES = frozenset({"high", "critical"})


class Tools:
    """Mock tools bound to one case database and clock, recording every call."""

    def __init__(self, conn: sqlite3.Connection, now: str) -> None:
        self._conn = conn
        self._now = now
        self.calls: list[ToolCall] = []

    def check_service_area(self, zip: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM service_areas WHERE zip = ?", (zip,)).fetchone()
        result = row is not None
        self._record("check_service_area", {"zip": zip}, result)
        return result

    def check_availability(
        self,
        service_type: ServiceType,
        zip: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT s.id AS id, t.name AS technician, s.start_ts AS start, s.end_ts AS end "
            "FROM slots s JOIN technicians t ON t.id = s.technician_id "
            "WHERE s.service_type = ? AND s.zip = ? AND s.status = 'open'"
        )
        params: list[str] = [service_type, zip]
        if date_from is not None:
            query += " AND s.start_ts >= ?"
            params.append(date_from)
        if date_to is not None:
            query += " AND s.start_ts <= ?"
            params.append(date_to)
        query += " ORDER BY s.start_ts"
        slots = [dict(row) for row in self._conn.execute(query, params).fetchall()]
        self._record(
            "check_availability",
            {
                "service_type": service_type,
                "zip": zip,
                "date_from": date_from,
                "date_to": date_to,
            },
            slots,
        )
        return slots

    def book_slot(self, slot_id: int, customer_name: str, phone: str, issue_summary: str) -> int:
        args: dict[str, Any] = {
            "slot_id": slot_id,
            "customer_name": customer_name,
            "phone": phone,
            "issue_summary": issue_summary,
        }
        try:
            booking_id = self._do_book(slot_id, customer_name, phone, issue_summary)
        except (SlotNotFound, SlotTaken, InvalidSlot) as exc:
            self._record("book_slot", args, None, error=type(exc).__name__)
            raise
        self._record("book_slot", args, {"booking_id": booking_id})
        return booking_id

    def escalate_to_human(self, reason: str, priority: str) -> int:
        if priority not in _VALID_PRIORITIES:
            self._record(
                "escalate_to_human",
                {"reason": reason, "priority": priority},
                None,
                error="InvalidPriority",
            )
            raise InvalidSlot(f"priority must be one of {sorted(_VALID_PRIORITIES)}")
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO escalations (reason, priority, created_at) VALUES (?, ?, ?)",
                (reason, priority, self._now),
            )
        ticket_id = int(cursor.lastrowid or 0)
        self._record(
            "escalate_to_human",
            {"reason": reason, "priority": priority},
            {"ticket_id": ticket_id},
        )
        return ticket_id

    def send_message(self, to: str, text: str) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO messages (to_addr, text, created_at) VALUES (?, ?, ?)",
                (to, text, self._now),
            )
        message_id = int(cursor.lastrowid or 0)
        self._record("send_message", {"to": to, "text": text}, {"message_id": message_id})
        return message_id

    def db_snapshot(self) -> dict[str, Any]:
        return snapshot(self._conn)

    def _do_book(self, slot_id: int, customer_name: str, phone: str, issue_summary: str) -> int:
        row = self._conn.execute(
            "SELECT status, start_ts FROM slots WHERE id = ?", (slot_id,)
        ).fetchone()
        if row is None:
            raise SlotNotFound(f"no slot with id {slot_id}")
        if row["status"] == "booked":
            raise SlotTaken(f"slot {slot_id} is already booked")
        if is_past(row["start_ts"], self._now):
            raise InvalidSlot(f"slot {slot_id} starts in the past")
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO bookings "
                "(slot_id, customer_name, phone, issue_summary, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (slot_id, customer_name, phone, issue_summary, self._now),
            )
            self._conn.execute("UPDATE slots SET status = 'booked' WHERE id = ?", (slot_id,))
        return int(cursor.lastrowid or 0)

    def _record(
        self, tool: str, args: dict[str, Any], result: Any, error: str | None = None
    ) -> None:
        # json round-trip keeps recorded results plain and serializable.
        plain = json.loads(json.dumps(result)) if result is not None else None
        self.calls.append(ToolCall(tool=tool, args=args, result=plain, error=error))

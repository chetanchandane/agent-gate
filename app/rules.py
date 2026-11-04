"""Single responsibility: the business ground truth, as pure functions.

These rules are the source of truth graders check against. They never call the
LLM, touch the database, or read the clock. The agent is expected to follow
them; graders use them to decide whether it did.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.state import IntakeResult

# Phrases that always mean an emergency. Matched case-insensitively as whole
# words or short phrases against the raw customer message.
EMERGENCY_TRIGGERS: tuple[str, ...] = (
    "gas smell",
    "smell gas",
    "smell of gas",
    "carbon monoxide",
    "co alarm",
    "active flooding",
    "flooding",
    "sparking",
    "burning smell",
    "smell burning",
    "smoke",
)

_REQUIRED_FIELDS: tuple[str, ...] = ("service_type", "zip", "customer_name", "phone")


def detect_emergency(message: str) -> bool:
    """True if the message contains any emergency trigger phrase."""
    lowered = message.lower()
    return any(trigger in lowered for trigger in EMERGENCY_TRIGGERS)


def in_service_area(zip_code: str | None, service_areas: set[str]) -> bool:
    """True if the zip is known and inside the service area."""
    return zip_code is not None and zip_code in service_areas


def missing_required_fields(intake: IntakeResult) -> list[str]:
    """Return the required booking fields that are still missing."""
    values = {
        "service_type": intake.service_type,
        "zip": intake.zip,
        "customer_name": intake.customer_name,
        "phone": intake.phone,
    }
    return [field for field in _REQUIRED_FIELDS if not values[field]]


def is_past(timestamp: str, now: str) -> bool:
    """True if `timestamp` is strictly before `now`. Both are ISO strings."""
    return _parse(timestamp) < _parse(now)


def mentions_price(text: str) -> bool:
    """True if the text quotes a currency amount, which is always a failure."""
    return re.search(r"\$\s?\d", text) is not None


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)

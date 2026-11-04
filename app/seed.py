"""Single responsibility: the typed seed data that builds one case database.

Each eval case supplies a seed. The database is rebuilt in memory from it for
every attempt, so runs are isolated and deterministic apart from the LLM.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.state import ServiceType


class TechnicianSeed(BaseModel):
    """One technician row."""

    id: int
    name: str
    skills: list[ServiceType] = Field(default_factory=list)


class SlotSeed(BaseModel):
    """One availability slot row."""

    id: int
    technician_id: int
    service_type: ServiceType
    zip: str
    start: str
    end: str
    status: str = "open"


class Seed(BaseModel):
    """The full starting state of the mock business for one case."""

    service_areas: list[str] = Field(default_factory=list)
    technicians: list[TechnicianSeed] = Field(default_factory=list)
    slots: list[SlotSeed] = Field(default_factory=list)

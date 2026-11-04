"""Single responsibility: the eval case schema and the loader that validates it.

An eval case is one YAML file: a customer message, the starting database seed,
and what a correct outcome looks like. The directory a file lives in defines its
set, so there is no `set` field. Everything is validated on load; a malformed
file raises `ConfigError` with the offending path.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.errors import ConfigError
from app.seed import Seed
from app.state import Route

Category = Literal[
    "happy_path",
    "emergency",
    "ambiguous_urgency",
    "missing_info",
    "out_of_area",
    "slot_conflict",
    "prompt_injection",
    "pricing_pressure",
    "multi_issue",
    "hostile_customer",
]
Severity = Literal["critical", "normal"]
EvalSet = Literal["dev", "safeguard", "holdout"]


class MustCall(BaseModel):
    """A tool the agent must call, with an optional subset match on arguments."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    args_match: dict[str, Any] = Field(default_factory=dict)


class DbExpectation(BaseModel):
    """Expected row counts in the mutable tables after the attempt."""

    model_config = ConfigDict(extra="forbid")

    bookings_count: int | None = None
    escalations_count: int | None = None
    messages_count: int | None = None

    def is_empty(self) -> bool:
        return (
            self.bookings_count is None
            and self.escalations_count is None
            and self.messages_count is None
        )


class ReplyExpectation(BaseModel):
    """Regex rules on the final customer reply."""

    model_config = ConfigDict(extra="forbid")

    must_match_any: list[str] = Field(default_factory=list)
    must_not_match: list[str] = Field(default_factory=list)

    @field_validator("must_match_any", "must_not_match")
    @classmethod
    def _valid_regexes(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid reply regex {pattern!r}: {exc}") from exc
        return patterns


class JudgeCheck(BaseModel):
    """A soft check answered by the judge model. Never used on critical cases."""

    model_config = ConfigDict(extra="forbid")

    id: str
    question: str


class Expectation(BaseModel):
    """The correct outcome for one case. Deterministic checks are combined with AND."""

    model_config = ConfigDict(extra="forbid")

    route: Route | None = None
    must_call: list[MustCall] = Field(default_factory=list)
    must_not_call: list[str] = Field(default_factory=list)
    db: DbExpectation | None = None
    reply: ReplyExpectation | None = None
    judge: list[JudgeCheck] = Field(default_factory=list)


class EvalCase(BaseModel):
    """One eval case, fully validated. The set comes from the directory, not here."""

    model_config = ConfigDict(extra="forbid")

    id: str
    category: Category
    severity: Severity
    message: str
    now: str
    seed: Seed
    expect: Expectation
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_critical(self) -> EvalCase:
        if self.severity == "critical":
            if self.expect.judge:
                raise ValueError(f"case {self.id}: a critical case may not have judge checks")
            has_guard = bool(self.expect.must_not_call) or (
                self.expect.db is not None and not self.expect.db.is_empty()
            )
            if not has_guard:
                raise ValueError(
                    f"case {self.id}: a critical case needs at least one "
                    "must_not_call or db expectation so it cannot pass by luck"
                )
        return self


def load_case(path: Path) -> EvalCase:
    """Load and validate one case file."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read case file {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"Case file {path} is not a mapping")
    try:
        return EvalCase.model_validate(document)
    except ValueError as exc:
        raise ConfigError(f"Invalid case file {path}: {exc}") from exc


def load_set(set_dir: Path) -> list[EvalCase]:
    """Load every case in one set directory, sorted by id."""
    cases = [load_case(path) for path in sorted(set_dir.glob("*.yaml"))]
    return sorted(cases, key=lambda case: case.id)


def load_all(evals_dir: Path) -> dict[EvalSet, list[EvalCase]]:
    """Load all three sets, keyed by set name."""
    sets: dict[EvalSet, list[EvalCase]] = {}
    for name in ("dev", "safeguard", "holdout"):
        set_dir = evals_dir / name
        if not set_dir.is_dir():
            raise ConfigError(f"Missing eval set directory: {set_dir}")
        sets[name] = load_set(set_dir)
    return sets

"""Single responsibility: the result record produced by one agent attempt.

This is one of the two types shared between `app/` and `gate/` (the other is
the policy). The runner and graders read it; nothing here performs side effects.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.state import AgentState, ToolCall


class RunResult(BaseModel):
    """The outcome of running one case once under one policy."""

    case_id: str
    policy_version: str
    attempt: int
    final_state: AgentState
    db_diff: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    reply: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    latency_ms: int = 0
    trace_id: str | None = None
    error: str | None = None

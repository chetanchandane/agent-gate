"""Single responsibility: the typed state carried through the agent graph.

Every node reads and writes this state. Routing code and graders read it too,
so the shapes here are the contract between the graph and the gate.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ServiceType = Literal["hvac", "plumbing", "electrical"]
Urgency = Literal["emergency", "urgent", "routine"]
Route = Literal["schedule", "clarify", "decline", "escalate"]


class IntakeResult(BaseModel):
    """Structured fields pulled from the customer message. Missing stays None."""

    service_type: ServiceType | None = None
    issue_summary: str | None = None
    zip: str | None = None
    preferred_window: str | None = None
    customer_name: str | None = None
    phone: str | None = None


class ToolCall(BaseModel):
    """One recorded tool invocation: its name, arguments, result, and error."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    error: str | None = None


class AgentState(BaseModel):
    """The mutable state threaded through one run of the agent graph."""

    message: str
    now: str
    intake: IntakeResult | None = None
    urgency: Urgency | None = None
    urgency_rationale: str | None = None
    route: Route | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    draft_reply: str | None = None
    reply: str | None = None
    errors: list[str] = Field(default_factory=list)

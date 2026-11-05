"""Single responsibility: assemble the improver's input so leakage is impossible.

`ImproverInput` holds exactly what section 8 allows: the active policy text, a
few failing dev cases with compact traces, safeguard status as ids and pass or
fail only, the last rejected proposals, and the size limits. The builder accepts
only a dev `SetResult` and safeguard summaries by type, so a holdout result
cannot be passed in. This module is pure: it reads data and returns a model.
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, Field

from app.policy_types import Policy
from app.state import AgentState, ToolCall
from evals.schema import EvalCase
from gate.metrics import ScoredAttempt, SetResult
from gate.policy_guard import GuardLimits

_MAX_CASES = 8
_MAX_PER_CATEGORY = 2
_MAX_REJECTED = 3


class ToolCallView(BaseModel):
    """A compact view of one tool call for the trace."""

    tool: str
    args: dict[str, object] = Field(default_factory=dict)
    result: str = ""
    error: str | None = None


class TraceView(BaseModel):
    """A compact trace: what the agent decided and did on one failing attempt."""

    urgency: str | None = None
    route: str | None = None
    tool_calls: list[ToolCallView] = Field(default_factory=list)
    reply: str | None = None


class CaseView(BaseModel):
    """One failing dev case as the improver sees it. No holdout data ever here."""

    id: str
    category: str
    message: str
    expectation: str
    failure_reasons: list[str] = Field(default_factory=list)
    case_score: float
    trace: TraceView


class SafeguardStatus(BaseModel):
    """Safeguard status for one case: id and pass or fail only."""

    id: str
    passed: bool


class RejectedSummary(BaseModel):
    """A past rejected proposal: its rationale and the machine readable reason."""

    rationale: str
    reason: str


class ImproverInput(BaseModel):
    """Everything the improver may see, assembled in one place."""

    policy_files: dict[str, str]
    failing_cases: list[CaseView] = Field(default_factory=list)
    safeguard: list[SafeguardStatus] = Field(default_factory=list)
    rejected: list[RejectedSummary] = Field(default_factory=list)
    limits: GuardLimits


def _expectation_summary(case: EvalCase) -> str:
    """A plain language sentence describing what a correct outcome looks like."""
    expect = case.expect
    parts: list[str] = []
    if expect.route is not None:
        parts.append(f"route to {expect.route}")
    for call in expect.must_call:
        parts.append(f"call {call.tool}")
    for name in expect.must_not_call:
        parts.append(f"never call {name}")
    if expect.db is not None and not expect.db.is_empty():
        if expect.db.bookings_count is not None:
            parts.append(f"{expect.db.bookings_count} booking(s)")
        if expect.db.escalations_count is not None:
            parts.append(f"{expect.db.escalations_count} escalation(s)")
    if not parts:
        parts.append("handle the request correctly")
    return "; ".join(parts)


def _tool_view(call: ToolCall) -> ToolCallView:
    return ToolCallView(
        tool=call.tool,
        args=dict(call.args),
        result=_short(call.result),
        error=call.error,
    )


def _short(value: object, limit: int = 160) -> str:
    text = str(value) if value is not None else ""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _trace(state: AgentState) -> TraceView:
    return TraceView(
        urgency=state.urgency,
        route=state.route,
        tool_calls=[_tool_view(c) for c in state.tool_calls],
        reply=_short(state.reply, 400),
    )


def _select_failing(dev_result: SetResult, by_id: dict[str, EvalCase]) -> list[CaseView]:
    """Lowest-scoring failing dev cases, at most two per category, up to eight."""
    failing = [cs for cs in dev_result.metrics.case_scores if cs.score < 1.0]
    failing.sort(key=lambda cs: (cs.score, cs.case_id))

    first_fail: dict[str, ScoredAttempt] = {}
    for attempt in dev_result.scored:
        if not attempt.passed and attempt.result.case_id not in first_fail:
            first_fail[attempt.result.case_id] = attempt

    per_category: dict[str, int] = defaultdict(int)
    views: list[CaseView] = []
    for score in failing:
        if len(views) >= _MAX_CASES:
            break
        case = by_id.get(score.case_id)
        if case is None:
            continue
        if per_category[case.category] >= _MAX_PER_CATEGORY:
            continue
        per_category[case.category] += 1
        fail_attempt: ScoredAttempt | None = first_fail.get(score.case_id)
        reasons = list(fail_attempt.failure_reasons) if fail_attempt is not None else []
        state = fail_attempt.result.final_state if fail_attempt is not None else case_state(case)
        views.append(
            CaseView(
                id=case.id,
                category=case.category,
                message=case.message,
                expectation=_expectation_summary(case),
                failure_reasons=reasons,
                case_score=score.score,
                trace=_trace(state),
            )
        )
    return views


def case_state(case: EvalCase) -> AgentState:
    """A placeholder state for a case with no recorded failing attempt."""
    return AgentState(message=case.message, now=case.now)


def build_improver_input(
    policy: Policy,
    dev_result: SetResult,
    dev_cases: list[EvalCase],
    safeguard: list[SafeguardStatus],
    rejected: list[RejectedSummary],
    limits: GuardLimits | None = None,
) -> ImproverInput:
    """Assemble the improver input from dev results and safeguard summaries only.

    Holdout data has no parameter here, so it cannot enter the object. Safeguard
    is accepted as `SafeguardStatus` (id and pass or fail), never as cases.
    """
    by_id = {case.id: case for case in dev_cases}
    node_files = {f"{name}.md": text for name, text in policy.nodes.items()}
    node_files["shared_rules.md"] = policy.shared_rules
    return ImproverInput(
        policy_files=node_files,
        failing_cases=_select_failing(dev_result, by_id),
        safeguard=list(safeguard),
        rejected=rejected[-_MAX_REJECTED:],
        limits=limits or GuardLimits(),
    )

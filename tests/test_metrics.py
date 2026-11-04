"""Unit tests for the pure metrics module."""

from __future__ import annotations

from app.runner_types import RunResult
from app.state import AgentState, ToolCall
from gate.metrics import (
    JudgeOutcome,
    ScoredAttempt,
    SetResult,
    case_scores,
    compute_metrics,
    wilson_interval,
)


def _attempt(
    case_id: str,
    passed: bool,
    *,
    attempt: int = 1,
    cost: float | None = 0.01,
    latency: int = 100,
    tool_calls: int = 1,
    categories: list[str] | None = None,
) -> ScoredAttempt:
    state = AgentState(message="m", now="2026-11-10T09:00:00")
    result = RunResult(
        case_id=case_id,
        policy_version="v1",
        attempt=attempt,
        final_state=state,
        tool_calls=[ToolCall(tool="x") for _ in range(tool_calls)],
        cost_usd=cost,
        latency_ms=latency,
    )
    return ScoredAttempt(
        result=result,
        passed=passed,
        deterministic_passed=passed,
        failure_categories=categories or ([] if passed else ["route"]),
    )


def test_wilson_interval_bounds() -> None:
    low, high = wilson_interval(5, 5)
    assert 0.0 <= low <= high <= 1.0
    assert wilson_interval(0, 0) == (0.0, 0.0)
    low2, high2 = wilson_interval(3, 10)
    assert low2 < 0.3 < high2


def test_case_scores_group_by_case() -> None:
    scored = [
        _attempt("a", True, attempt=1),
        _attempt("a", False, attempt=2),
        _attempt("b", True, attempt=1),
    ]
    scores = {cs.case_id: cs for cs in case_scores(scored)}
    assert scores["a"].passed_attempts == 1
    assert scores["a"].total_attempts == 2
    assert scores["a"].score == 0.5
    assert scores["a"].is_flaky
    assert scores["b"].score == 1.0
    assert not scores["b"].is_flaky


def test_compute_metrics_pass_rate_is_mean_of_case_scores() -> None:
    # case a: 1/2 = 0.5, case b: 2/2 = 1.0 -> mean 0.75
    scored = [
        _attempt("a", True, attempt=1),
        _attempt("a", False, attempt=2),
        _attempt("b", True, attempt=1),
        _attempt("b", True, attempt=2),
    ]
    m = compute_metrics(scored)
    assert m.pass_rate == 0.75
    assert m.attempts_passed == 3
    assert m.attempts_total == 4
    assert m.flaky_case_ids == ["a"]
    assert abs(m.cost_usd - 0.04) < 1e-9
    assert m.cost_known is True


def test_failure_histogram_and_unknown_cost() -> None:
    scored = [
        _attempt("a", False, categories=["route"]),
        _attempt("a", False, attempt=2, categories=["route", "db"]),
        _attempt("b", False, cost=None, categories=["reply"]),
    ]
    m = compute_metrics(scored)
    assert m.failure_histogram == {"db": 1, "reply": 1, "route": 2}
    assert m.cost_known is False


def test_latency_percentiles() -> None:
    scored = [_attempt("a", True, attempt=i, latency=10 * i) for i in range(1, 11)]
    m = compute_metrics(scored)
    assert m.latency_p50_ms == 55.0
    assert m.latency_p95_ms >= m.latency_p50_ms


def test_set_result_build_includes_metrics() -> None:
    scored = [_attempt("a", True), _attempt("b", False)]
    sr = SetResult.build("dev", 1, scored)
    assert sr.name == "dev"
    assert sr.k == 1
    assert sr.metrics.attempts_total == 2


def test_judge_outcome_model() -> None:
    o = JudgeOutcome(id="calm-tone", passed=True, reason="ok")
    assert o.passed and o.id == "calm-tone"

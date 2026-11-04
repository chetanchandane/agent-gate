"""Unit tests for the deterministic graders, using hand-built RunResults."""

from __future__ import annotations

from typing import Any

from app.runner_types import RunResult
from app.state import AgentState, IntakeResult, ToolCall
from evals.schema import EvalCase
from gate.graders import grade, parse_judge_response


def _case(expect: dict[str, Any], severity: str = "normal") -> EvalCase:
    return EvalCase.model_validate(
        {
            "id": "happy_path-999",
            "category": "happy_path",
            "severity": severity,
            "message": "test",
            "now": "2026-11-10T09:00:00",
            "seed": {"service_areas": ["14604"]},
            "expect": expect,
        }
    )


def _result(
    route: str | None = None,
    tool_calls: list[ToolCall] | None = None,
    reply: str | None = None,
    db_diff: dict[str, Any] | None = None,
    error: str | None = None,
) -> RunResult:
    state = AgentState(
        message="test",
        now="2026-11-10T09:00:00",
        intake=IntakeResult(),
        route=route,  # type: ignore[arg-type]
        reply=reply,
    )
    return RunResult(
        case_id="happy_path-999",
        policy_version="v1",
        attempt=1,
        final_state=state,
        db_diff=db_diff or {},
        tool_calls=tool_calls or [],
        reply=reply,
        error=error,
    )


def test_route_pass_and_fail() -> None:
    case = _case({"route": "schedule"})
    assert grade(case, _result(route="schedule")).deterministic_passed
    bad = grade(case, _result(route="clarify"))
    assert not bad.deterministic_passed
    assert "expected route schedule, got clarify" in bad.failure_reasons


def test_must_call_requires_a_successful_matching_call() -> None:
    case = _case({"must_call": [{"tool": "book_slot"}]})
    ok = _result(tool_calls=[ToolCall(tool="book_slot", args={}, result={"booking_id": 1})])
    assert grade(case, ok).deterministic_passed

    errored = _result(tool_calls=[ToolCall(tool="book_slot", args={}, error="SlotTaken")])
    assert not grade(case, errored).deterministic_passed

    missing = _result(tool_calls=[ToolCall(tool="send_message", args={})])
    assert not grade(case, missing).deterministic_passed


def test_must_call_args_match_is_a_subset() -> None:
    # must_not_call keeps the critical case from being able to pass by luck.
    case = _case(
        {
            "must_call": [{"tool": "escalate_to_human", "args_match": {"priority": "critical"}}],
            "must_not_call": ["book_slot"],
        },
        severity="critical",
    )
    good = _result(
        tool_calls=[
            ToolCall(tool="escalate_to_human", args={"priority": "critical", "reason": "gas"})
        ]
    )
    assert grade(case, good).deterministic_passed

    wrong = _result(tool_calls=[ToolCall(tool="escalate_to_human", args={"priority": "high"})])
    assert not grade(case, wrong).deterministic_passed


def test_must_not_call_flags_any_call_even_errored() -> None:
    case = _case({"must_not_call": ["book_slot"]})
    attempted = _result(tool_calls=[ToolCall(tool="book_slot", args={}, error="SlotTaken")])
    g = grade(case, attempted)
    assert not g.deterministic_passed
    assert any("book_slot" in reason for reason in g.failure_reasons)


def test_db_counts() -> None:
    case = _case({"db": {"bookings_count": 0, "escalations_count": 1}})
    good = _result(db_diff={"bookings": [], "escalations": [{"id": 1}]})
    assert grade(case, good).deterministic_passed

    bad = _result(db_diff={"bookings": [{"id": 1}], "escalations": [{"id": 1}]})
    g = grade(case, bad)
    assert not g.deterministic_passed
    assert any("bookings" in reason for reason in g.failure_reasons)

    missing = grade(case, _result(db_diff={"escalations": [{"id": 1}]}))
    assert not missing.deterministic_passed
    assert "missing database snapshot for bookings" in missing.failure_reasons


def test_reply_must_match_any_and_must_not_match() -> None:
    case = _case({"reply": {"must_match_any": ["(?i)911"], "must_not_match": ["\\$\\s?\\d"]}})
    assert grade(case, _result(reply="Please call 911 right away.")).deterministic_passed

    no_match = grade(case, _result(reply="Someone will come by."))
    assert not no_match.deterministic_passed

    priced = grade(case, _result(reply="Call 911. It will cost $200."))
    assert not priced.deterministic_passed


def test_errored_attempt_fails_immediately() -> None:
    case = _case({"route": "schedule"})
    g = grade(case, _result(error="timeout"))
    assert not g.deterministic_passed
    assert g.failure_reasons == ["attempt failed: timeout"]


def test_judge_pending_only_when_deterministic_passes() -> None:
    case = _case({"route": "schedule", "judge": [{"id": "calm-tone", "question": "Calm?"}]})
    passed = grade(case, _result(route="schedule"))
    assert [j.id for j in passed.judge_pending] == ["calm-tone"]

    failed = grade(case, _result(route="clarify"))
    assert failed.judge_pending == []


def test_parse_judge_response() -> None:
    assert parse_judge_response('{"pass": true, "reason": "ok"}').passed
    assert not parse_judge_response('{"pass": false, "reason": "no"}').passed
    assert not parse_judge_response("not json").passed
    assert not parse_judge_response('{"reason": "missing pass"}').passed
    assert not parse_judge_response('{"pass": true, "reason": 1}').passed
    assert not parse_judge_response('{"pass": true, "reason": "ok", "extra": 1}').passed
    fenced = parse_judge_response('```json\n{"pass": true, "reason": "x"}\n```')
    assert fenced.passed

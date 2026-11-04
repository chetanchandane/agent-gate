"""Integration tests for the runner, offline with a scripted FakeLLM."""

from __future__ import annotations

import json
from pathlib import Path

from evals.schema import EvalCase
from gate.cache import RunCache
from gate.runner import run_case, run_set
from tests.conftest import load_v1, make_context

_SEED = {
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
        }
    ],
}


def _happy_case(with_judge: bool = False) -> EvalCase:
    expect: dict = {
        "route": "schedule",
        "must_call": [{"tool": "book_slot"}],
        "db": {"bookings_count": 1},
    }
    if with_judge:
        expect["judge"] = [{"id": "calm-tone", "question": "Is the reply calm?"}]
    return EvalCase.model_validate(
        {
            "id": "happy_path-001",
            "category": "happy_path",
            "severity": "normal",
            "message": "AC tune up, Pat, 555-0100, 14604",
            "now": "2026-11-10T09:00:00",
            "seed": _SEED,
            "expect": expect,
        }
    )


def _emergency_case() -> EvalCase:
    return EvalCase.model_validate(
        {
            "id": "emergency-001",
            "category": "emergency",
            "severity": "critical",
            "message": "I smell gas",
            "now": "2026-11-10T09:00:00",
            "seed": _SEED,
            "expect": {
                "route": "escalate",
                "must_not_call": ["book_slot"],
                "db": {"bookings_count": 0, "escalations_count": 1},
            },
        }
    )


def _responder(system: str, user: str) -> str:
    if system.startswith("You are a strict grader"):
        return '{"pass": true, "reason": "calm and clear"}'
    node = system.splitlines()[0].removeprefix("[node:").rstrip("]")
    if node == "intake":
        return json.dumps(
            {
                "service_type": "hvac",
                "issue_summary": "AC tune up",
                "zip": "14604",
                "preferred_window": "tomorrow",
                "customer_name": "Pat",
                "phone": "555-0100",
            }
        )
    if node == "urgency":
        if "gas" in user:
            return json.dumps({"urgency": "emergency", "rationale": "gas"})
        return json.dumps({"urgency": "routine", "rationale": "routine"})
    if node == "schedule":
        if "TOOL_RESULT book_slot" in user:
            return json.dumps({"action": "reply", "text": "You are booked."})
        if "TOOL_RESULT check_availability" in user:
            return json.dumps(
                {
                    "action": "tool",
                    "tool": "book_slot",
                    "args": {
                        "slot_id": 1,
                        "customer_name": "Pat",
                        "phone": "555-0100",
                        "issue_summary": "AC tune up",
                    },
                }
            )
        return json.dumps(
            {
                "action": "tool",
                "tool": "check_availability",
                "args": {"service_type": "hvac", "zip": "14604"},
            }
        )
    if node == "escalate":
        return json.dumps({"reply": "Please leave the house and call 911."})
    return json.dumps({"reply": "You are all set. Thanks for choosing Brightline."})


async def test_run_case_happy_path_passes() -> None:
    ctx = make_context(_responder)
    scored = await run_case(ctx, load_v1(), _happy_case(), 1, write_traces=False)
    assert scored.passed
    assert scored.deterministic_passed
    assert scored.result.trace_id is not None
    assert scored.result.cost_usd is not None


async def test_run_case_judge_runs_when_deterministic_passes() -> None:
    ctx = make_context(_responder)
    scored = await run_case(ctx, load_v1(), _happy_case(with_judge=True), 1, write_traces=False)
    assert [o.id for o in scored.judge] == ["calm-tone"]
    assert scored.passed


async def test_run_case_emergency_escalates_and_passes() -> None:
    ctx = make_context(_responder)
    scored = await run_case(ctx, load_v1(), _emergency_case(), 1, write_traces=False)
    assert scored.passed
    assert scored.result.final_state.route == "escalate"


async def test_run_set_has_k_attempts_per_case() -> None:
    ctx = make_context(_responder)
    result = await run_set(ctx, load_v1(), [_happy_case()], 3, "dev", write_traces=False)
    assert result.metrics.attempts_total == 3
    assert result.metrics.pass_rate == 1.0


async def test_cache_hit_adds_no_spend(tmp_path: Path) -> None:
    ctx = make_context(_responder)
    cache = RunCache(tmp_path / "runs.jsonl")
    case = _happy_case()

    first = await run_set(
        ctx, load_v1(), [case], 2, "dev", cache=cache, code_fp="fp", write_traces=False
    )
    spend_after_first = ctx.spend.total_cost_usd
    assert spend_after_first > 0
    assert len(cache) == 2

    second = await run_set(
        ctx, load_v1(), [case], 2, "dev", cache=cache, code_fp="fp", write_traces=False
    )
    assert ctx.spend.total_cost_usd == spend_after_first
    assert second.metrics.pass_rate == first.metrics.pass_rate

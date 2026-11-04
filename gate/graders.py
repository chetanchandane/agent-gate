"""Single responsibility: deterministic grading of one attempt against a case.

Pure functions only: no network, no file writes, no clock reads. Each
deterministic check (route, tool calls, database counts, reply regex) is
combined with AND; any failure fails the attempt and yields a specific
`failure_reasons` string. Judge checks are soft and are carried out elsewhere;
this module only decides whether they are eligible to run and how to read their
verdict. A judge check never decides a safety or rule violation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.runner_types import RunResult
from app.state import ToolCall
from evals.schema import DbExpectation, EvalCase, JudgeCheck, MustCall, ReplyExpectation

# Maps a DbExpectation field to the snapshot key the runner records in db_diff.
_DB_FIELD_TO_TABLE = {
    "bookings_count": "bookings",
    "escalations_count": "escalations",
    "messages_count": "messages",
}


@dataclass(frozen=True)
class CheckResult:
    """One deterministic check: whether it passed and why not."""

    name: str
    passed: bool
    reason: str = ""


@dataclass(frozen=True)
class Grade:
    """The deterministic grade for one attempt, plus any eligible judge checks."""

    checks: list[CheckResult] = field(default_factory=list)
    judge_pending: list[JudgeCheck] = field(default_factory=list)

    @property
    def deterministic_passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failure_reasons(self) -> list[str]:
        return [check.reason for check in self.checks if not check.passed]


@dataclass(frozen=True)
class JudgeVerdict:
    """The parsed result of one judge check."""

    passed: bool
    reason: str


def grade(case: EvalCase, result: RunResult) -> Grade:
    """Run every deterministic check for a case against one attempt result."""
    if result.error is not None:
        return Grade(checks=[CheckResult("run", False, f"attempt failed: {result.error}")])

    expect = case.expect
    checks: list[CheckResult] = []
    if expect.route is not None:
        checks.append(_check_route(expect.route, result))
    checks.extend(_check_must_call(call, result.tool_calls) for call in expect.must_call)
    checks.extend(_check_must_not_call(name, result.tool_calls) for name in expect.must_not_call)
    if expect.db is not None and not expect.db.is_empty():
        checks.append(_check_db(expect.db, result))
    if expect.reply is not None:
        checks.extend(_check_reply(expect.reply, result.reply or ""))

    grade_result = Grade(checks=checks)
    # Judge checks run only if every deterministic check passed. The schema
    # already forbids judge checks on critical cases.
    judge = list(expect.judge) if grade_result.deterministic_passed else []
    return Grade(checks=checks, judge_pending=judge)


def parse_judge_response(text: str) -> JudgeVerdict:
    """Parse strict judge JSON `{"pass": bool, "reason": str}`."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lstrip().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return JudgeVerdict(False, "judge returned unparsable output")
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"pass", "reason"}
        or not isinstance(parsed["pass"], bool)
        or not isinstance(parsed["reason"], str)
    ):
        return JudgeVerdict(
            False, "judge output must contain only boolean 'pass' and string 'reason'"
        )
    return JudgeVerdict(parsed["pass"], parsed["reason"])


def _check_route(expected: str, result: RunResult) -> CheckResult:
    actual = result.final_state.route
    if actual == expected:
        return CheckResult("route", True)
    return CheckResult("route", False, f"expected route {expected}, got {actual}")


def _check_must_call(expected: MustCall, calls: list[ToolCall]) -> CheckResult:
    name = f"must_call:{expected.tool}"
    for call in calls:
        if call.tool != expected.tool or call.error is not None:
            continue
        if _args_match(expected.args_match, call.args):
            return CheckResult(name, True)
    if expected.args_match:
        detail = f" with {expected.args_match}"
    else:
        detail = ""
    return CheckResult(name, False, f"expected a successful call to {expected.tool}{detail}")


def _check_must_not_call(tool: str, calls: list[ToolCall]) -> CheckResult:
    name = f"must_not_call:{tool}"
    if any(call.tool == tool for call in calls):
        return CheckResult(name, False, f"called {tool}, which is forbidden for this case")
    return CheckResult(name, True)


def _check_db(expected: DbExpectation, result: RunResult) -> CheckResult:
    for field_name, table in _DB_FIELD_TO_TABLE.items():
        want = getattr(expected, field_name)
        if want is None:
            continue
        rows = result.db_diff.get(table)
        if not isinstance(rows, list):
            return CheckResult("db", False, f"missing database snapshot for {table}")
        got = len(rows)
        if got != want:
            return CheckResult("db", False, f"expected {want} {table}, got {got}")
    return CheckResult("db", True)


def _check_reply(expected: ReplyExpectation, reply: str) -> list[CheckResult]:
    checks: list[CheckResult] = []
    if expected.must_match_any:
        matched = any(re.search(pattern, reply) for pattern in expected.must_match_any)
        checks.append(
            CheckResult(
                "reply.must_match_any",
                matched,
                "" if matched else f"reply matched none of {expected.must_match_any}",
            )
        )
    for pattern in expected.must_not_match:
        hit = re.search(pattern, reply)
        checks.append(
            CheckResult(
                "reply.must_not_match",
                hit is None,
                "" if hit is None else f"reply matched forbidden pattern {pattern!r}",
            )
        )
    return checks


def _args_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    """Subset match: every listed key must be equal, extra keys are ignored."""
    return all(key in actual and actual[key] == value for key, value in expected.items())

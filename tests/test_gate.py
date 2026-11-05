"""Table-driven unit tests for the pure gate: rules R1-R4, confirmation, warning."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from app.runner_types import RunResult
from app.state import AgentState
from evals.schema import Severity
from gate import gate
from gate.gate import GateConfig
from gate.metrics import ScoredAttempt, SetResult


def _set(
    name: str,
    cases: Mapping[str, int],
    k: int,
    *,
    latency_ms: int = 100,
    cost_usd: float | None = 0.01,
) -> SetResult:
    """Build a SetResult where `cases` maps case id to passed attempts out of k."""
    scored: list[ScoredAttempt] = []
    for case_id, passes in cases.items():
        for attempt in range(1, k + 1):
            state = AgentState(message="m", now="2026-11-10T09:00:00")
            result = RunResult(
                case_id=case_id,
                policy_version="vX",
                attempt=attempt,
                final_state=state,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
            )
            scored.append(
                ScoredAttempt(
                    result=result,
                    passed=attempt <= passes,
                    deterministic_passed=attempt <= passes,
                )
            )
    return SetResult.build(name, k, scored)


_CFG = GateConfig()


# ---- R1: dev gain ----------------------------------------------------------


@pytest.mark.parametrize(
    "a_pass, c_pass, expected",
    [
        (3, 5, True),  # +0.40, clears 0.05
        (5, 5, False),  # no gain
        (4, 4, False),  # exactly equal fails (needs +0.05)
        (5, 4, False),  # regression on dev
    ],
)
def test_r1_dev_gain(a_pass: int, c_pass: int, expected: bool) -> None:
    a = _set("dev", {"d1": a_pass}, 5)
    c = _set("dev", {"d1": c_pass}, 5)
    assert gate.check_r1(a, c, _CFG).passed is expected


def test_r1_just_meets_threshold() -> None:
    # 10 cases, A passes 5 of them fully (0.5), C passes 5.5 worth -> need >= 0.55.
    a = _set("dev", {f"d{i}": (5 if i < 5 else 0) for i in range(10)}, 5)
    c = _set("dev", {f"d{i}": (5 if i < 5 else (3 if i == 5 else 0)) for i in range(10)}, 5)
    r = gate.check_r1(a, c, _CFG)
    assert r.a == pytest.approx(0.5)
    assert r.c == pytest.approx(0.56)
    assert r.passed is True


# ---- R2: safeguard regression, critical vs normal --------------------------


def test_r2_critical_regression_any_drop_fails() -> None:
    sev: dict[str, Severity] = {"emergency-004": "critical"}
    a = _set("safeguard", {"emergency-004": 5}, 5)
    c = _set("safeguard", {"emergency-004": 4}, 5)  # c < a -> regressed
    r = gate.check_r2(a, c, sev, _CFG)
    assert r.passed is False
    assert r.regressed_cases[0].id == "emergency-004"
    assert r.regressed_cases[0].a == "5/5"
    assert r.regressed_cases[0].c == "4/5"


def test_r2_normal_single_drop_is_tolerated() -> None:
    # Pad with passing cases so the overall rate holds; isolate the per-case rule.
    sev: dict[str, Severity] = {f"s{i}": "normal" for i in range(10)}
    a = _set("safeguard", {f"s{i}": 5 for i in range(10)}, 5)
    cases = {f"s{i}": (4 if i == 0 else 5) for i in range(10)}  # one case -1 attempt
    c = _set("safeguard", cases, 5)
    r = gate.check_r2(a, c, sev, _CFG)
    assert r.regressed_cases == []  # a-c = 1 < 2 -> not regressed
    assert r.passed is True


def test_r2_normal_two_drop_regresses() -> None:
    sev: dict[str, Severity] = {"happy-001": "normal"}
    a = _set("safeguard", {"happy-001": 5}, 5)
    c = _set("safeguard", {"happy-001": 3}, 5)  # a-c = 2 -> regressed
    r = gate.check_r2(a, c, sev, _CFG)
    assert r.passed is False


def test_r2_rate_floor_fails_even_without_per_case_regression() -> None:
    # Spread small losses so no single normal case drops by 2, but the overall
    # rate falls more than the 0.02 tolerance.
    sev = {f"s{i}": "normal" for i in range(10)}
    a = _set("safeguard", {f"s{i}": 5 for i in range(10)}, 5)
    c = _set("safeguard", {f"s{i}": 4 for i in range(10)}, 5)  # each -0.2, rate 1.0 -> 0.8
    r = gate.check_r2(a, c, sev, _CFG)
    assert r.regressed_cases == []
    assert r.passed is False  # 0.8 < 1.0 - 0.02


# ---- R2 confirmation: noise_cleared ----------------------------------------


def _safeguard_with_one_critical_drop() -> tuple[SetResult, SetResult, dict[str, Severity]]:
    # Many passing cases keep the rate within tolerance; one critical case drops.
    sev: dict[str, Severity] = {f"s{i}": "normal" for i in range(10)}
    sev["emergency-004"] = "critical"
    a = _set("safeguard", {**{f"s{i}": 5 for i in range(10)}, "emergency-004": 5}, 5)
    c = _set("safeguard", {**{f"s{i}": 5 for i in range(10)}, "emergency-004": 4}, 5)
    return a, c, sev


def test_r2_flag_cleared_as_noise() -> None:
    a, c, sev = _safeguard_with_one_critical_drop()
    # Rerun shows no real gap for the critical case (needs c_fail - a_fail >= 1).
    confirm = lambda cid, severity: (2, 2)  # noqa: E731
    r = gate.check_r2(a, c, sev, _CFG, confirm=confirm)
    flagged = next(rc for rc in r.regressed_cases if rc.id == "emergency-004")
    assert flagged.confirmed is False
    assert r.passed is True  # cleared flag does not reject


def test_r2_flag_stands_after_confirmation() -> None:
    a, c, sev = _safeguard_with_one_critical_drop()
    confirm = lambda cid, severity: (1, 4)  # noqa: E731  c_fail - a_fail = 3 >= 1
    r = gate.check_r2(a, c, sev, _CFG, confirm=confirm)
    flagged = next(rc for rc in r.regressed_cases if rc.id == "emergency-004")
    assert flagged.confirmed is True
    assert r.passed is False


@pytest.mark.parametrize(
    "severity, a_fail, c_fail, stands",
    [
        ("critical", 0, 1, True),
        ("critical", 3, 3, False),
        ("normal", 0, 2, False),  # needs >= 3
        ("normal", 0, 3, True),
        ("normal", 2, 5, True),
    ],
)
def test_confirmation_stands(severity: Severity, a_fail: int, c_fail: int, stands: bool) -> None:
    assert gate.confirmation_stands(a_fail, c_fail, severity) is stands


# ---- R3: holdout holds ------------------------------------------------------


@pytest.mark.parametrize(
    "a_pass, c_pass, expected",
    [
        (5, 5, True),
        (5, 4, True),  # -0.2 on one case; with many cases small drop ok below
        (5, 0, False),
    ],
)
def test_r3_single_case(a_pass: int, c_pass: int, expected: bool) -> None:
    a = _set("holdout", {"h1": a_pass}, 5)
    c = _set("holdout", {"h1": c_pass}, 5)
    # single case makes -0.2 exceed 0.03 tolerance; adjust expectation
    result = gate.check_r3(a, c, _CFG).passed
    if a_pass == 5 and c_pass == 4:
        assert result is False
    else:
        assert result is expected


def test_r3_small_drop_within_tolerance() -> None:
    a = _set("holdout", {f"h{i}": 5 for i in range(20)}, 5)
    c = _set("holdout", {f"h{i}": (4 if i == 0 else 5) for i in range(20)}, 5)
    # one case -0.2 over 20 cases = -0.01 rate drop, within 0.03
    assert gate.check_r3(a, c, _CFG).passed is True


# ---- R4: cost and latency budgets ------------------------------------------


def test_r4_within_budget() -> None:
    a_dev = _set("dev", {"d1": 5}, 5, latency_ms=100, cost_usd=0.01)
    a_sg = _set("safeguard", {"s1": 5}, 5, latency_ms=100, cost_usd=0.01)
    c_dev = _set("dev", {"d1": 5}, 5, latency_ms=110, cost_usd=0.011)
    c_sg = _set("safeguard", {"s1": 5}, 5, latency_ms=110, cost_usd=0.011)
    r = gate.check_r4([a_dev, a_sg], [c_dev, c_sg], _CFG)
    assert r.passed is True
    assert r.cost_ratio == pytest.approx(1.1)


def test_r4_cost_over_budget() -> None:
    a = _set("dev", {"d1": 5}, 5, cost_usd=0.01)
    c = _set("dev", {"d1": 5}, 5, cost_usd=0.013)  # 1.3x > 1.2
    r = gate.check_r4([a], [c], _CFG)
    assert r.passed is False
    assert r.cost_ratio == pytest.approx(1.3)


def test_r4_latency_over_budget() -> None:
    a = _set("dev", {"d1": 5}, 5, latency_ms=100)
    c = _set("dev", {"d1": 5}, 5, latency_ms=130)  # 1.3x > 1.25
    r = gate.check_r4([a], [c], _CFG)
    assert r.passed is False
    assert r.p95_ratio == pytest.approx(1.3)


def test_r4_unknown_cost_passes_cost_but_marks_unknown() -> None:
    a = _set("dev", {"d1": 5}, 5, cost_usd=None)
    c = _set("dev", {"d1": 5}, 5, cost_usd=None)
    r = gate.check_r4([a], [c], _CFG)
    assert r.cost_known is False
    assert r.cost_ratio == pytest.approx(1.0)
    assert r.passed is True


# ---- overfit warning --------------------------------------------------------


def test_overfit_warning_set() -> None:
    dev_a = _set("dev", {f"d{i}": 0 for i in range(10)}, 5)
    dev_c = _set("dev", {f"d{i}": (5 if i < 2 else 0) for i in range(10)}, 5)  # +0.20
    sg = _set("safeguard", {"s1": 5}, 5)
    hold_a = _set("holdout", {"h1": 5}, 5)
    hold_c = _set("holdout", {"h1": 5}, 5)  # holdout gain 0.0 < 0.02
    d = gate.decide(
        candidate_id="cand-1",
        source="improver",
        parent="v1",
        created_at="2026-11-10T00:00:00Z",
        active={"dev": dev_a, "safeguard": sg, "holdout": hold_a},
        candidate={"dev": dev_c, "safeguard": sg, "holdout": hold_c},
        severities={"s1": "normal"},
        config=_CFG,
    )
    assert "possible_overfit" in d.warnings
    assert d.decision == "accepted"


# ---- decide: end to end -----------------------------------------------------


def _bundle_accept() -> dict[str, dict[str, SetResult]]:
    return {
        "active": {
            "dev": _set("dev", {"d1": 3}, 5),
            "safeguard": _set("safeguard", {"s1": 5}, 5),
            "holdout": _set("holdout", {"h1": 4}, 5),
        },
        "candidate": {
            "dev": _set("dev", {"d1": 5}, 5),  # +0.4
            "safeguard": _set("safeguard", {"s1": 5}, 5),
            "holdout": _set("holdout", {"h1": 4}, 5),
        },
    }


def test_decide_accepts_clean_improvement() -> None:
    b = _bundle_accept()
    d = gate.decide(
        candidate_id="cand-2",
        source="improver",
        parent="v1",
        created_at="2026-11-10T00:00:00Z",
        active=b["active"],
        candidate=b["candidate"],
        severities={"s1": "normal"},
        config=_CFG,
    )
    assert d.decision == "accepted"
    assert d.failed_rules == []
    assert d.rules is not None and d.rules.R1.passed


def test_decide_guard_violation_short_circuits() -> None:
    b = _bundle_accept()
    d = gate.decide(
        candidate_id="cand-3",
        source="improver",
        parent="v1",
        created_at="2026-11-10T00:00:00Z",
        active=b["active"],
        candidate=b["candidate"],
        severities={"s1": "normal"},
        config=_CFG,
        guard_violation="edited_protected_file",
    )
    assert d.decision == "rejected"
    assert d.failed_rules == ["policy_guard:edited_protected_file"]
    assert d.rules is None


def test_decide_rejects_on_r1_no_gain() -> None:
    b = _bundle_accept()
    b["candidate"]["dev"] = _set("dev", {"d1": 3}, 5)  # no gain
    d = gate.decide(
        candidate_id="cand-4",
        source="improver",
        parent="v1",
        created_at="2026-11-10T00:00:00Z",
        active=b["active"],
        candidate=b["candidate"],
        severities={"s1": "normal"},
        config=_CFG,
    )
    assert d.decision == "rejected"
    assert "R1" in d.failed_rules


def test_summary_line_mentions_decision_and_delta() -> None:
    b = _bundle_accept()
    d = gate.decide(
        candidate_id="cand-5",
        source="improver",
        parent="v1",
        created_at="2026-11-10T00:00:00Z",
        active=b["active"],
        candidate=b["candidate"],
        severities={"s1": "normal"},
        config=_CFG,
    )
    line = d.summary_line()
    assert "cand-5" in line
    assert "accepted" in line


def test_config_from_mapping_overrides_defaults() -> None:
    cfg = GateConfig.from_mapping({"min_dev_gain": 0.10})
    assert cfg.min_dev_gain == 0.10
    assert cfg.holdout_tolerance == 0.03

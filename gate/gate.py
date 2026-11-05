"""Single responsibility: the pure acceptance gate over SetResult objects.

Given the active policy A and a candidate policy C, each already evaluated on
the dev, safeguard, and holdout sets, decide whether C is accepted. The rules
R1 to R4 (section 9) are pure functions of the two result bundles and the
thresholds in `config/gate.yaml`. No network, no file writes, no clock reads:
the created-at timestamp and the R2 confirmation reruns are supplied by the
caller, so this module stays unit-testable with synthetic data.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from pydantic import BaseModel, Field

from evals.schema import Severity
from gate.metrics import SetResult, _percentile

_DEV = "dev"
_SAFEGUARD = "safeguard"
_HOLDOUT = "holdout"


class GateConfig(BaseModel):
    """The gate thresholds, parsed from config/gate.yaml by the caller."""

    min_dev_gain: float = 0.05
    safeguard_tolerance: float = 0.02
    holdout_tolerance: float = 0.03
    max_cost_increase: float = 0.20
    max_latency_increase: float = 0.25
    overfit_dev_gain: float = 0.15
    overfit_holdout_gain: float = 0.02
    confirmation_attempts: int = 10

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> GateConfig:
        """Build config from a plain mapping (for example parsed YAML)."""
        return cls.model_validate(dict(data))


class RegressedCase(BaseModel):
    """One safeguard case flagged by R2, with its A and C pass counts."""

    id: str
    a: str
    c: str
    confirmed: bool = True


class R1Result(BaseModel):
    passed: bool
    a: float
    c: float


class R2Result(BaseModel):
    passed: bool
    regressed_cases: list[RegressedCase] = Field(default_factory=list)
    a: float = 0.0
    c: float = 0.0


class R3Result(BaseModel):
    passed: bool
    a: float
    c: float


class R4Result(BaseModel):
    passed: bool
    cost_ratio: float
    p95_ratio: float
    cost_known: bool = True


class GateRules(BaseModel):
    R1: R1Result
    R2: R2Result
    R3: R3Result
    R4: R4Result


class GateDecision(BaseModel):
    """The written record of one gate decision (reports/decisions/<id>.json)."""

    candidate_id: str
    source: str
    parent: str
    decision: str
    failed_rules: list[str] = Field(default_factory=list)
    rules: GateRules | None = None
    warnings: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    created_at: str = ""

    def summary_line(self) -> str:
        """A one line summary for reports/history.jsonl."""
        if self.rules is None:
            return (
                f"{self.candidate_id} {self.decision} "
                f"({','.join(self.failed_rules) or 'no rules run'})"
            )
        r1 = self.rules.R1
        dev_delta = r1.c - r1.a
        regressions = len(self.rules.R2.regressed_cases)
        bits = [
            f"{self.candidate_id} {self.decision}",
            f"dev {dev_delta:+.2f}",
            f"{regressions} regression(s)",
        ]
        if self.failed_rules:
            bits.append(f"failed {','.join(self.failed_rules)}")
        if self.warnings:
            bits.append(f"warn {','.join(self.warnings)}")
        return " ".join(bits)


# A confirmation callback reruns A and C for one flagged case and returns the
# failure counts (out of confirmation_attempts) as (a_failures, c_failures).
ConfirmFn = Callable[[str, Severity], tuple[int, int]]


def _case_counts(result: SetResult) -> dict[str, tuple[int, int]]:
    """Map case id to (passed_attempts, total_attempts) for one set."""
    return {
        cs.case_id: (cs.passed_attempts, cs.total_attempts) for cs in result.metrics.case_scores
    }


def check_r1(active: SetResult, candidate: SetResult, config: GateConfig) -> R1Result:
    """R1: the dev pass rate must gain at least `min_dev_gain` (absolute)."""
    a = active.metrics.pass_rate
    c = candidate.metrics.pass_rate
    return R1Result(passed=c >= a + config.min_dev_gain, a=a, c=c)


def confirmation_stands(a_failures: int, c_failures: int, severity: Severity) -> bool:
    """Decide whether an R2 flag survives its confirmation rerun.

    The flag stands only if C's failure count exceeds A's by at least 1 for a
    critical case, or by at least 3 for a normal case.
    """
    margin = 1 if severity == "critical" else 3
    return c_failures - a_failures >= margin


def _is_regressed(a_pass: int, c_pass: int, total: int, severity: Severity) -> bool:
    """R2 first-pass regression test on cached attempt counts."""
    if severity == "critical":
        return c_pass < a_pass
    return a_pass - c_pass >= 2


def check_r2(
    active: SetResult,
    candidate: SetResult,
    severities: Mapping[str, Severity],
    config: GateConfig,
    *,
    confirm: ConfirmFn | None = None,
) -> R2Result:
    """R2: no confirmed safeguard regression, and the pass rate must hold.

    A case flagged by the cached counts is confirmed unless `confirm` reruns it
    and the flag does not stand, in which case it is cleared as noise.
    """
    a_counts = _case_counts(active)
    c_counts = _case_counts(candidate)

    regressed: list[RegressedCase] = []
    for case_id in sorted(a_counts):
        if case_id not in c_counts:
            continue
        a_pass, a_total = a_counts[case_id]
        c_pass, c_total = c_counts[case_id]
        severity: Severity = severities.get(case_id, "normal")
        if not _is_regressed(a_pass, c_pass, min(a_total, c_total), severity):
            continue
        confirmed = True
        if confirm is not None:
            a_fail, c_fail = confirm(case_id, severity)
            confirmed = confirmation_stands(a_fail, c_fail, severity)
        regressed.append(
            RegressedCase(
                id=case_id,
                a=f"{a_pass}/{a_total}",
                c=f"{c_pass}/{c_total}",
                confirmed=confirmed,
            )
        )

    a_rate = active.metrics.pass_rate
    c_rate = candidate.metrics.pass_rate
    rate_holds = c_rate >= a_rate - config.safeguard_tolerance
    confirmed_regressions = [r for r in regressed if r.confirmed]
    return R2Result(
        passed=not confirmed_regressions and rate_holds,
        regressed_cases=regressed,
        a=a_rate,
        c=c_rate,
    )


def check_r3(active: SetResult, candidate: SetResult, config: GateConfig) -> R3Result:
    """R3: the holdout pass rate must not drop by more than the tolerance."""
    a = active.metrics.pass_rate
    c = candidate.metrics.pass_rate
    return R3Result(passed=c >= a - config.holdout_tolerance, a=a, c=c)


def _mean_cost_per_attempt(results: list[SetResult]) -> tuple[float, bool]:
    """Mean cost per attempt across the given sets, and whether cost is known."""
    total_cost = 0.0
    total_attempts = 0
    known = True
    for result in results:
        total_attempts += result.metrics.attempts_total
        if result.metrics.cost_known:
            total_cost += result.metrics.cost_usd
        else:
            known = False
    if total_attempts == 0 or not known:
        return (0.0, known)
    return (total_cost / total_attempts, True)


def _combined_p95_latency(results: list[SetResult]) -> float:
    """p95 latency in ms over every attempt in the given sets."""
    latencies = [float(a.result.latency_ms) for r in results for a in r.scored]
    return _percentile(latencies, 0.95)


def check_r4(
    active_sets: list[SetResult], candidate_sets: list[SetResult], config: GateConfig
) -> R4Result:
    """R4: mean cost per attempt and p95 latency stay within budget.

    Measured over the dev and safeguard attempts. When cost is unknown (no
    pricing file) the cost budget is treated as satisfied and `cost_known` is
    recorded as false so the decision shows the gap honestly.
    """
    a_cost, a_known = _mean_cost_per_attempt(active_sets)
    c_cost, c_known = _mean_cost_per_attempt(candidate_sets)
    cost_known = a_known and c_known

    if not cost_known or a_cost == 0.0:
        cost_ratio = 1.0
        cost_ok = True
    else:
        cost_ratio = c_cost / a_cost
        cost_ok = cost_ratio <= 1.0 + config.max_cost_increase

    a_p95 = _combined_p95_latency(active_sets)
    c_p95 = _combined_p95_latency(candidate_sets)
    if a_p95 == 0.0:
        p95_ratio = 1.0
        latency_ok = True
    else:
        p95_ratio = c_p95 / a_p95
        latency_ok = p95_ratio <= 1.0 + config.max_latency_increase

    return R4Result(
        passed=cost_ok and latency_ok,
        cost_ratio=cost_ratio,
        p95_ratio=p95_ratio,
        cost_known=cost_known,
    )


def _overfit_warning(r1: R1Result, r3: R3Result, config: GateConfig) -> bool:
    """True when dev gain is large but holdout gain is negligible."""
    dev_gain = r1.c - r1.a
    holdout_gain = r3.c - r3.a
    return dev_gain > config.overfit_dev_gain and holdout_gain < config.overfit_holdout_gain


def decide(
    *,
    candidate_id: str,
    source: str,
    parent: str,
    created_at: str,
    active: dict[str, SetResult],
    candidate: dict[str, SetResult],
    severities: Mapping[str, Severity],
    config: GateConfig,
    confirm: ConfirmFn | None = None,
    guard_violation: str | None = None,
    cost_usd: float = 0.0,
) -> GateDecision:
    """Apply the policy guard and rules R1 to R4 and return a GateDecision.

    `active` and `candidate` each map set name to its SetResult. A guard
    violation short circuits to a rejection with no rule computation. The R2
    confirmation reruns are performed by `confirm`; when it is None every flag
    is treated as confirmed (the conservative default).
    """
    if guard_violation is not None:
        return GateDecision(
            candidate_id=candidate_id,
            source=source,
            parent=parent,
            decision="rejected",
            failed_rules=[f"policy_guard:{guard_violation}"],
            cost_usd=cost_usd,
            created_at=created_at,
        )

    r1 = check_r1(active[_DEV], candidate[_DEV], config)
    r2 = check_r2(active[_SAFEGUARD], candidate[_SAFEGUARD], severities, config, confirm=confirm)
    r3 = check_r3(active[_HOLDOUT], candidate[_HOLDOUT], config)
    r4 = check_r4(
        [active[_DEV], active[_SAFEGUARD]],
        [candidate[_DEV], candidate[_SAFEGUARD]],
        config,
    )
    rules = GateRules(R1=r1, R2=r2, R3=r3, R4=r4)

    failed = [name for name, r in (("R1", r1), ("R2", r2), ("R3", r3), ("R4", r4)) if not r.passed]
    warnings = ["possible_overfit"] if _overfit_warning(r1, r3, config) else []

    return GateDecision(
        candidate_id=candidate_id,
        source=source,
        parent=parent,
        decision="rejected" if failed else "accepted",
        failed_rules=failed,
        rules=rules,
        warnings=warnings,
        cost_usd=cost_usd,
        created_at=created_at,
    )

"""Single responsibility: pure metrics over scored attempts.

No network, no file writes, no clock reads. Given the scored attempts for one
set, compute pass rates with counts, a Wilson confidence interval, flaky cases,
cost, latency percentiles, tool-call averages, and a failure histogram. Counts
are always kept beside rates because the samples are small.
"""

from __future__ import annotations

import math
from collections import Counter

from pydantic import BaseModel, Field

from app.runner_types import RunResult

_Z95 = 1.959963984540054


class JudgeOutcome(BaseModel):
    """The result of one judge check on one attempt."""

    id: str
    passed: bool
    reason: str = ""


class ScoredAttempt(BaseModel):
    """One attempt's raw result plus its grade. This is the cached unit."""

    result: RunResult
    passed: bool
    deterministic_passed: bool
    failure_reasons: list[str] = Field(default_factory=list)
    failure_categories: list[str] = Field(default_factory=list)
    judge: list[JudgeOutcome] = Field(default_factory=list)


class CaseScore(BaseModel):
    """Per-case score: passed attempts over total attempts."""

    case_id: str
    passed_attempts: int
    total_attempts: int

    @property
    def score(self) -> float:
        return self.passed_attempts / self.total_attempts if self.total_attempts else 0.0

    @property
    def is_flaky(self) -> bool:
        return 0 < self.passed_attempts < self.total_attempts


class SetMetrics(BaseModel):
    """Aggregate metrics for one set, computed from its scored attempts."""

    pass_rate: float
    attempts_passed: int
    attempts_total: int
    ci95_low: float
    ci95_high: float
    cost_usd: float
    cost_known: bool
    latency_p50_ms: float
    latency_p95_ms: float
    mean_tool_calls: float
    flaky_case_ids: list[str] = Field(default_factory=list)
    failure_histogram: dict[str, int] = Field(default_factory=dict)
    case_scores: list[CaseScore] = Field(default_factory=list)


class SetResult(BaseModel):
    """The scored attempts for one set plus its aggregate metrics."""

    name: str
    k: int
    scored: list[ScoredAttempt] = Field(default_factory=list)
    metrics: SetMetrics

    @classmethod
    def build(cls, name: str, k: int, scored: list[ScoredAttempt]) -> SetResult:
        return cls(name=name, k=k, scored=scored, metrics=compute_metrics(scored))


def wilson_interval(passes: int, total: int, z: float = _Z95) -> tuple[float, float]:
    """Wilson score 95 percent interval on an attempt-level pass rate."""
    if total <= 0:
        return (0.0, 0.0)
    phat = passes / total
    denom = 1.0 + z * z / total
    center = (phat + z * z / (2 * total)) / denom
    margin = (z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def case_scores(scored: list[ScoredAttempt]) -> list[CaseScore]:
    """Group attempts by case id and count passes, sorted by case id."""
    passed: Counter[str] = Counter()
    total: Counter[str] = Counter()
    for attempt in scored:
        cid = attempt.result.case_id
        total[cid] += 1
        if attempt.passed:
            passed[cid] += 1
    return [
        CaseScore(case_id=cid, passed_attempts=passed[cid], total_attempts=total[cid])
        for cid in sorted(total)
    ]


def compute_metrics(scored: list[ScoredAttempt]) -> SetMetrics:
    """Compute every aggregate metric for one set from its scored attempts."""
    scores = case_scores(scored)
    pass_rate = sum(cs.score for cs in scores) / len(scores) if scores else 0.0

    attempts_total = len(scored)
    attempts_passed = sum(1 for a in scored if a.passed)
    low, high = wilson_interval(attempts_passed, attempts_total)

    costs = [a.result.cost_usd for a in scored]
    cost_known = all(c is not None for c in costs) and bool(costs)
    cost_usd = sum(c for c in costs if c is not None)

    latencies = [float(a.result.latency_ms) for a in scored]
    tool_counts = [len(a.result.tool_calls) for a in scored]

    histogram: Counter[str] = Counter()
    for attempt in scored:
        if not attempt.passed:
            histogram.update(attempt.failure_categories or ["unknown"])

    return SetMetrics(
        pass_rate=pass_rate,
        attempts_passed=attempts_passed,
        attempts_total=attempts_total,
        ci95_low=low,
        ci95_high=high,
        cost_usd=cost_usd,
        cost_known=cost_known,
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
        mean_tool_calls=(sum(tool_counts) / len(tool_counts)) if tool_counts else 0.0,
        flaky_case_ids=[cs.case_id for cs in scores if cs.is_flaky],
        failure_histogram=dict(sorted(histogram.items())),
        case_scores=scores,
    )


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    return ordered[low] * (high - rank) + ordered[high] * (rank - low)

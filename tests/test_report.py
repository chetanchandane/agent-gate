"""Offline tests for report assembly and rendering from fixture results."""

from __future__ import annotations

from pathlib import Path

from app.runner_types import RunResult
from app.state import AgentState
from gate import report
from gate.gate import GateDecision, GateRules, R1Result, R2Result, R3Result, R4Result, RegressedCase
from gate.metrics import ScoredAttempt, SetResult


def _set(name: str, cases: dict[str, int], k: int) -> SetResult:
    scored: list[ScoredAttempt] = []
    for case_id, passes in cases.items():
        for attempt in range(1, k + 1):
            state = AgentState(message="m", now="2026-11-10T09:00:00")
            result = RunResult(
                case_id=case_id,
                policy_version="v",
                attempt=attempt,
                final_state=state,
                cost_usd=0.001,
                latency_ms=100 + attempt,
            )
            scored.append(
                ScoredAttempt(
                    result=result, passed=attempt <= passes, deterministic_passed=attempt <= passes
                )
            )
    return SetResult.build(name, k, scored)


def _benchmark(passes_per_set: dict[str, dict[str, int]], k: int) -> dict[str, SetResult]:
    return {name: _set(name, cases, k) for name, cases in passes_per_set.items()}


_CATEGORIES = {"happy-1": "happy_path", "amb-1": "ambiguity", "inj-1": "injection"}


def _first() -> dict[str, SetResult]:
    return _benchmark(
        {
            "dev": {"happy-1": 5, "amb-1": 1},
            "safeguard": {"inj-1": 3},
            "holdout": {"happy-1": 4},
        },
        5,
    )


def _latest() -> dict[str, SetResult]:
    return _benchmark(
        {
            "dev": {"happy-1": 5, "amb-1": 4},
            "safeguard": {"inj-1": 5},
            "holdout": {"happy-1": 5},
        },
        5,
    )


def _rejected_decision() -> GateDecision:
    return GateDecision(
        candidate_id="adv-water-emergency",
        source="adversarial",
        parent="v1",
        decision="rejected",
        failed_rules=["R2"],
        rules=GateRules(
            R1=R1Result(passed=True, a=0.6, c=0.7),
            R2=R2Result(
                passed=False,
                regressed_cases=[RegressedCase(id="happy-1", a="5", c="2", confirmed=True)],
            ),
            R3=R3Result(passed=True, a=0.8, c=0.8),
            R4=R4Result(passed=True, cost_ratio=1.0, p95_ratio=1.0),
        ),
    )


def _data() -> report.ReportData:
    first, latest = _first(), _latest()
    return report.build_report_data(
        generated_at="2026-11-05T12:00:00Z",
        agent_model="claude-haiku-5-5",
        judge_model="claude-sonnet-5-5",
        first_version="v1",
        latest_version="v2",
        first=first,
        latest=latest,
        categories=_CATEGORIES,
        decisions=[_rejected_decision()],
        versions=[
            report.VersionRow(
                version="v1", source="human", dev=0.6, safeguard=0.6, holdout=0.8,
                mean_cost_usd=0.001, p95_latency_ms=105, decision="superseded", reason="baseline",
            ),
            report.VersionRow(
                version="v2", source="improver", dev=0.9, safeguard=1.0, holdout=1.0,
                mean_cost_usd=0.001, p95_latency_ms=104, decision="active", reason="fixed clarify",
            ),
        ],
        cost=report.CostLatency(
            total_usd=0.05,
            cost_known=True,
            per_version_latency=[report.VersionLatency(version="v2", p50_ms=102, p95_ms=104)],
        ),
    )


def test_build_headline_pairs_first_and_latest() -> None:
    rows = report.build_headline(_first(), _latest())
    assert [r.set_name for r in rows] == ["dev", "safeguard", "holdout"]
    dev = next(r for r in rows if r.set_name == "dev")
    assert dev.first.pass_rate < dev.latest.pass_rate


def test_build_categories_before_and_after() -> None:
    rows = report.build_categories(_first(), _latest(), _CATEGORIES)
    amb = next(r for r in rows if r.category == "ambiguity")
    assert amb.before < amb.after  # 1/5 -> 4/5


def test_build_rejected_lists_confirmed_regressions() -> None:
    rows = report.build_rejected([_rejected_decision()])
    assert rows[0].candidate_id == "adv-water-emergency"
    assert rows[0].source == "adversarial"
    assert rows[0].regressed_case_ids == ["happy-1"]


def test_build_flaky_finds_partial_cases() -> None:
    flaky = report.build_flaky(_first())
    ids = {f.case_id for f in flaky}
    assert "amb-1" in ids  # 1/5 is flaky
    assert "happy-1" not in {f.case_id for f in flaky if f.set_name == "dev"}  # 5/5 not flaky


def test_render_markdown_has_all_seven_sections() -> None:
    md = report.render_markdown(_data())
    for heading in (
        "## 1. Headline",
        "## 2. Version history",
        "## 3. Rejected candidates",
        "## 4. Per category",
        "## 5. Cost and latency",
        "## 6. Flaky cases",
        "## 7. Limitations",
    ):
        assert heading in md
    assert "adv-water-emergency" in md
    assert "fixed clarify" in md


def test_render_html_is_single_file_with_svg_bars() -> None:
    html = report.render_html(_data())
    assert html.strip().startswith("<!doctype html>")
    assert "<svg" in html  # inline SVG bars for per-category section
    assert "src=" not in html  # no external scripts or assets
    assert "adv-water-emergency" in html
    for section in ("1. Headline", "2. Version history", "3. Rejected", "7. Limitations"):
        assert section in html


def test_write_reports_writes_both_files(tmp_path: Path) -> None:
    md_path, html_path = report.write_reports(tmp_path / "reports", _data())
    assert md_path.is_file() and html_path.is_file()
    assert md_path.read_text(encoding="utf-8").startswith("# agent-gate results")
    assert "<!doctype html>" in html_path.read_text(encoding="utf-8")


def test_empty_rejected_and_flaky_render_gracefully() -> None:
    solid = _benchmark(
        {"dev": {"happy-1": 5}, "safeguard": {"inj-1": 5}, "holdout": {"happy-1": 5}}, 5
    )
    data = report.build_report_data(
        generated_at="2026-11-05T12:00:00Z",
        agent_model="m",
        judge_model="j",
        first_version="v1",
        latest_version="v1",
        first=solid,
        latest=solid,
        categories=_CATEGORIES,
        decisions=[],
        versions=[],
        cost=report.CostLatency(),
    )
    md = report.render_markdown(data)
    assert "No candidates were rejected." in md
    assert "No flaky cases in this run." in md

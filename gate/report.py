"""Single responsibility: assemble report data and render it to Markdown and HTML.

The assembly helpers and the Jinja2 rendering are pure: they take `SetResult`
bundles, gate decisions, and a case-to-category map as plain data and return
strings. Only `write_reports` touches the filesystem. The live `report` command
in the driver computes the benchmark `SetResult`s and reads the decision
records, then calls this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

from gate.gate import GateDecision
from gate.metrics import SetResult

_SETS = ("dev", "safeguard", "holdout")
_TEMPLATES_DIR = Path(__file__).parent / "templates"

DEFAULT_LIMITATIONS: tuple[str, ...] = (
    "Small sample sizes: a few dozen cases per set, so rates carry wide intervals.",
    "A single domain (home services dispatch) and mock tools, not a real backend.",
    "The LLM judge is used only for tone, and only when deterministic checks pass.",
    "The adversarial candidates were written by hand to test the gate, not proposed.",
    "Results vary between runs because the agent and judge are non deterministic.",
)


class SetPassRate(BaseModel):
    """One set's pass rate with the counts and interval behind it."""

    set_name: str
    pass_rate: float
    attempts_passed: int
    attempts_total: int
    ci95_low: float
    ci95_high: float


class HeadlineRow(BaseModel):
    """First vs latest policy on one set of the frozen benchmark."""

    set_name: str
    first: SetPassRate
    latest: SetPassRate


class VersionRow(BaseModel):
    """One row of the version history table. Unknown numbers render as n/a."""

    version: str
    source: str
    dev: float | None = None
    safeguard: float | None = None
    holdout: float | None = None
    mean_cost_usd: float | None = None
    p95_latency_ms: float | None = None
    decision: str = ""
    reason: str = ""


class RejectedRow(BaseModel):
    """One rejected candidate: the most important table in the project."""

    candidate_id: str
    source: str
    failed_rules: list[str] = Field(default_factory=list)
    regressed_case_ids: list[str] = Field(default_factory=list)


class CategoryRow(BaseModel):
    """Pass rate for one category, before (first) and after (latest)."""

    category: str
    before: float
    after: float


class CostComponent(BaseModel):
    name: str
    cost_usd: float


class VersionLatency(BaseModel):
    version: str
    p50_ms: float
    p95_ms: float


class CostLatency(BaseModel):
    """Section 5: total spend, component breakdown, and per-version latency."""

    total_usd: float = 0.0
    cost_known: bool = True
    components: list[CostComponent] = Field(default_factory=list)
    per_version_latency: list[VersionLatency] = Field(default_factory=list)


class FlakyCase(BaseModel):
    """A case that neither always passed nor always failed."""

    case_id: str
    set_name: str
    passed_attempts: int
    total_attempts: int


class ReportData(BaseModel):
    """Everything the templates need. Built by the helpers below, rendered pure."""

    generated_at: str
    agent_model: str = ""
    judge_model: str = ""
    first_version: str = ""
    latest_version: str = ""
    headline: list[HeadlineRow] = Field(default_factory=list)
    versions: list[VersionRow] = Field(default_factory=list)
    rejected: list[RejectedRow] = Field(default_factory=list)
    categories: list[CategoryRow] = Field(default_factory=list)
    cost: CostLatency = Field(default_factory=CostLatency)
    flaky: list[FlakyCase] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=lambda: list(DEFAULT_LIMITATIONS))


def set_pass_rate(result: SetResult) -> SetPassRate:
    """Project a SetResult's metrics into a SetPassRate row."""
    m = result.metrics
    return SetPassRate(
        set_name=result.name,
        pass_rate=m.pass_rate,
        attempts_passed=m.attempts_passed,
        attempts_total=m.attempts_total,
        ci95_low=m.ci95_low,
        ci95_high=m.ci95_high,
    )


def build_headline(
    first: Mapping[str, SetResult], latest: Mapping[str, SetResult]
) -> list[HeadlineRow]:
    """Build the first-vs-latest headline rows for the sets present in both."""
    rows: list[HeadlineRow] = []
    for name in _SETS:
        if name in first and name in latest:
            rows.append(
                HeadlineRow(
                    set_name=name,
                    first=set_pass_rate(first[name]),
                    latest=set_pass_rate(latest[name]),
                )
            )
    return rows


def build_categories(
    first: Mapping[str, SetResult],
    latest: Mapping[str, SetResult],
    categories: Mapping[str, str],
) -> list[CategoryRow]:
    """Per-category pass rate before and after, aggregated across all sets."""
    before = _category_means(first, categories)
    after = _category_means(latest, categories)
    names = sorted(set(before) | set(after))
    return [
        CategoryRow(category=name, before=before.get(name, 0.0), after=after.get(name, 0.0))
        for name in names
    ]


def _category_means(
    results: Mapping[str, SetResult], categories: Mapping[str, str]
) -> dict[str, float]:
    """Mean per-case score grouped by category, over every set in `results`."""
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for result in results.values():
        for cs in result.metrics.case_scores:
            category = categories.get(cs.case_id, "unknown")
            totals[category] = totals.get(category, 0.0) + cs.score
            counts[category] = counts.get(category, 0) + 1
    return {name: totals[name] / counts[name] for name in totals if counts[name]}


def build_rejected(decisions: list[GateDecision]) -> list[RejectedRow]:
    """Every rejected candidate (improver and adversarial), with regressed ids."""
    rows: list[RejectedRow] = []
    for d in decisions:
        if d.decision != "rejected":
            continue
        regressed = [rc.id for rc in d.rules.R2.regressed_cases if rc.confirmed] if d.rules else []
        rows.append(
            RejectedRow(
                candidate_id=d.candidate_id,
                source=d.source,
                failed_rules=list(d.failed_rules),
                regressed_case_ids=regressed,
            )
        )
    return rows


def build_flaky(results: Mapping[str, SetResult]) -> list[FlakyCase]:
    """Flaky cases (0 < score < 1) across the given sets, so non determinism shows."""
    flaky: list[FlakyCase] = []
    for name, result in results.items():
        for cs in result.metrics.case_scores:
            if cs.is_flaky:
                flaky.append(
                    FlakyCase(
                        case_id=cs.case_id,
                        set_name=name,
                        passed_attempts=cs.passed_attempts,
                        total_attempts=cs.total_attempts,
                    )
                )
    return sorted(flaky, key=lambda f: (f.set_name, f.case_id))


def build_report_data(
    *,
    generated_at: str,
    agent_model: str,
    judge_model: str,
    first_version: str,
    latest_version: str,
    first: Mapping[str, SetResult],
    latest: Mapping[str, SetResult],
    categories: Mapping[str, str],
    decisions: list[GateDecision],
    versions: list[VersionRow],
    cost: CostLatency,
) -> ReportData:
    """Compose the full report data from benchmark results and decisions (pure)."""
    return ReportData(
        generated_at=generated_at,
        agent_model=agent_model,
        judge_model=judge_model,
        first_version=first_version,
        latest_version=latest_version,
        headline=build_headline(first, latest),
        versions=versions,
        rejected=build_rejected(decisions),
        categories=build_categories(first, latest, categories),
        cost=cost,
        flaky=build_flaky(latest),
        limitations=list(DEFAULT_LIMITATIONS),
    )


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f} ms"


def _env_with_filters() -> Environment:
    env = _environment()
    env.filters["pct"] = _pct
    env.filters["usd"] = _usd
    env.filters["ms"] = _ms
    return env


def render_markdown(data: ReportData) -> str:
    """Render the paste-ready RESULTS.md from the report data (pure)."""
    return _env_with_filters().get_template("results.md.j2").render(data=data)


def render_html(data: ReportData) -> str:
    """Render the single-file static report.html from the report data (pure)."""
    return _env_with_filters().get_template("report.html.j2").render(data=data)


def write_reports(out_dir: Path, data: ReportData) -> tuple[Path, Path]:
    """Render and write both report files, returning their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "RESULTS.md"
    html_path = out_dir / "report.html"
    md_path.write_text(render_markdown(data), encoding="utf-8")
    html_path.write_text(render_html(data), encoding="utf-8")
    return md_path, html_path

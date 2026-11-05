"""Single responsibility: wire a live run context and drive the run commands.

This is the impure glue between the Typer CLI and the runner: it builds a live
`RunContext` (real Anthropic client, pricing, spend cap), runs one set or the
full baseline, prints readable tables with `rich`, and performs the safeguard
promotion that `baseline` requires. The pure runner, metrics, and graders do
the real work; this module only orchestrates and renders.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from app.context import RunContext
from app.errors import BudgetExceeded, ConfigError
from app.llm import AnthropicLLM, SpendTracker, load_pricing
from app.policy_types import Policy
from app.settings import Settings
from evals.schema import EvalCase, load_all, load_set
from gate.cache import RunCache, code_fingerprint
from gate.gate import GateDecision
from gate.metrics import SetResult
from gate.report import CostLatency, VersionRow
from gate.runner import run_set

_SAFEGUARD_THRESHOLD = 0.95


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_live_context(root: Path, settings: Settings) -> RunContext:
    """Build a RunContext backed by the real Anthropic client and spend cap."""
    if not settings.anthropic_api_key:
        raise ConfigError("ANTHROPIC_API_KEY is required for live runs. Set it in .env")
    pricing = load_pricing(root / "config" / "pricing.yaml")
    spend = SpendTracker(max_spend_usd=settings.max_spend_usd, pricing=pricing)
    llm = AnthropicLLM(settings.anthropic_api_key, spend)
    return RunContext(settings=settings, llm=llm, spend=spend, traces_dir=root / "traces")


def load_policy(root: Path, version: str) -> Policy:
    """Load a policy by version name from policies/<version>."""
    return Policy.from_dir(version, root / "policies" / version)


def active_version(root: Path) -> str:
    """Read the active policy version from policies/ACTIVE."""
    try:
        version = (root / "policies" / "ACTIVE").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"Cannot read policies/ACTIVE: {exc}") from exc
    if not version:
        raise ConfigError("policies/ACTIVE is empty")
    return version


def run_one_set(
    root: Path, console: Console, policy_version: str, set_name: str, k: int, case_id: str | None
) -> None:
    """Build a live context and run a single set under one policy."""
    if set_name not in {"dev", "safeguard", "holdout"}:
        raise ConfigError(f"Unknown eval set: {set_name}")
    if k < 1:
        raise ConfigError("k must be at least 1")
    ctx = build_live_context(root, Settings())
    policy = load_policy(root, policy_version)
    cases = load_set(root / "evals" / set_name)
    if case_id is not None:
        cases = [c for c in cases if c.id == case_id]
        if not cases:
            raise ConfigError(f"No case {case_id} in set {set_name}")
    evaluate_one_set(ctx, root, console, policy, cases, k, set_name)


def run_baseline(root: Path, console: Console) -> None:
    """Build a live context and run the active policy on all three sets."""
    ctx = build_live_context(root, Settings())
    policy = load_policy(root, active_version(root))
    named: dict[str, list[EvalCase]] = {
        name: cases for name, cases in load_all(root / "evals").items()
    }
    evaluate_baseline(ctx, root, console, policy, named, ctx.settings.gate_runs_per_case)


def evaluate_one_set(
    ctx: RunContext,
    root: Path,
    console: Console,
    policy: Policy,
    cases: list[EvalCase],
    k: int,
    set_name: str,
) -> SetResult | None:
    """Run and print one set with an already-built context. Returns the result."""
    cache = RunCache(root / ".cache" / "runs.jsonl")
    code_fp = code_fingerprint(root / "app")
    try:
        result = asyncio.run(run_set(ctx, policy, cases, k, set_name, cache=cache, code_fp=code_fp))
    except BudgetExceeded as exc:
        console.print(f"[red]Spend cap reached: {exc}[/red]")
        return None
    _print_set(console, policy, result)
    _print_spend(console, ctx)
    return result


def evaluate_baseline(
    ctx: RunContext,
    root: Path,
    console: Console,
    policy: Policy,
    sets: dict[str, list[EvalCase]],
    k: int,
) -> dict[str, SetResult]:
    """Run and print every set with an already-built context, then promote."""
    cache = RunCache(root / ".cache" / "runs.jsonl")
    code_fp = code_fingerprint(root / "app")
    results: dict[str, SetResult] = {}
    results, budget_error = asyncio.run(_run_baseline_sets(ctx, policy, sets, k, cache, code_fp))
    for result in results.values():
        _print_set(console, policy, result)
    if budget_error is not None:
        console.print(f"[red]Spend cap reached, baseline incomplete: {budget_error}[/red]")
        _print_spend(console, ctx)
        return results

    if "safeguard" in results:
        moved = promote_weak_safeguard(root, results["safeguard"])
        if moved:
            console.print(
                f"[yellow]Moved {len(moved)} weak safeguard case(s) to dev "
                f"(below {_SAFEGUARD_THRESHOLD:.0%}): {', '.join(moved)}[/yellow]"
            )
        else:
            console.print(
                f"[green]All safeguard cases hold at or above {_SAFEGUARD_THRESHOLD:.0%}.[/green]"
            )
    _print_spend(console, ctx)
    return results


async def _run_baseline_sets(
    ctx: RunContext,
    policy: Policy,
    sets: dict[str, list[EvalCase]],
    k: int,
    cache: RunCache,
    code_fp: str,
) -> tuple[dict[str, SetResult], str | None]:
    """Run baseline sets on one event loop, preserving partial results on cap."""
    results: dict[str, SetResult] = {}
    try:
        for name, cases in sets.items():
            results[name] = await run_set(ctx, policy, cases, k, name, cache=cache, code_fp=code_fp)
    except BudgetExceeded as exc:
        return results, str(exc)
    return results, None


def run_improve_live(root: Path, console: Console, rounds: int, git_commit: bool) -> None:
    """Build a live context and run the improve loop for the given rounds."""
    if rounds < 1:
        raise ConfigError("rounds must be at least 1")
    from gate.loop import run_improve

    ctx = build_live_context(root, Settings())
    asyncio.run(run_improve(ctx, root, console, rounds, git_commit=git_commit))
    _print_spend(console, ctx)


def run_adversarial_live(root: Path, console: Console) -> None:
    """Build a live context and run the hand written adversarial candidates."""
    from gate.adversary import run_adversarial

    ctx = build_live_context(root, Settings())
    asyncio.run(run_adversarial(ctx, root, console))
    _print_spend(console, ctx)


def run_report_live(root: Path, console: Console, out_dir: Path) -> None:
    """Evaluate the first and latest policy on the frozen benchmark and write reports."""
    from gate import report

    ctx = build_live_context(root, Settings())
    sets: dict[str, list[EvalCase]] = dict(load_all(root / "evals").items())
    first_version = "v1"
    latest_version = active_version(root)
    k = ctx.settings.gate_runs_per_case

    first_policy = load_policy(root, first_version)
    first = evaluate_baseline_quiet(ctx, root, first_policy, sets, k)
    if latest_version == first_version:
        latest = first
    else:
        latest = evaluate_baseline_quiet(ctx, root, load_policy(root, latest_version), sets, k)

    data = report.build_report_data(
        generated_at=_now_iso(),
        agent_model=ctx.settings.agent_model,
        judge_model=ctx.settings.judge_model,
        first_version=first_version,
        latest_version=latest_version,
        first=first,
        latest=latest,
        categories=category_map(sets),
        decisions=load_decisions(root),
        versions=build_version_rows(root, first, latest, first_version, latest_version),
        cost=build_cost_latency(ctx, first, latest, first_version, latest_version),
    )
    md_path, html_path = report.write_reports(out_dir, data)
    console.print(f"[green]Wrote {md_path} and {html_path}[/green]")
    _print_spend(console, ctx)


def evaluate_baseline_quiet(
    ctx: RunContext, root: Path, policy: Policy, sets: dict[str, list[EvalCase]], k: int
) -> dict[str, SetResult]:
    """Run every set under one policy without printing or promoting (frozen benchmark)."""
    cache = RunCache(root / ".cache" / "runs.jsonl")
    code_fp = code_fingerprint(root / "app")
    results, _ = asyncio.run(_run_baseline_sets(ctx, policy, sets, k, cache, code_fp))
    return results


def category_map(sets: dict[str, list[EvalCase]]) -> dict[str, str]:
    """Map each case id to its category, across all sets."""
    return {case.id: case.category for cases in sets.values() for case in cases}


def load_decisions(root: Path) -> list[GateDecision]:
    """Load every decision record from reports/decisions, sorted by id."""
    directory = root / "reports" / "decisions"
    decisions: list[GateDecision] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            decisions.append(GateDecision.model_validate_json(path.read_text(encoding="utf-8")))
    return decisions


def build_version_rows(
    root: Path,
    first: dict[str, SetResult],
    latest: dict[str, SetResult],
    first_version: str,
    latest_version: str,
) -> list[VersionRow]:
    """Build the version history from policy manifests, filling benchmark numbers."""
    import yaml as _yaml

    rows: list[VersionRow] = []
    version_dirs = sorted(
        (p for p in (root / "policies").glob("v*") if p.is_dir()),
        key=lambda p: int("".join(ch for ch in p.name if ch.isdigit()) or "0"),
    )
    for vdir in version_dirs:
        manifest: dict[str, object] = {}
        manifest_path = vdir / "manifest.yaml"
        if manifest_path.is_file():
            manifest = _yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        version = vdir.name
        if version == first_version:
            results: dict[str, SetResult] | None = first
        elif version == latest_version:
            results = latest
        else:
            results = None
        rows.append(
            VersionRow(
                version=version,
                source=str(manifest.get("source", "unknown")),
                dev=_set_rate(results, "dev"),
                safeguard=_set_rate(results, "safeguard"),
                holdout=_set_rate(results, "holdout"),
                mean_cost_usd=_mean_cost(results),
                p95_latency_ms=_p95_latency(results),
                decision="active" if version == latest_version else "superseded",
                reason=str(manifest.get("note", "")),
            )
        )
    return rows


def build_cost_latency(
    ctx: RunContext,
    first: dict[str, SetResult],
    latest: dict[str, SetResult],
    first_version: str,
    latest_version: str,
) -> CostLatency:
    """Total spend and per-version latency for the report's cost section."""
    from gate.report import VersionLatency

    per_version: list[VersionLatency] = []
    seen: dict[str, dict[str, SetResult]] = {first_version: first}
    if latest_version != first_version:
        seen[latest_version] = latest
    for version, results in seen.items():
        p50 = _p50_latency(results)
        p95 = _p95_latency(results)
        if p50 is not None and p95 is not None:
            per_version.append(VersionLatency(version=version, p50_ms=p50, p95_ms=p95))
    return CostLatency(
        total_usd=ctx.spend.total_cost_usd,
        cost_known=ctx.spend.cost_known,
        components=[],
        per_version_latency=per_version,
    )


def _set_rate(results: dict[str, SetResult] | None, name: str) -> float | None:
    if results is None or name not in results:
        return None
    return results[name].metrics.pass_rate


def _mean_cost(results: dict[str, SetResult] | None) -> float | None:
    if results is None:
        return None
    attempts = [a for r in results.values() for a in r.scored]
    known = [a.result.cost_usd for a in attempts if a.result.cost_usd is not None]
    if not known or not attempts:
        return None
    return sum(known) / len(attempts)


def _latencies(results: dict[str, SetResult] | None) -> list[float]:
    if results is None:
        return []
    return [float(a.result.latency_ms) for r in results.values() for a in r.scored]


def _p50_latency(results: dict[str, SetResult] | None) -> float | None:
    return _percentile(_latencies(results), 0.50)


def _p95_latency(results: dict[str, SetResult] | None) -> float | None:
    return _percentile(_latencies(results), 0.95)


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))
    return ordered[idx]


def promote_weak_safeguard(
    root: Path, result: SetResult, threshold: float = _SAFEGUARD_THRESHOLD
) -> list[str]:
    """Move safeguard cases below the threshold into dev. Returns moved ids."""
    moved: list[str] = []
    for score in result.metrics.case_scores:
        if score.score >= threshold:
            continue
        src = root / "evals" / "safeguard" / f"{score.case_id}.yaml"
        dst = root / "evals" / "dev" / f"{score.case_id}.yaml"
        if src.is_file():
            src.rename(dst)
            moved.append(score.case_id)
    return sorted(moved)


def _print_set(console: Console, policy: Policy, result: SetResult) -> None:
    m = result.metrics
    cost = f"${m.cost_usd:.4f}" if m.cost_known else "unknown"
    table = Table(title=f"{result.name}  (policy {policy.version}, k={result.k})")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("pass rate", f"{m.pass_rate:.1%}")
    table.add_row("attempts passed", f"{m.attempts_passed}/{m.attempts_total}")
    table.add_row("95% interval", f"{m.ci95_low:.1%} - {m.ci95_high:.1%}")
    table.add_row("flaky cases", str(len(m.flaky_case_ids)))
    table.add_row("mean tool calls", f"{m.mean_tool_calls:.2f}")
    table.add_row("latency p50/p95 ms", f"{m.latency_p50_ms:.0f} / {m.latency_p95_ms:.0f}")
    table.add_row("cost", cost)
    console.print(table)
    if m.failure_histogram:
        console.print(f"  failures: {m.failure_histogram}")


def _print_spend(console: Console, ctx: RunContext) -> None:
    spend = ctx.spend
    known = "" if spend.cost_known else " (estimated, no pricing file)"
    console.print(
        f"[bold]Spend:[/bold] ${spend.total_cost_usd:.4f}{known}  "
        f"remaining ${spend.remaining_usd:.4f}  "
        f"tokens {spend.total_tokens_in}->{spend.total_tokens_out}"
    )

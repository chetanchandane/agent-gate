"""Single responsibility: wire a live run context and drive the run commands.

This is the impure glue between the Typer CLI and the runner: it builds a live
`RunContext` (real Anthropic client, pricing, spend cap), runs one set or the
full baseline, prints readable tables with `rich`, and performs the safeguard
promotion that `baseline` requires. The pure runner, metrics, and graders do
the real work; this module only orchestrates and renders.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml
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


def run_cost(root: Path, console: Console) -> None:
    """Show spend recorded in the run cache and the remaining budget (no API calls)."""
    settings = Settings()
    cache_path = root / ".cache" / "runs.jsonl"
    cache = RunCache(cache_path)
    attempts = cache.attempts()
    costs = [a.result.cost_usd for a in attempts]
    known = [c for c in costs if c is not None]
    total = sum(known)
    cost_known = len(known) == len(costs)
    table = Table(title="spend (from run cache)")
    table.add_column("metric")
    table.add_column("value", justify="right")
    spend_str = f"${total:.4f}" if cost_known else f"~${total:.4f} (some unknown)"
    table.add_row("cached attempts", str(len(attempts)))
    table.add_row("recorded spend", spend_str)
    table.add_row("spend cap", f"${settings.max_spend_usd:.2f}")
    table.add_row("remaining", f"${max(0.0, settings.max_spend_usd - total):.4f}")
    console.print(table)
    if not cache_path.is_file():
        console.print("[yellow]No run cache yet; nothing has been spent.[/yellow]")


def print_trace(root: Path, console: Console, trace_id: str) -> None:
    """Print the span tree with durations for one attempt (no API calls)."""
    import json

    from rich.tree import Tree

    path = root / "traces" / f"{trace_id}.jsonl"
    if not path.is_file():
        raise ConfigError(f"No trace file for {trace_id} at {path}")
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))

    def _sid(rec: dict[str, object]) -> str | None:
        value = rec.get("span_id")
        return value if isinstance(value, str) else None

    def _pid(rec: dict[str, object]) -> str | None:
        value = rec.get("parent_id")
        return value if isinstance(value, str) else None

    by_parent: dict[str | None, list[dict[str, object]]] = {}
    for rec in records:
        by_parent.setdefault(_pid(rec), []).append(rec)
    span_ids = {_sid(rec) for rec in records}
    roots = [rec for rec in records if _pid(rec) not in span_ids]

    def _label(rec: dict[str, object]) -> str:
        dur = rec.get("duration_ms")
        dur_s = f"{dur:.1f} ms" if isinstance(dur, (int, float)) else "n/a"
        return f"{rec.get('name')}  [dim]{dur_s}[/dim]"

    def _attach(parent_tree: Tree, rec: dict[str, object]) -> None:
        node = parent_tree.add(_label(rec))
        for child in by_parent.get(_sid(rec), []):
            _attach(node, child)

    tree = Tree(f"trace {trace_id}")
    for root_rec in roots:
        _attach(tree, root_rec)
    console.print(tree)


def _resolve_dir(root: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else root / path


def run_gate_live(root: Path, console: Console, candidate: str, baseline_dir: str | None) -> int:
    """Gate one candidate policy dir against a baseline. Returns the CLI exit code.

    0 accepted, 1 rejected, 2 error. Used by CI. The baseline defaults to the
    repository's active policy when `--baseline-dir` is omitted.
    """
    from gate.loop import evaluate_candidate, load_gate_config

    ctx = build_live_context(root, Settings())
    cand_path = _resolve_dir(root, candidate)
    if not cand_path.is_dir():
        raise ConfigError(f"Candidate policy directory not found: {cand_path}")
    candidate_policy = Policy.from_dir(cand_path.name, cand_path)

    if baseline_dir is None:
        base_name = active_version(root)
        base_path = root / "policies" / base_name
    else:
        base_path = _resolve_dir(root, baseline_dir)
        base_name = base_path.name
    if not base_path.is_dir():
        raise ConfigError(f"Baseline policy directory not found: {base_path}")
    baseline_policy = Policy.from_dir(base_name, base_path)

    sets: dict[str, list[EvalCase]] = dict(load_all(root / "evals").items())
    config = load_gate_config(root)
    k = ctx.settings.gate_runs_per_case

    async def _gate() -> GateDecision:
        cache = RunCache(root / ".cache" / "runs.jsonl")
        code_fp = code_fingerprint(root / "app")
        active_results: dict[str, SetResult] = {}
        for name, cases in sets.items():
            active_results[name] = await run_set(
                ctx, baseline_policy, cases, k, name, cache=cache, code_fp=code_fp
            )
        from gate.loop import _files_of

        decision, _ = await evaluate_candidate(
            ctx,
            root,
            cand_id=candidate_policy.version,
            source="candidate",
            active_version=base_name,
            active=baseline_policy,
            active_results=active_results,
            candidate_files=_files_of(candidate_policy),
            sets=sets,
            config=config,
        )
        return decision

    try:
        decision = asyncio.run(_gate())
    except BudgetExceeded as exc:
        console.print(f"[red]Spend cap reached, gate could not finish: {exc}[/red]")
        _print_spend(console, ctx)
        return 2

    _print_gate_decision(console, decision)
    # A well-known path so CI can post the decision without knowing the version.
    out = root / "reports" / "gate_decision.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(decision.model_dump_json(indent=2), encoding="utf-8")
    _print_spend(console, ctx)
    return 0 if decision.decision == "accepted" else 1


def _print_gate_decision(console: Console, decision: GateDecision) -> None:
    table = Table(title=f"gate: {decision.candidate_id} vs {decision.parent}")
    table.add_column("rule")
    table.add_column("passed")
    table.add_column("values")
    rules = decision.rules
    if rules is not None:
        table.add_row("R1 dev gain", _yn(rules.R1.passed), f"{rules.R1.a:.2f} -> {rules.R1.c:.2f}")
        regressed = ", ".join(rc.id for rc in rules.R2.regressed_cases) or "none"
        table.add_row("R2 safeguard", _yn(rules.R2.passed), f"regressed: {regressed}")
        table.add_row("R3 holdout", _yn(rules.R3.passed), f"{rules.R3.a:.2f} -> {rules.R3.c:.2f}")
        table.add_row(
            "R4 budgets",
            _yn(rules.R4.passed),
            f"cost x{rules.R4.cost_ratio:.2f}, p95 x{rules.R4.p95_ratio:.2f}",
        )
    console.print(table)
    verdict = decision.decision.upper()
    color = "green" if decision.decision == "accepted" else "red"
    failed = ", ".join(decision.failed_rules) or "none"
    console.print(f"[{color}]{verdict}[/{color}] (failed: {failed})")


def _yn(passed: bool) -> str:
    return "[green]yes[/green]" if passed else "[red]no[/red]"


def run_report_live(root: Path, console: Console, out_dir: Path) -> None:
    """Evaluate the first and latest policy on the frozen benchmark and write reports."""
    from gate import report

    ctx = build_live_context(root, Settings())
    current_sets: dict[str, list[EvalCase]] = dict(load_all(root / "evals").items())
    sets = load_benchmark_sets(root, current_sets)
    first_version = "v1"
    latest_version = active_version(root)
    k = ctx.settings.gate_runs_per_case

    first_policy = load_policy(root, first_version)
    first, latest = asyncio.run(
        evaluate_report_policies(
            ctx,
            root,
            first_policy,
            load_policy(root, latest_version) if latest_version != first_version else first_policy,
            sets,
            k,
        )
    )

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


def load_benchmark_sets(
    root: Path, current_sets: Mapping[str, list[EvalCase]]
) -> dict[str, list[EvalCase]]:
    """Load the immutable benchmark membership and resolve current case files."""
    path = root / "evals" / "benchmark.lock.yaml"
    try:
        locked = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read benchmark lock {path}: {exc}") from exc
    if not isinstance(locked, dict):
        raise ConfigError(f"Benchmark lock {path} must be a mapping")
    by_id = {case.id: case for cases in current_sets.values() for case in cases}
    benchmark: dict[str, list[EvalCase]] = {}
    for name in ("dev", "safeguard", "holdout"):
        ids = locked.get(name)
        if not isinstance(ids, list) or not all(isinstance(case_id, str) for case_id in ids):
            raise ConfigError(f"Benchmark lock {path} has invalid {name} ids")
        try:
            benchmark[name] = [by_id[case_id] for case_id in ids]
        except KeyError as exc:
            raise ConfigError(f"Benchmark lock references missing case {exc.args[0]}") from exc
    locked_ids = [case.id for cases in benchmark.values() for case in cases]
    if len(locked_ids) != len(set(locked_ids)):
        raise ConfigError(f"Benchmark lock {path} contains duplicate case ids")
    return benchmark


async def evaluate_report_policies(
    ctx: RunContext,
    root: Path,
    first_policy: Policy,
    latest_policy: Policy,
    sets: dict[str, list[EvalCase]],
    k: int,
) -> tuple[dict[str, SetResult], dict[str, SetResult]]:
    """Evaluate first and latest policies on one event loop without promotion."""
    cache = RunCache(root / ".cache" / "runs.jsonl")
    code_fp = code_fingerprint(root / "app")
    first, _ = await _run_baseline_sets(ctx, first_policy, sets, k, cache, code_fp)
    if latest_policy.content_hash == first_policy.content_hash:
        return first, first
    latest, _ = await _run_baseline_sets(ctx, latest_policy, sets, k, cache, code_fp)
    return first, latest


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
    rows: list[VersionRow] = []
    version_dirs = sorted(
        (p for p in (root / "policies").glob("v*") if p.is_dir()),
        key=lambda p: int("".join(ch for ch in p.name if ch.isdigit()) or "0"),
    )
    for vdir in version_dirs:
        manifest: dict[str, object] = {}
        manifest_path = vdir / "manifest.yaml"
        if manifest_path.is_file():
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
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
    from gate.report import CostComponent

    return CostLatency(
        total_usd=ctx.spend.total_cost_usd,
        cost_known=ctx.spend.cost_known,
        components=[
            CostComponent(name=name, cost_usd=amount)
            for name, amount in sorted(ctx.spend.component_costs.items())
        ],
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
    if not attempts or len(known) != len(attempts):
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

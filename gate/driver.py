"""Single responsibility: wire a live run context and drive the run commands.

This is the impure glue between the Typer CLI and the runner: it builds a live
`RunContext` (real Anthropic client, pricing, spend cap), runs one set or the
full baseline, prints readable tables with `rich`, and performs the safeguard
promotion that `baseline` requires. The pure runner, metrics, and graders do
the real work; this module only orchestrates and renders.
"""

from __future__ import annotations

import asyncio
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
from gate.metrics import SetResult
from gate.runner import run_set

_SAFEGUARD_THRESHOLD = 0.95


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

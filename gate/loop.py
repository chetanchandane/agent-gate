"""Single responsibility: drive the propose, guard, run, gate, accept loop.

This is the impure orchestration behind `agent-gate improve`. Each round reads
the active policy, runs it and a freshly proposed candidate on all three sets,
applies the policy guard and the gate, writes a decision record, and either
accepts the candidate (new version, safeguard promotion, optional git commit)
or files it under rejected. The pure gate, guard, metrics, and improver edit
logic do the real work; this module sequences them and performs the side effects.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml
from rich.console import Console

from app.context import RunContext
from app.errors import BudgetExceeded, InvalidProposal
from app.policy_types import POLICY_NODES, SHARED_RULES_FILE, Policy
from evals.schema import EvalCase, Severity, load_set
from gate import gate
from gate.cache import RunCache, code_fingerprint
from gate.gate import GateConfig, GateDecision
from gate.improver import apply_edits, propose
from gate.improver_input import RejectedSummary, SafeguardStatus, build_improver_input
from gate.metrics import SetResult
from gate.policy_guard import GuardLimits, check_candidate
from gate.runner import run_case, run_set

_SETS = ("dev", "safeguard", "holdout")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_gate_config(root: Path) -> GateConfig:
    """Read config/gate.yaml into a GateConfig (file read kept out of gate.py)."""
    data = yaml.safe_load((root / "config" / "gate.yaml").read_text(encoding="utf-8"))
    return GateConfig.from_mapping(data or {})


def _files_of(policy: Policy) -> dict[str, str]:
    files = {f"{node}.md": policy.nodes[node] for node in POLICY_NODES}
    files[SHARED_RULES_FILE] = policy.shared_rules
    return files


def _policy_from_files(version: str, files: dict[str, str]) -> Policy:
    from app.policy_types import content_hash

    return Policy(
        version=version,
        nodes={node: files[f"{node}.md"] for node in POLICY_NODES},
        shared_rules=files[SHARED_RULES_FILE],
        content_hash=content_hash(files),
    )


def _next_version(active: str) -> str:
    digits = "".join(ch for ch in active if ch.isdigit())
    n = int(digits) if digits else 1
    return f"v{n + 1}"


def _write_candidate(root: Path, cand_id: str, files: dict[str, str]) -> Path:
    cand_dir = root / "policies" / "candidates" / cand_id
    cand_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (cand_dir / name).write_text(text, encoding="utf-8")
    return cand_dir


async def _confirm_counts(
    ctx: RunContext,
    active: Policy,
    candidate: Policy,
    case: EvalCase,
    attempts: int,
) -> tuple[int, int]:
    """Rerun one case `attempts` times for A and C, bypassing the cache."""
    a_fail = 0
    c_fail = 0
    for attempt in range(1, attempts + 1):
        a_scored = await run_case(ctx, active, case, attempt, cache=None, write_traces=False)
        if not a_scored.passed:
            a_fail += 1
        c_scored = await run_case(ctx, candidate, case, attempt, cache=None, write_traces=False)
        if not c_scored.passed:
            c_fail += 1
    return a_fail, c_fail


def _write_decision(root: Path, decision: GateDecision) -> None:
    decisions_dir = root / "reports" / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    (decisions_dir / f"{decision.candidate_id}.json").write_text(
        decision.model_dump_json(indent=2), encoding="utf-8"
    )
    history = root / "reports" / "history.jsonl"
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"summary": decision.summary_line()}) + "\n")


def _accept(
    root: Path,
    files: dict[str, str],
    new_version: str,
    parent: str,
    note: str,
) -> None:
    version_dir = root / "policies" / new_version
    version_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (version_dir / name).write_text(text, encoding="utf-8")
    manifest = {
        "version": new_version,
        "parent": parent,
        "created_at": _now_iso(),
        "source": "improver",
        "note": note[:200],
    }
    (version_dir / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    (root / "policies" / "ACTIVE").write_text(new_version + "\n", encoding="utf-8")


def _reject(root: Path, cand_id: str, files: dict[str, str], decision: GateDecision) -> None:
    rej_dir = root / "policies" / "rejected" / cand_id
    rej_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (rej_dir / name).write_text(text, encoding="utf-8")
    (rej_dir / "decision.json").write_text(decision.model_dump_json(indent=2), encoding="utf-8")


def promote_dev_to_safeguard(
    root: Path, dev_active: SetResult, dev_candidate: SetResult, new_version: str
) -> list[str]:
    """Move dev cases now passing k of k under C that were weaker under A."""
    a_scores = {cs.case_id: cs for cs in dev_active.metrics.case_scores}
    moved: list[str] = []
    for cs in dev_candidate.metrics.case_scores:
        a = a_scores.get(cs.case_id)
        full_under_c = cs.passed_attempts == cs.total_attempts and cs.total_attempts > 0
        weaker_under_a = a is None or a.passed_attempts < a.total_attempts
        if not (full_under_c and weaker_under_a):
            continue
        src = root / "evals" / "dev" / f"{cs.case_id}.yaml"
        dst = root / "evals" / "safeguard" / f"{cs.case_id}.yaml"
        if src.is_file():
            _move_case_with_meta(src, dst, new_version)
            moved.append(cs.case_id)
    return sorted(moved)


def _move_case_with_meta(src: Path, dst: Path, new_version: str) -> None:
    data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    meta = data.get("meta") or {}
    meta["promoted_in"] = new_version
    data["meta"] = meta
    dst.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    src.unlink()


def _git_commit(root: Path, message: str, paths: list[Path]) -> None:
    subprocess.run(
        ["git", "-C", str(root), "add", "--", *(str(path) for path in paths)],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "commit", "-m", message], check=True)


async def evaluate_candidate(
    ctx: RunContext,
    root: Path,
    *,
    cand_id: str,
    source: str,
    active_version: str,
    active: Policy,
    active_results: dict[str, SetResult],
    candidate_files: dict[str, str],
    sets: dict[str, list[EvalCase]],
    config: GateConfig,
) -> tuple[GateDecision, dict[str, SetResult] | None]:
    """Run one candidate through the full gate and write its decision record.

    Shared by the improve loop and the adversarial command so both use the
    identical path: policy guard, run all three sets, the R2 confirmation
    reruns, and the pure `gate.decide`. This is a read-and-record step only; it
    performs no accept, reject, or promotion side effects. On a guard violation
    the returned candidate results are None (the sets were not run).
    """
    cache = RunCache(root / ".cache" / "runs.jsonl")
    code_fp = code_fingerprint(root / "app")
    k = ctx.settings.gate_runs_per_case

    async def _run(policy: Policy, name: str) -> SetResult:
        return await run_set(ctx, policy, sets[name], k, name, cache=cache, code_fp=code_fp)

    parent_files = _files_of(active)
    eval_messages = [c.message for cases in sets.values() for c in cases]
    holdout_ids = [c.id for c in sets["holdout"]]
    violation = check_candidate(
        candidate_files=candidate_files,
        parent_files=parent_files,
        eval_messages=eval_messages,
        holdout_ids=holdout_ids,
        limits=GuardLimits(),
    )
    if violation is not None:
        decision = gate.decide(
            candidate_id=cand_id,
            source=source,
            parent=active_version,
            created_at=_now_iso(),
            active=active_results,
            candidate=active_results,
            severities={},
            config=config,
            guard_violation=violation,
        )
        _write_decision(root, decision)
        return decision, None

    candidate = _policy_from_files(cand_id, candidate_files)
    candidate_results = {name: await _run(candidate, name) for name in _SETS}

    severities: dict[str, Severity] = {c.id: c.severity for c in sets["safeguard"]}
    safeguard_by_id = {c.id: c for c in sets["safeguard"]}

    flags = gate.check_r2(
        active_results["safeguard"], candidate_results["safeguard"], severities, config
    ).regressed_cases
    confirm_map: dict[str, tuple[int, int]] = {}
    for flag in flags:
        case = safeguard_by_id[flag.id]
        confirm_map[flag.id] = await _confirm_counts(
            ctx, active, candidate, case, config.confirmation_attempts
        )

    def confirm(case_id: str, severity: Severity) -> tuple[int, int]:
        return confirm_map.get(case_id, (0, 0))

    decision = gate.decide(
        candidate_id=cand_id,
        source=source,
        parent=active_version,
        created_at=_now_iso(),
        active=active_results,
        candidate=candidate_results,
        severities=severities,
        config=config,
        confirm=confirm,
        cost_usd=ctx.spend.total_cost_usd,
    )
    _write_decision(root, decision)
    return decision, candidate_results


async def run_round(
    ctx: RunContext,
    root: Path,
    console: Console,
    cand_id: str,
    sets: dict[str, list[EvalCase]],
    config: GateConfig,
    rejected: list[RejectedSummary],
    *,
    git_commit: bool = False,
) -> GateDecision:
    """One improve round. Returns the decision; mutates policies on acceptance."""
    cache = RunCache(root / ".cache" / "runs.jsonl")
    code_fp = code_fingerprint(root / "app")
    k = ctx.settings.gate_runs_per_case

    active_version = (root / "policies" / "ACTIVE").read_text(encoding="utf-8").strip()
    active = Policy.from_dir(active_version, root / "policies" / active_version)

    async def _run(policy: Policy, name: str) -> SetResult:
        return await run_set(ctx, policy, sets[name], k, name, cache=cache, code_fp=code_fp)

    active_results = {name: await _run(active, name) for name in _SETS}

    safeguard_status = [
        SafeguardStatus(id=cs.case_id, passed=cs.score >= 1.0)
        for cs in active_results["safeguard"].metrics.case_scores
    ]
    improver_input = build_improver_input(
        active, active_results["dev"], sets["dev"], safeguard_status, rejected
    )

    try:
        proposal = await propose(ctx, improver_input)
    except InvalidProposal as exc:
        return _record_rejected_no_run(root, cand_id, active_version, "invalid_proposal", str(exc))

    parent_files = _files_of(active)
    try:
        candidate_files = apply_edits(parent_files, proposal)
    except InvalidProposal as exc:
        return _record_rejected_no_run(root, cand_id, active_version, "invalid_proposal", str(exc))

    _write_candidate(root, cand_id, candidate_files)

    decision, candidate_results = await evaluate_candidate(
        ctx,
        root,
        cand_id=cand_id,
        source="improver",
        active_version=active_version,
        active=active,
        active_results=active_results,
        candidate_files=candidate_files,
        sets=sets,
        config=config,
    )
    if candidate_results is None:
        _reject(root, cand_id, candidate_files, decision)
        console.print(f"[yellow]{cand_id} rejected: {','.join(decision.failed_rules)}[/yellow]")
        return decision

    if decision.decision == "accepted":
        new_version = _next_version(active_version)
        _accept(root, candidate_files, new_version, active_version, proposal.rationale)
        moved = promote_dev_to_safeguard(
            root, active_results["dev"], candidate_results["dev"], new_version
        )
        dev_delta = decision.rules.R1.c - decision.rules.R1.a if decision.rules else 0.0
        console.print(
            f"[green]{cand_id} accepted as {new_version} "
            f"(dev {dev_delta:+.2f}, {len(moved)} promoted)[/green]"
        )
        if git_commit:
            commit_paths = [
                Path("policies") / new_version,
                Path("policies") / "ACTIVE",
                Path("reports") / "decisions" / f"{cand_id}.json",
                Path("reports") / "history.jsonl",
            ]
            commit_paths.extend(
                Path("evals") / "safeguard" / f"{case_id}.yaml" for case_id in moved
            )
            commit_paths.extend(Path("evals") / "dev" / f"{case_id}.yaml" for case_id in moved)
            _git_commit(
                root,
                f"policy: {new_version} accepted (dev {dev_delta:+.2f}, "
                f"{len(decision.rules.R2.regressed_cases) if decision.rules else 0} regressions)",
                commit_paths,
            )
    else:
        _reject(root, cand_id, candidate_files, decision)
        console.print(f"[yellow]{cand_id} rejected: {','.join(decision.failed_rules)}[/yellow]")

    return decision


def _record_rejected_no_run(
    root: Path, cand_id: str, parent: str, reason: str, detail: str
) -> GateDecision:
    decision = GateDecision(
        candidate_id=cand_id,
        source="improver",
        parent=parent,
        decision="rejected",
        failed_rules=[reason],
        created_at=_now_iso(),
    )
    _write_decision(root, decision)
    return decision


async def run_improve(
    ctx: RunContext,
    root: Path,
    console: Console,
    rounds: int,
    *,
    git_commit: bool = False,
) -> list[GateDecision]:
    """Run the full improve loop for the given number of rounds."""
    if rounds < 1:
        raise ValueError("rounds must be at least 1")
    config = load_gate_config(root)
    sets = {name: load_set(root / "evals" / name) for name in _SETS}
    rejected: list[RejectedSummary] = []
    decisions: list[GateDecision] = []
    for round_no in range(1, rounds + 1):
        cand_id = f"cand-{round_no:04d}"
        try:
            decision = await run_round(
                ctx, root, console, cand_id, sets, config, rejected, git_commit=git_commit
            )
        except BudgetExceeded as exc:
            console.print(
                f"[red]Spend cap reached, stopping after {round_no - 1} round(s): {exc}[/red]"
            )
            break
        decisions.append(decision)
        if decision.decision == "rejected":
            rejected.append(
                RejectedSummary(
                    rationale="(see decision record)",
                    reason=",".join(decision.failed_rules),
                )
            )
            # Reload sets in case nothing changed; on acceptance sets may move.
        else:
            sets = {name: load_set(root / "evals" / name) for name in _SETS}
    return decisions

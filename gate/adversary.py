"""Single responsibility: load the hand written adversarial candidates and run them.

The three candidates in `gate/adversarial/` are written by hand to prove the
gate rejects bad policies; they are not improver proposals. Loading and parsing
them and turning each into a `Proposal` are pure. `run_adversarial` is the
impure driver: it runs each candidate through the exact same gate path as the
improve loop (via `loop.evaluate_candidate`), with `source="adversarial"`, and
records a decision. It never accepts, promotes, or mutates ACTIVE, and the
candidates are kept out of the improver acceptance statistics.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ValidationError
from rich.console import Console

from app.context import RunContext
from app.errors import AgentGateError
from app.policy_types import Policy
from evals.schema import EvalCase, load_set
from gate.gate import GateDecision
from gate.improver import Edit, Proposal, apply_edits
from gate.loop import _files_of, _write_candidate, evaluate_candidate, load_gate_config
from gate.metrics import SetResult
from gate.runner import run_set

_SETS = ("dev", "safeguard", "holdout")
_SOURCE = "adversarial"
CANDIDATES_DIR = Path(__file__).parent / "adversarial"


class AdversarialCandidate(BaseModel):
    """One hand written candidate: metadata plus the edits to apply to ACTIVE."""

    id: str
    label: str
    expectation: str
    rationale: str
    edits: list[Edit]

    def proposal(self) -> Proposal:
        return Proposal(rationale=self.rationale, edits=self.edits)


class InvalidAdversarialCandidate(AgentGateError):
    """A candidate file is missing, is not valid JSON, or has the wrong shape."""


def load_candidates(directory: Path = CANDIDATES_DIR) -> list[AdversarialCandidate]:
    """Load and parse every `*.json` candidate in the directory, sorted by id."""
    candidates: list[AdversarialCandidate] = []
    for path in sorted(directory.glob("*.json")):
        raw = path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvalidAdversarialCandidate(f"{path.name} is not valid JSON: {exc}") from exc
        try:
            candidates.append(AdversarialCandidate.model_validate(data))
        except ValidationError as exc:
            raise InvalidAdversarialCandidate(f"{path.name} has the wrong shape: {exc}") from exc
    if not candidates:
        raise InvalidAdversarialCandidate(f"no candidate files found in {directory}")
    ids = [c.id for c in candidates]
    if len(set(ids)) != len(ids):
        raise InvalidAdversarialCandidate(f"duplicate candidate id in {directory}")
    return candidates


async def run_adversarial(
    ctx: RunContext,
    root: Path,
    console: Console,
    directory: Path = CANDIDATES_DIR,
) -> list[GateDecision]:
    """Run every adversarial candidate through the gate and record its decision."""
    candidates = load_candidates(directory)
    config = load_gate_config(root)
    sets: dict[str, list[EvalCase]] = {name: load_set(root / "evals" / name) for name in _SETS}

    active_version = (root / "policies" / "ACTIVE").read_text(encoding="utf-8").strip()
    active = Policy.from_dir(active_version, root / "policies" / active_version)
    parent_files = _files_of(active)

    from gate.cache import RunCache, code_fingerprint

    cache = RunCache(root / ".cache" / "runs.jsonl")
    code_fp = code_fingerprint(root / "app")
    k = ctx.settings.gate_runs_per_case
    active_results: dict[str, SetResult] = {
        name: await run_set(ctx, active, sets[name], k, name, cache=cache, code_fp=code_fp)
        for name in _SETS
    }

    decisions: list[GateDecision] = []
    for candidate in candidates:
        candidate_files = apply_edits(parent_files, candidate.proposal())
        _write_candidate(root, candidate.id, candidate_files)
        decision, _ = await evaluate_candidate(
            ctx,
            root,
            cand_id=candidate.id,
            source=_SOURCE,
            active_version=active_version,
            active=active,
            active_results=active_results,
            candidate_files=candidate_files,
            sets=sets,
            config=config,
        )
        decisions.append(decision)
        verdict = decision.decision
        color = "red" if verdict == "accepted" else "green"
        detail = ",".join(decision.failed_rules) if decision.failed_rules else "no rule failed"
        console.print(
            f"[{color}]{candidate.id}: {verdict} ({detail})[/{color}] - {candidate.label}"
        )
    return decisions

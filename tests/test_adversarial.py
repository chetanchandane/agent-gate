"""Offline tests for the adversarial candidates and the adversarial command.

The three candidates are hand written to prove the gate rejects bad policies.
These tests cover the mechanics: the candidates load and apply to v1, and the
command runs each through the same gate path with source "adversarial", records
a decision, and never mutates ACTIVE or promotes a version. The LLM driven
regression itself is shown only by the live run (deferred), because the offline
FakeLLM ignores policy text, so active and candidate behave identically here.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import yaml
from rich.console import Console

from evals.schema import EvalCase
from gate import adversary
from gate.improver import apply_edits
from gate.loop import _files_of
from tests.conftest import load_v1, make_context, repo_root
from tests.test_runner import _emergency_case, _happy_case, _responder

_EXPECTED_IDS = {
    "adv-always-book-earliest",
    "adv-shortened-shared-rules",
    "adv-water-emergency",
}


def test_load_candidates_returns_the_three_hand_written_candidates() -> None:
    candidates = adversary.load_candidates()
    assert {c.id for c in candidates} == _EXPECTED_IDS
    for c in candidates:
        assert c.label
        assert c.expectation
        assert c.edits


def test_every_candidate_applies_cleanly_to_v1() -> None:
    parent = _files_of(load_v1())
    for c in adversary.load_candidates():
        candidate_files = apply_edits(parent, c.proposal())
        changed = [name for name in candidate_files if candidate_files[name] != parent[name]]
        assert len(changed) == 1, f"{c.id} should touch exactly one file, touched {changed}"


def test_candidate_proposal_roundtrips() -> None:
    c = next(c for c in adversary.load_candidates() if c.id == "adv-water-emergency")
    proposal = c.proposal()
    assert proposal.rationale == c.rationale
    assert proposal.edits == c.edits


def _build_tmp_repo(tmp_path: Path) -> Path:
    """Copy v1, ACTIVE, and gate.yaml into tmp_path and write a tiny eval set."""
    root = tmp_path
    (root / "policies").mkdir(parents=True)
    shutil.copytree(repo_root() / "policies" / "v1", root / "policies" / "v1")
    (root / "policies" / "ACTIVE").write_text("v1\n", encoding="utf-8")
    (root / "config").mkdir()
    shutil.copyfile(repo_root() / "config" / "gate.yaml", root / "config" / "gate.yaml")
    sets: dict[str, list[EvalCase]] = {
        "dev": [_happy_case()],
        "safeguard": [_emergency_case()],
        "holdout": [_happy_case()],
    }
    for name, cases in sets.items():
        set_dir = root / "evals" / name
        set_dir.mkdir(parents=True)
        for case in cases:
            (set_dir / f"{case.id}.yaml").write_text(
                yaml.safe_dump(case.model_dump(exclude_none=True), sort_keys=False),
                encoding="utf-8",
            )
    return root


async def test_run_adversarial_records_a_decision_per_candidate(tmp_path: Path) -> None:
    root = _build_tmp_repo(tmp_path)
    ctx = make_context(_responder)
    console = Console(file=io.StringIO(), width=100)

    decisions = await adversary.run_adversarial(ctx, root, console)

    assert {d.candidate_id for d in decisions} == _EXPECTED_IDS
    assert all(d.source == "adversarial" for d in decisions)
    for d in decisions:
        assert (root / "reports" / "decisions" / f"{d.candidate_id}.json").is_file()


async def test_run_adversarial_never_mutates_active_or_promotes(tmp_path: Path) -> None:
    root = _build_tmp_repo(tmp_path)
    ctx = make_context(_responder)
    console = Console(file=io.StringIO(), width=100)

    await adversary.run_adversarial(ctx, root, console)

    assert (root / "policies" / "ACTIVE").read_text(encoding="utf-8").strip() == "v1"
    assert not (root / "policies" / "v2").exists()
    # Candidates are written for inspection but ACTIVE is untouched.
    for cand_id in _EXPECTED_IDS:
        assert (root / "policies" / "candidates" / cand_id).is_dir()

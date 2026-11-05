"""Offline tests for the improve loop side effects and pure helpers."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.runner_types import RunResult
from app.state import AgentState
from gate import loop
from gate.metrics import ScoredAttempt, SetResult


def _set(name: str, cases: dict[str, int], k: int) -> SetResult:
    scored: list[ScoredAttempt] = []
    for case_id, passes in cases.items():
        for attempt in range(1, k + 1):
            state = AgentState(message="m", now="2026-11-10T09:00:00")
            result = RunResult(
                case_id=case_id, policy_version="v", attempt=attempt, final_state=state
            )
            scored.append(
                ScoredAttempt(
                    result=result, passed=attempt <= passes, deterministic_passed=attempt <= passes
                )
            )
    return SetResult.build(name, k, scored)


def test_next_version_increments() -> None:
    assert loop._next_version("v1") == "v2"
    assert loop._next_version("v9") == "v10"


def test_load_gate_config_reads_repo_defaults() -> None:
    cfg = loop.load_gate_config(Path(__file__).parents[1])
    assert cfg.min_dev_gain == 0.05
    assert cfg.confirmation_attempts == 10


def test_promote_dev_to_safeguard_moves_newly_solid_cases(tmp_path: Path) -> None:
    dev = tmp_path / "evals" / "dev"
    safeguard = tmp_path / "evals" / "safeguard"
    dev.mkdir(parents=True)
    safeguard.mkdir(parents=True)
    (dev / "fixed-1.yaml").write_text("id: fixed-1\nmessage: hi\n", encoding="utf-8")
    (dev / "still-weak.yaml").write_text("id: still-weak\nmessage: hi\n", encoding="utf-8")

    active = _set("dev", {"fixed-1": 2, "still-weak": 1}, 5)  # both weak under A
    candidate = _set("dev", {"fixed-1": 5, "still-weak": 3}, 5)  # only fixed-1 now solid

    moved = loop.promote_dev_to_safeguard(tmp_path, active, candidate, "v2")
    assert moved == ["fixed-1"]
    assert (safeguard / "fixed-1.yaml").is_file()
    assert not (dev / "fixed-1.yaml").is_file()
    data = yaml.safe_load((safeguard / "fixed-1.yaml").read_text(encoding="utf-8"))
    assert data["meta"]["promoted_in"] == "v2"


def test_promote_skips_cases_already_solid_under_active(tmp_path: Path) -> None:
    dev = tmp_path / "evals" / "dev"
    safeguard = tmp_path / "evals" / "safeguard"
    dev.mkdir(parents=True)
    safeguard.mkdir(parents=True)
    (dev / "always.yaml").write_text("id: always\nmessage: hi\n", encoding="utf-8")
    active = _set("dev", {"always": 5}, 5)
    candidate = _set("dev", {"always": 5}, 5)
    assert loop.promote_dev_to_safeguard(tmp_path, active, candidate, "v2") == []
    assert (dev / "always.yaml").is_file()


def test_accept_writes_version_and_active(tmp_path: Path) -> None:
    (tmp_path / "policies").mkdir()
    files = {"intake.md": "a\n", "shared_rules.md": "b\n"}
    loop._accept(tmp_path, files, "v2", "v1", "a helpful note")
    assert (tmp_path / "policies" / "v2" / "intake.md").read_text(encoding="utf-8") == "a\n"
    assert (tmp_path / "policies" / "ACTIVE").read_text(encoding="utf-8").strip() == "v2"
    manifest = yaml.safe_load((tmp_path / "policies" / "v2" / "manifest.yaml").read_text())
    assert manifest["parent"] == "v1"
    assert manifest["source"] == "improver"


def test_reject_files_candidate_with_decision(tmp_path: Path) -> None:
    decision = loop.GateDecision(
        candidate_id="cand-0001",
        source="improver",
        parent="v1",
        decision="rejected",
        failed_rules=["R1"],
    )
    loop._reject(tmp_path, "cand-0001", {"intake.md": "x\n"}, decision)
    rej = tmp_path / "policies" / "rejected" / "cand-0001"
    assert (rej / "intake.md").is_file()
    assert (rej / "decision.json").is_file()


def test_record_rejected_no_run_writes_decision_and_history(tmp_path: Path) -> None:
    decision = loop._record_rejected_no_run(
        tmp_path, "cand-0002", "v1", "invalid_proposal", "bad json"
    )
    assert decision.decision == "rejected"
    assert decision.failed_rules == ["invalid_proposal"]
    assert (tmp_path / "reports" / "decisions" / "cand-0002.json").is_file()
    history = (tmp_path / "reports" / "history.jsonl").read_text(encoding="utf-8")
    assert "cand-0002" in history

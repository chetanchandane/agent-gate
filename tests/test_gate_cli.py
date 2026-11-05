"""Offline tests for the gate, cost, and trace commands and their driver glue."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest
import yaml
from rich.console import Console

from app.runner_types import RunResult
from app.state import AgentState
from gate import driver
from gate.cache import RunCache
from gate.metrics import ScoredAttempt
from tests.conftest import make_context, repo_root
from tests.test_runner import _emergency_case, _happy_case, _responder


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, width=100), buffer


def _build_repo_with_candidate(tmp_path: Path) -> Path:
    """Copy v1 as both the active policy and a candidate dir, with a tiny eval set."""
    root = tmp_path
    (root / "policies").mkdir(parents=True)
    shutil.copytree(repo_root() / "policies" / "v1", root / "policies" / "v1")
    shutil.copytree(repo_root() / "policies" / "v1", root / "policies" / "v2")
    (root / "policies" / "ACTIVE").write_text("v1\n", encoding="utf-8")
    (root / "config").mkdir()
    shutil.copyfile(repo_root() / "config" / "gate.yaml", root / "config" / "gate.yaml")
    for name, cases in {
        "dev": [_happy_case()],
        "safeguard": [_emergency_case()],
        "holdout": [_happy_case()],
    }.items():
        set_dir = root / "evals" / name
        set_dir.mkdir(parents=True)
        for case in cases:
            (set_dir / f"{case.id}.yaml").write_text(
                yaml.safe_dump(case.model_dump(exclude_none=True), sort_keys=False),
                encoding="utf-8",
            )
    return root


def test_run_gate_live_rejects_no_gain_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _build_repo_with_candidate(tmp_path)
    monkeypatch.setattr(driver, "build_live_context", lambda r, s: make_context(_responder))
    console, _ = _console()

    # Candidate v2 is identical to v1, so dev shows no gain: R1 fails, exit code 1.
    code = driver.run_gate_live(root, console, "policies/v2", "policies/v1")

    assert code == 1
    decision_path = root / "reports" / "gate_decision.json"
    assert decision_path.is_file()
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    assert decision["source"] == "candidate"
    assert decision["decision"] == "rejected"


def test_run_gate_live_errors_on_missing_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _build_repo_with_candidate(tmp_path)
    monkeypatch.setattr(driver, "build_live_context", lambda r, s: make_context(_responder))
    console, _ = _console()
    from app.errors import ConfigError

    with pytest.raises(ConfigError):
        driver.run_gate_live(root, console, "policies/does-not-exist", None)


def _scored(case_id: str, cost: float | None) -> ScoredAttempt:
    state = AgentState(message="m", now="2026-11-10T09:00:00")
    result = RunResult(
        case_id=case_id, policy_version="v1", attempt=1, final_state=state, cost_usd=cost
    )
    return ScoredAttempt(result=result, passed=True, deterministic_passed=True)


def test_run_cost_sums_cached_spend(tmp_path: Path) -> None:
    cache = RunCache(tmp_path / ".cache" / "runs.jsonl")
    cache.put("k1", _scored("c1", 0.002))
    cache.put("k2", _scored("c2", 0.003))
    console, buffer = _console()
    driver.run_cost(tmp_path, console)
    out = buffer.getvalue()
    assert "cached attempts" in out
    assert "0.0050" in out  # 0.002 + 0.003
    assert "remaining" in out


def test_run_cost_flags_unknown_spend(tmp_path: Path) -> None:
    cache = RunCache(tmp_path / ".cache" / "runs.jsonl")
    cache.put("k1", _scored("c1", None))
    console, buffer = _console()
    driver.run_cost(tmp_path, console)
    output = buffer.getvalue()
    assert "unknown" in output
    assert "remaining" in output


def test_print_trace_renders_span_tree(tmp_path: Path) -> None:
    traces = tmp_path / "traces"
    traces.mkdir()
    records = [
        {"span_id": "aaaa", "parent_id": None, "name": "node.route", "duration_ms": 1.5},
        {"span_id": "bbbb", "parent_id": "aaaa", "name": "llm.intake", "duration_ms": 42.0},
    ]
    (traces / "abc123.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records), encoding="utf-8"
    )
    console, buffer = _console()
    driver.print_trace(tmp_path, console, "abc123")
    out = buffer.getvalue()
    assert "node.route" in out
    assert "llm.intake" in out
    assert "42.0 ms" in out


def test_print_trace_missing_file_raises(tmp_path: Path) -> None:
    from app.errors import ConfigError

    console, _ = _console()
    with pytest.raises(ConfigError):
        driver.print_trace(tmp_path, console, "nope")

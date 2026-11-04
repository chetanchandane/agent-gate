"""Unit tests for the code fingerprint, cache key, and the JSONL run cache."""

from __future__ import annotations

from pathlib import Path

from app.runner_types import RunResult
from app.state import AgentState
from gate.cache import RunCache, code_fingerprint, make_key
from gate.metrics import ScoredAttempt
from tests.conftest import repo_root


def _scored(case_id: str = "happy_path-001") -> ScoredAttempt:
    state = AgentState(message="m", now="2026-11-10T09:00:00")
    result = RunResult(case_id=case_id, policy_version="v1", attempt=1, final_state=state)
    return ScoredAttempt(result=result, passed=True, deterministic_passed=True)


def test_code_fingerprint_is_stable_and_nonempty() -> None:
    app_dir = repo_root() / "app"
    fp1 = code_fingerprint(app_dir)
    fp2 = code_fingerprint(app_dir)
    assert fp1 == fp2
    assert len(fp1) == 64


def test_make_key_depends_on_every_input() -> None:
    base = make_key("hash", "case", 1, "model", "fp")
    assert base != make_key("hash2", "case", 1, "model", "fp")
    assert base != make_key("hash", "case", 2, "model", "fp")
    assert base != make_key("hash", "case", 1, "model2", "fp")
    assert base != make_key("hash", "case", 1, "model", "fp2")
    assert base != make_key("hash", "case", 1, "model", "fp", "judge2")
    assert base != make_key("hash", "case", 1, "model", "fp", "", "case2")
    assert base == make_key("hash", "case", 1, "model", "fp")


def test_run_cache_roundtrip_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "runs.jsonl"
    cache = RunCache(path)
    assert cache.get("k") is None
    cache.put("k", _scored("happy_path-002"))
    assert cache.get("k") is not None

    reloaded = RunCache(path)
    assert len(reloaded) == 1
    hit = reloaded.get("k")
    assert hit is not None
    assert hit.result.case_id == "happy_path-002"


def test_missing_cache_file_is_empty(tmp_path: Path) -> None:
    cache = RunCache(tmp_path / "does_not_exist.jsonl")
    assert len(cache) == 0

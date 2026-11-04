"""Offline tests for the driver: context wiring, promotion, and orchestration."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from app.errors import ConfigError
from app.settings import Settings
from evals.schema import EvalCase
from gate import driver
from gate.metrics import SetResult
from tests.conftest import load_v1, make_context
from tests.test_runner import _SEED, _happy_case, _responder


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, width=100), buffer


def test_build_live_context_requires_api_key(tmp_path: Path) -> None:
    settings = Settings(ANTHROPIC_API_KEY="")  # type: ignore[call-arg]
    with pytest.raises(ConfigError):
        driver.build_live_context(tmp_path, settings)


def test_build_live_context_with_key(tmp_path: Path) -> None:
    settings = Settings(ANTHROPIC_API_KEY="sk-test")  # type: ignore[call-arg]
    ctx = driver.build_live_context(tmp_path, settings)
    assert ctx.settings.anthropic_api_key == "sk-test"
    assert ctx.traces_dir == tmp_path / "traces"


def test_active_version_and_load_policy_against_repo() -> None:
    root = Path(__file__).parents[1]
    assert driver.active_version(root) == "v1"
    policy = driver.load_policy(root, "v1")
    assert policy.version == "v1"


def test_active_version_missing_raises(tmp_path: Path) -> None:
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "ACTIVE").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        driver.active_version(tmp_path)


def _fake_set_result(name: str, scores: dict[str, int], k: int) -> SetResult:
    from app.runner_types import RunResult
    from app.state import AgentState
    from gate.metrics import ScoredAttempt

    scored: list[ScoredAttempt] = []
    for case_id, passes in scores.items():
        for attempt in range(1, k + 1):
            state = AgentState(message="m", now="2026-11-10T09:00:00")
            result = RunResult(
                case_id=case_id, policy_version="v1", attempt=attempt, final_state=state
            )
            scored.append(
                ScoredAttempt(
                    result=result,
                    passed=attempt <= passes,
                    deterministic_passed=attempt <= passes,
                )
            )
    return SetResult.build(name, k, scored)


def test_promote_weak_safeguard_moves_only_weak_cases(tmp_path: Path) -> None:
    safeguard = tmp_path / "evals" / "safeguard"
    dev = tmp_path / "evals" / "dev"
    safeguard.mkdir(parents=True)
    dev.mkdir(parents=True)
    (safeguard / "strong-1.yaml").write_text("x", encoding="utf-8")
    (safeguard / "weak-1.yaml").write_text("y", encoding="utf-8")

    result = _fake_set_result("safeguard", {"strong-1": 5, "weak-1": 3}, k=5)
    moved = driver.promote_weak_safeguard(tmp_path, result)

    assert moved == ["weak-1"]
    assert (dev / "weak-1.yaml").is_file()
    assert (safeguard / "strong-1.yaml").is_file()
    assert not (safeguard / "weak-1.yaml").is_file()


def test_evaluate_one_set_prints_metrics(tmp_path: Path) -> None:
    ctx = make_context(_responder)
    console, buffer = _console()
    result = driver.evaluate_one_set(ctx, tmp_path, console, load_v1(), [_happy_case()], 2, "dev")
    assert result is not None
    assert result.metrics.pass_rate == 1.0
    out = buffer.getvalue()
    assert "pass rate" in out
    assert "Spend" in out


def test_evaluate_baseline_runs_sets_without_promotion(tmp_path: Path) -> None:
    ctx = make_context(_responder)
    console, buffer = _console()
    # Build a safeguard dir so promotion has somewhere to look (happy case passes).
    (tmp_path / "evals" / "safeguard").mkdir(parents=True)
    (tmp_path / "evals" / "dev").mkdir(parents=True)
    case = _happy_case()
    EvalCase.model_validate(case.model_dump())  # sanity: still valid
    sets: dict[str, list[EvalCase]] = {"safeguard": [case]}
    (tmp_path / "evals" / "safeguard" / f"{case.id}.yaml").write_text("x", encoding="utf-8")

    results = driver.evaluate_baseline(ctx, tmp_path, console, load_v1(), sets, 2)
    assert results["safeguard"].metrics.pass_rate == 1.0
    # Passing case is not promoted, file stays in safeguard.
    assert (tmp_path / "evals" / "safeguard" / f"{case.id}.yaml").is_file()
    assert "hold at or above" in buffer.getvalue()


def test_seed_shape_is_usable() -> None:
    assert _SEED["service_areas"] == ["14604"]

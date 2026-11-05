"""Assert the improver input can never contain holdout data.

Builds a real ImproverInput from the active policy, a synthetic dev SetResult,
and safeguard summaries, then serializes the improver prompt and checks that no
holdout id or holdout message text appears anywhere in it. The builder has no
holdout parameter, so this guards the serialization as well as the shape.
"""

from __future__ import annotations

from app.runner_types import RunResult
from app.state import AgentState
from evals.schema import load_set
from gate.improver import render_user_prompt
from gate.improver_input import SafeguardStatus, build_improver_input
from gate.metrics import ScoredAttempt, SetResult
from tests.conftest import load_v1, repo_root


def _dev_result(dev_cases: list) -> SetResult:
    scored: list[ScoredAttempt] = []
    for case in dev_cases[:5]:
        state = AgentState(message=case.message, now=case.now)
        result = RunResult(
            case_id=case.id, policy_version="v1", attempt=1, final_state=state, reply="hi"
        )
        scored.append(
            ScoredAttempt(
                result=result,
                passed=False,
                deterministic_passed=False,
                failure_reasons=["route: expected schedule"],
                failure_categories=["route"],
            )
        )
    return SetResult.build("dev", 1, scored)


def test_improver_input_has_no_holdout_data() -> None:
    root = repo_root()
    dev_cases = load_set(root / "evals" / "dev")
    safeguard_cases = load_set(root / "evals" / "safeguard")
    holdout_cases = load_set(root / "evals" / "holdout")

    safeguard = [SafeguardStatus(id=c.id, passed=True) for c in safeguard_cases]
    improver_input = build_improver_input(
        load_v1(), _dev_result(dev_cases), dev_cases, safeguard, []
    )
    prompt = render_user_prompt(improver_input)

    for case in holdout_cases:
        assert case.id not in prompt, f"holdout id {case.id} leaked into improver prompt"
        # A long substring of the holdout message must not appear verbatim.
        snippet = case.message.strip()[:40]
        if len(snippet) >= 20:
            assert snippet not in prompt, f"holdout message for {case.id} leaked"


def test_safeguard_entries_carry_only_id_and_pass_fail() -> None:
    root = repo_root()
    dev_cases = load_set(root / "evals" / "dev")
    safeguard_cases = load_set(root / "evals" / "safeguard")
    safeguard = [SafeguardStatus(id=c.id, passed=False) for c in safeguard_cases]
    improver_input = build_improver_input(
        load_v1(), _dev_result(dev_cases), dev_cases, safeguard, []
    )
    # The safeguard entries expose nothing but id and passed.
    for entry in improver_input.safeguard:
        assert set(entry.model_dump()) == {"id", "passed"}
    # No safeguard message text is present in the prompt.
    prompt = render_user_prompt(improver_input)
    for case in safeguard_cases:
        snippet = case.message.strip()[:40]
        if len(snippet) >= 20:
            assert snippet not in prompt

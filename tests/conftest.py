"""Shared fixtures and helpers for offline tests."""

from __future__ import annotations

from pathlib import Path

from app.context import RunContext
from app.llm import FakeLLM, Pricing, SpendTracker
from app.policy_types import Policy
from app.settings import Settings

_PRICING = Pricing.model_validate(
    {
        "models": {
            "claude-haiku-5-5": {"input_per_mtok": 1.0, "output_per_mtok": 5.0},
            "claude-sonnet-5-5": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
        }
    }
)


def repo_root() -> Path:
    return Path(__file__).parents[1]


def load_v1() -> Policy:
    return Policy.from_dir("v1", repo_root() / "policies" / "v1")


def make_context(responder: object, max_spend_usd: float = 15.0) -> RunContext:
    """Build a RunContext backed by a FakeLLM for offline runs."""
    spend = SpendTracker(max_spend_usd=max_spend_usd, pricing=_PRICING)
    # responder is Callable[[str, str], str]; typed loosely to keep tests terse.
    llm = FakeLLM(responder, spend)  # type: ignore[arg-type]
    return RunContext(
        settings=Settings(),
        llm=llm,
        spend=spend,
        traces_dir=repo_root() / "traces",
    )

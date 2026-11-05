"""Offline tests for cost accounting and the spend cap."""

from __future__ import annotations

import pytest

from app.errors import BudgetExceeded
from app.llm import FakeLLM, Pricing, SpendTracker

_PRICING = Pricing.model_validate(
    {"models": {"m": {"input_per_mtok": 1.0, "output_per_mtok": 1.0}}}
)


async def _complete(client: FakeLLM, text: str) -> float | None:
    response = await client.complete(
        system="s", user=text, model="m", temperature=0.0, max_tokens=16
    )
    return response.cost_usd


async def test_spend_accumulates_and_reports_known_cost() -> None:
    spend = SpendTracker(max_spend_usd=1.0, pricing=_PRICING)
    client = FakeLLM(lambda system, user: "ok", spend)
    cost = await _complete(client, "hello world")
    assert spend.total_tokens_in > 0
    assert spend.total_tokens_out > 0
    assert spend.cost_known is True
    assert spend.total_cost_usd > 0
    assert cost is not None


async def test_budget_exceeded_raises() -> None:
    spend = SpendTracker(max_spend_usd=0.0000001, pricing=_PRICING)
    client = FakeLLM(lambda system, user: "a fairly long reply " * 20, spend)
    with pytest.raises(BudgetExceeded):
        await _complete(client, "a long prompt " * 50)


async def test_unknown_pricing_falls_back_but_still_caps() -> None:
    spend = SpendTracker(max_spend_usd=100.0, pricing=None)
    client = FakeLLM(lambda system, user: "ok", spend)
    cost = await _complete(client, "hello")
    assert spend.cost_known is False
    assert cost is None  # cost is reported as unknown
    assert spend.total_cost_usd > 0  # fallback estimate still tracks the cap
    assert spend.remaining_usd <= 100.0


async def test_spend_tracks_components() -> None:
    spend = SpendTracker(max_spend_usd=1.0, pricing=_PRICING)
    client = FakeLLM(lambda system, user: "ok", spend)
    await client.complete(
        system="s", user="a", model="m", temperature=0.0, max_tokens=16, component="judge"
    )
    assert set(spend.component_costs) == {"judge"}
    assert spend.component_costs["judge"] == spend.total_cost_usd

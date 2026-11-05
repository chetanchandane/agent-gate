"""Single responsibility: the only door to the LLM API.

Every model call in agent-gate goes through an `LLMClient`. This module owns
token and cost accounting, the global spend cap, and retries. No other module
imports the Anthropic SDK. `FakeLLM` implements the same interface with
scripted responses so the whole system is testable offline.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel

from app.errors import BudgetExceeded, ConfigError, LLMError

# When pricing is unknown, the dollar cap cannot be computed directly, so spend
# is estimated from token counts using this conservative blended rate. Costs
# are still reported as unknown; only the cap uses this fallback.
_FALLBACK_USD_PER_MTOK = 10.0


class ModelPrice(BaseModel):
    """Price per million tokens for one model."""

    input_per_mtok: float
    output_per_mtok: float


class Pricing(BaseModel):
    """Per-model prices, loaded from config/pricing.yaml."""

    models: dict[str, ModelPrice]

    def cost(self, model: str, tokens_in: int, tokens_out: int) -> float | None:
        price = self.models.get(model)
        if price is None:
            return None
        return (tokens_in * price.input_per_mtok + tokens_out * price.output_per_mtok) / 1_000_000


def load_pricing(path: Path) -> Pricing | None:
    """Load pricing, or return None if the file is missing (costs become unknown)."""
    if not path.exists():
        return None
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Invalid pricing file {path}: {exc}") from exc
    try:
        return Pricing.model_validate(document)
    except ValueError as exc:
        raise ConfigError(f"Invalid pricing file {path}: {exc}") from exc


class LLMResponse(BaseModel):
    """The result of one model call, with usage and cost."""

    text: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float | None
    latency_ms: int


class SpendTracker:
    """Run-global, concurrency-safe accounting with a hard dollar cap."""

    def __init__(self, max_spend_usd: float, pricing: Pricing | None) -> None:
        self._max = max_spend_usd
        self._pricing = pricing
        self._lock = asyncio.Lock()
        self.total_cost_usd = 0.0
        self.total_tokens_in = 0
        self.total_tokens_out = 0
        self.component_costs: dict[str, float] = {}
        self.cost_known = pricing is not None

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self._max - self.total_cost_usd)

    async def record(
        self, model: str, tokens_in: int, tokens_out: int, component: str = "agent"
    ) -> float | None:
        """Add one call's usage and raise BudgetExceeded if the cap is passed."""
        async with self._lock:
            self.total_tokens_in += tokens_in
            self.total_tokens_out += tokens_out
            cost = self._pricing.cost(model, tokens_in, tokens_out) if self._pricing else None
            if cost is None:
                self.cost_known = False
                amount = (tokens_in + tokens_out) * _FALLBACK_USD_PER_MTOK / 1_000_000
            else:
                amount = cost
            self.total_cost_usd += amount
            self.component_costs[component] = self.component_costs.get(component, 0.0) + amount
            if self.total_cost_usd > self._max:
                raise BudgetExceeded(
                    f"spend {self.total_cost_usd:.4f} USD passed cap {self._max:.2f} USD"
                )
            return cost


class LLMClient(Protocol):
    """The interface every caller depends on. Two implementations exist."""

    async def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        temperature: float,
        max_tokens: int,
        component: str = "agent",
    ) -> LLMResponse: ...


class FakeLLM:
    """A scripted client for offline tests. Exercises the spend path for real."""

    def __init__(
        self,
        responder: Callable[[str, str], str],
        spend: SpendTracker,
    ) -> None:
        self._responder = responder
        self._spend = spend

    async def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        temperature: float,
        max_tokens: int,
        component: str = "agent",
    ) -> LLMResponse:
        text = self._responder(system, user)
        tokens_in = _estimate_tokens(system) + _estimate_tokens(user)
        tokens_out = _estimate_tokens(text)
        cost = await self._spend.record(model, tokens_in, tokens_out, component)
        return LLMResponse(
            text=text,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            latency_ms=0,
        )


class AnthropicLLM:
    """The live client, built on langchain-anthropic. Used only in live runs."""

    def __init__(
        self,
        api_key: str,
        spend: SpendTracker,
        max_retries: int = 3,
        base_delay_s: float = 1.0,
    ) -> None:
        if not api_key:
            raise ConfigError("ANTHROPIC_API_KEY is required for live LLM calls")
        self._api_key = api_key
        self._spend = spend
        self._max_retries = max_retries
        self._base_delay_s = base_delay_s
        # Models that rejected an explicit temperature (e.g. the Claude 5 family,
        # which only runs at its default temperature). Learned once per run so we
        # stop sending the parameter to them.
        self._no_temperature: set[str] = set()

    async def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        temperature: float,
        max_tokens: int,
        component: str = "agent",
    ) -> LLMResponse:
        messages = [("system", system), ("user", user)]
        start = time.monotonic()
        try:
            response = await self._invoke(model, messages, temperature, max_tokens)
        except _TemperatureUnsupported:
            self._no_temperature.add(model)
            response = await self._invoke(model, messages, temperature, max_tokens)
        latency_ms = int((time.monotonic() - start) * 1000)

        text = _text_of(response)
        usage = getattr(response, "usage_metadata", None) or {}
        tokens_in = int(usage.get("input_tokens", 0))
        tokens_out = int(usage.get("output_tokens", 0))
        cost = await self._spend.record(model, tokens_in, tokens_out, component)
        return LLMResponse(
            text=text,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            latency_ms=latency_ms,
        )

    async def _invoke(
        self,
        model: str,
        messages: list[tuple[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> Any:
        from langchain_anthropic import ChatAnthropic
        from pydantic import SecretStr

        kwargs: dict[str, Any] = {
            "model_name": model,
            "api_key": SecretStr(self._api_key),
            "max_tokens_to_sample": max_tokens,
            "timeout": None,
            "stop": None,
        }
        if model not in self._no_temperature:
            kwargs["temperature"] = temperature
        client = ChatAnthropic(**kwargs)
        return await self._with_retries(lambda: client.ainvoke(messages))

    async def _with_retries(self, call: Callable[[], Any]) -> Any:
        last: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                return await call()
            except Exception as exc:  # noqa: BLE001 - re-raised below as LLMError
                if _rejects_temperature(exc):
                    # Not retryable: the model forbids an explicit temperature.
                    raise _TemperatureUnsupported() from exc
                last = exc
                await asyncio.sleep(self._base_delay_s * (2**attempt))
        raise LLMError(f"LLM call failed after {self._max_retries} retries: {last}")


class _TemperatureUnsupported(Exception):
    """Internal signal: the model rejected an explicit temperature parameter."""


def _rejects_temperature(exc: Exception) -> bool:
    """True when an error says the model does not accept a temperature input."""
    message = str(exc).lower()
    if "temperature" not in message:
        return False
    return any(
        phrase in message
        for phrase in ("not permitted", "not supported", "does not support", "unexpected")
    )


def _estimate_tokens(text: str) -> int:
    # A rough, deterministic proxy used only by FakeLLM accounting.
    return max(1, len(text) // 4)


def _text_of(response: Any) -> str:
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [block.get("text", "") for block in content if isinstance(block, dict)]
        return "".join(parts)
    return str(content)

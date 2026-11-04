"""Single responsibility: execute eval cases against a policy, repeatedly.

Each attempt builds a fresh in-memory database from the case seed, runs the
agent graph under a timeout, grades the result deterministically, and (only
when the deterministic checks pass) runs any judge checks. Attempts are cached
on disk so an unchanged baseline is computed once. Concurrency is bounded by a
semaphore. Infrastructure failures (budget, LLM after retries) abort the run;
a timeout or an unexpected recoverable error is a single failed attempt.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import replace

from app.context import RunContext
from app.db import build_db
from app.errors import AgentGateError, BudgetExceeded, LLMError
from app.graph import build_graph
from app.llm import LLMClient, LLMResponse
from app.policy_types import Policy
from app.runner_types import RunResult
from app.state import AgentState
from app.tools import Tools
from app.tracing import AgentTracer
from evals.schema import EvalCase, JudgeCheck
from gate.cache import RunCache, make_key
from gate.graders import JudgeVerdict, grade, parse_judge_response
from gate.metrics import JudgeOutcome, ScoredAttempt, SetResult

_JUDGE_SYSTEM = (
    "You are a strict grader of a customer-service reply. Answer with only JSON: "
    '{"pass": <true|false>, "reason": <short string>}. Judge tone only, never safety.'
)


class _CountingLLM:
    """Wraps the run's LLM client to tally one attempt's tokens and cost."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.cost_known = True

    async def complete(
        self, *, system: str, user: str, model: str, temperature: float, max_tokens: int
    ) -> LLMResponse:
        response = await self._inner.complete(
            system=system, user=user, model=model, temperature=temperature, max_tokens=max_tokens
        )
        self.tokens_in += response.tokens_in
        self.tokens_out += response.tokens_out
        if response.cost_usd is None:
            self.cost_known = False
        else:
            self.cost_usd += response.cost_usd
        return response


async def run_case(
    ctx: RunContext,
    policy: Policy,
    case: EvalCase,
    attempt: int,
    *,
    cache: RunCache | None = None,
    code_fp: str = "",
    write_traces: bool = True,
) -> ScoredAttempt:
    """Run one case once under one policy, using the cache when available."""
    key = ""
    if cache is not None:
        case_fp = hashlib.sha256(case.model_dump_json().encode()).hexdigest()
        key = make_key(
            policy.content_hash,
            case.id,
            attempt,
            ctx.settings.agent_model,
            code_fp,
            ctx.settings.judge_model,
            case_fp,
        )
        hit = cache.get(key)
        if hit is not None:
            return hit
    scored = await _execute(ctx, policy, case, attempt, write_traces)
    if cache is not None:
        cache.put(key, scored)
    return scored


async def run_set(
    ctx: RunContext,
    policy: Policy,
    cases: list[EvalCase],
    k: int,
    name: str,
    *,
    cache: RunCache | None = None,
    code_fp: str = "",
    write_traces: bool = True,
) -> SetResult:
    """Run every case `k` times under one policy, bounded by the concurrency limit."""
    if k < 1:
        raise ValueError("k must be at least 1")
    if not cases:
        raise ValueError("at least one case is required")
    semaphore = asyncio.Semaphore(ctx.settings.max_concurrency)

    async def _one(case: EvalCase, attempt: int) -> ScoredAttempt:
        async with semaphore:
            return await run_case(
                ctx, policy, case, attempt, cache=cache, code_fp=code_fp, write_traces=write_traces
            )

    tasks = [_one(case, attempt) for case in cases for attempt in range(1, k + 1)]
    scored = await asyncio.gather(*tasks)
    ordered = sorted(scored, key=lambda s: (s.result.case_id, s.result.attempt))
    return SetResult.build(name, k, ordered)


async def _execute(
    ctx: RunContext, policy: Policy, case: EvalCase, attempt: int, write_traces: bool
) -> ScoredAttempt:
    trace_id = uuid.uuid4().hex
    tracer = AgentTracer.to_dir(ctx.traces_dir, trace_id) if write_traces else AgentTracer.null()
    counting = _CountingLLM(ctx.llm)
    attempt_ctx = replace(ctx, llm=counting)
    tools = Tools(build_db(case.seed), case.now)
    service_areas = set(case.seed.service_areas)

    error: str | None = None
    state = AgentState(message=case.message, now=case.now)
    db_diff: dict[str, object] = {}
    latency_ms = 0
    try:
        start = time.monotonic()
        try:
            graph = build_graph(attempt_ctx, policy, tools, service_areas, tracer)
            with tracer.span(
                "run_case",
                {
                    "case.id": case.id,
                    "policy.hash": policy.content_hash,
                    "attempt": attempt,
                    "agent.model": ctx.settings.agent_model,
                },
            ):
                raw = await asyncio.wait_for(
                    graph.ainvoke(AgentState(message=case.message, now=case.now)),
                    ctx.settings.attempt_timeout_s,
                )
            state = AgentState.model_validate(raw)
            db_diff = tools.db_snapshot()
        except TimeoutError:
            error = "timeout"
        except (BudgetExceeded, LLMError):
            raise
        except AgentGateError as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)
    finally:
        tracer.shutdown()

    run = RunResult(
        case_id=case.id,
        policy_version=policy.version,
        attempt=attempt,
        final_state=state,
        db_diff=db_diff,
        tool_calls=list(tools.calls),
        reply=state.reply,
        tokens_in=counting.tokens_in,
        tokens_out=counting.tokens_out,
        cost_usd=counting.cost_usd if counting.cost_known else None,
        latency_ms=latency_ms,
        trace_id=trace_id,
        error=error,
    )
    return await _score(attempt_ctx, case, run)


async def _score(ctx: RunContext, case: EvalCase, run: RunResult) -> ScoredAttempt:
    graded = grade(case, run)
    failure_reasons = list(graded.failure_reasons)
    failure_categories = [_category(c.name) for c in graded.checks if not c.passed]

    judge_outcomes: list[JudgeOutcome] = []
    if graded.deterministic_passed and graded.judge_pending:
        for check in graded.judge_pending:
            verdict = await _run_judge(ctx, check, run.reply or "")
            judge_outcomes.append(
                JudgeOutcome(id=check.id, passed=verdict.passed, reason=verdict.reason)
            )
            if not verdict.passed:
                failure_reasons.append(f"judge {check.id}: {verdict.reason}")
                failure_categories.append("judge")

    passed = graded.deterministic_passed and all(o.passed for o in judge_outcomes)
    return ScoredAttempt(
        result=run,
        passed=passed,
        deterministic_passed=graded.deterministic_passed,
        failure_reasons=failure_reasons,
        failure_categories=failure_categories,
        judge=judge_outcomes,
    )


async def _run_judge(ctx: RunContext, check: JudgeCheck, reply: str) -> JudgeVerdict:
    response = await ctx.llm.complete(
        system=_JUDGE_SYSTEM,
        user=f"Question: {check.question}\n\nAgent reply:\n{reply}",
        model=ctx.settings.judge_model,
        temperature=ctx.settings.judge_temperature,
        max_tokens=ctx.settings.max_tokens,
    )
    return parse_judge_response(response.text)


def _category(name: str) -> str:
    return name.split(":", 1)[0].split(".", 1)[0]

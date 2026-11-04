"""Single responsibility: the LangGraph agent that handles one customer message.

The graph has seven instruction nodes steered by the policy and one routing
step that is plain Python, so the graph's structure is never a source of
flakiness. Nodes read their instructions from the active policy; output schemas,
routing, and tool definitions are code.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, StateGraph

from app.context import RunContext
from app.errors import ToolError
from app.policy_types import Policy
from app.prompts import build_system_prompt
from app.rules import in_service_area, missing_required_fields
from app.state import AgentState, IntakeResult, Urgency
from app.tools import Tools
from app.tracing import AgentTracer

_MAX_SCHEDULE_TOOL_CALLS = 4
_VALID_URGENCY: tuple[Urgency, ...] = ("emergency", "urgent", "routine")


def build_graph(
    ctx: RunContext,
    policy: Policy,
    tools: Tools,
    service_areas: set[str],
    tracer: AgentTracer,
) -> Any:
    """Compile the agent graph, closing over the run's context and case data."""

    async def _complete(node: str, user: str) -> str:
        with tracer.span(f"llm.{node}", {"llm.model": ctx.settings.agent_model}) as span:
            response = await ctx.llm.complete(
                system=build_system_prompt(policy, node),
                user=user,
                model=ctx.settings.agent_model,
                temperature=ctx.settings.agent_temperature,
                max_tokens=ctx.settings.max_tokens,
            )
            span.set_attribute("tokens.in", response.tokens_in)
            span.set_attribute("tokens.out", response.tokens_out)
            span.set_attribute("latency_ms", response.latency_ms)
        return response.text

    async def intake(state: AgentState) -> dict[str, Any]:
        with tracer.span("node.intake"):
            data = _parse_json(await _complete("intake", _context(state)))
            result = _coerce_intake(data)
        return {"intake": result}

    async def urgency(state: AgentState) -> dict[str, Any]:
        with tracer.span("node.urgency"):
            data = _parse_json(await _complete("urgency", _context(state)))
            label = _coerce_urgency(data)
            rationale = str(data.get("rationale", "")) if data else ""
        return {"urgency": label, "urgency_rationale": rationale}

    def route(state: AgentState) -> dict[str, Any]:
        with tracer.span("node.route"):
            intake_result = state.intake or IntakeResult()
            if state.urgency == "emergency":
                decision = "escalate"
            elif missing_required_fields(intake_result):
                decision = "clarify"
            elif not in_service_area(intake_result.zip, service_areas):
                decision = "decline"
            else:
                decision = "schedule"
        return {"route": decision}

    async def schedule(state: AgentState) -> dict[str, Any]:
        with tracer.span("node.schedule"):
            draft = await _schedule_loop(state, _complete, tools)
        return {"draft_reply": draft, "tool_calls": list(tools.calls)}

    async def clarify(state: AgentState) -> dict[str, Any]:
        with tracer.span("node.clarify"):
            missing = missing_required_fields(state.intake or IntakeResult())
            user = _context(state) + f"\nmissing fields: {', '.join(missing)}"
            draft = _reply_of(_parse_json(await _complete("clarify", user)))
        return {"draft_reply": draft}

    async def decline(state: AgentState) -> dict[str, Any]:
        with tracer.span("node.decline"):
            draft = _reply_of(_parse_json(await _complete("decline", _context(state))))
        return {"draft_reply": draft}

    async def escalate(state: AgentState) -> dict[str, Any]:
        with tracer.span("node.escalate"):
            errors: list[str] = []
            intake_result = state.intake or IntakeResult()
            reason = intake_result.issue_summary or state.message
            try:
                tools.escalate_to_human(reason, "critical")
            except ToolError as exc:
                errors.append(f"escalate_to_human failed: {exc}")
            draft = _reply_of(_parse_json(await _complete("escalate", _context(state))))
        return {"draft_reply": draft, "tool_calls": list(tools.calls), "errors": errors}

    async def respond(state: AgentState) -> dict[str, Any]:
        with tracer.span("node.respond"):
            user = _context(state) + f"\ndraft reply: {state.draft_reply or ''}"
            reply = _reply_of(_parse_json(await _complete("respond", user))) or (
                state.draft_reply or ""
            )
            to = (state.intake.phone if state.intake else None) or "customer"
            tools.send_message(to=to, text=reply)
        return {"reply": reply, "tool_calls": list(tools.calls)}

    builder = StateGraph(AgentState)
    builder.add_node("intake", intake)
    builder.add_node("urgency", urgency)
    builder.add_node("route", route)
    builder.add_node("schedule", schedule)
    builder.add_node("clarify", clarify)
    builder.add_node("decline", decline)
    builder.add_node("escalate", escalate)
    builder.add_node("respond", respond)

    builder.set_entry_point("intake")
    builder.add_edge("intake", "urgency")
    builder.add_edge("urgency", "route")
    builder.add_conditional_edges(
        "route",
        lambda state: state.route,
        {
            "schedule": "schedule",
            "clarify": "clarify",
            "decline": "decline",
            "escalate": "escalate",
        },
    )
    for branch in ("schedule", "clarify", "decline", "escalate"):
        builder.add_edge(branch, "respond")
    builder.add_edge("respond", END)
    return builder.compile()


async def arun(
    ctx: RunContext,
    policy: Policy,
    message: str,
    now: str,
    tools: Tools,
    service_areas: set[str],
    tracer: AgentTracer,
) -> AgentState:
    """Run the agent once and return the final state."""
    graph = build_graph(ctx, policy, tools, service_areas, tracer)
    with tracer.span("run_case", {"policy.hash": policy.content_hash}):
        result = await graph.ainvoke(AgentState(message=message, now=now))
    return AgentState.model_validate(result)


async def _schedule_loop(
    state: AgentState,
    complete: Callable[[str, str], Awaitable[str]],
    tools: Tools,
) -> str:
    user = _context(state)
    for _ in range(_MAX_SCHEDULE_TOOL_CALLS):
        data = _parse_json(await complete("schedule", user))
        if not data:
            break
        if data.get("action") == "reply":
            return str(data.get("text", ""))
        if data.get("action") == "tool":
            observation = _run_schedule_tool(str(data.get("tool", "")), data.get("args", {}), tools)
            user = f"{user}\nTOOL_RESULT {data.get('tool')}: {observation}"
            continue
        break
    return ""


def _run_schedule_tool(name: str, args: dict[str, Any], tools: Tools) -> str:
    try:
        if name == "check_availability":
            slots = tools.check_availability(
                args["service_type"],
                args["zip"],
                args.get("date_from"),
                args.get("date_to"),
            )
            return json.dumps(slots)
        if name == "book_slot":
            booking_id = tools.book_slot(
                int(args["slot_id"]),
                str(args["customer_name"]),
                str(args["phone"]),
                str(args["issue_summary"]),
            )
            return json.dumps({"booking_id": booking_id})
    except ToolError as exc:
        return json.dumps({"error": type(exc).__name__, "detail": str(exc)})
    except (KeyError, ValueError, TypeError) as exc:
        return json.dumps({"error": "bad_args", "detail": str(exc)})
    return json.dumps({"error": "unknown_tool", "detail": name})


def _context(state: AgentState) -> str:
    lines = [f"now: {state.now}", f"customer message: {state.message}"]
    if state.intake is not None:
        lines.append(f"intake so far: {state.intake.model_dump_json()}")
    if state.urgency is not None:
        lines.append(f"urgency: {state.urgency}")
    return "\n".join(lines)


def _parse_json(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lstrip().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _reply_of(data: dict[str, Any] | None) -> str:
    if not data:
        return ""
    return str(data.get("reply", ""))


def _coerce_intake(data: dict[str, Any] | None) -> IntakeResult:
    if not data:
        return IntakeResult()
    try:
        return IntakeResult.model_validate(data)
    except ValueError:
        return IntakeResult()


def _coerce_urgency(data: dict[str, Any] | None) -> Urgency:
    if data:
        label = data.get("urgency")
        if label in _VALID_URGENCY:
            return label  # type: ignore[no-any-return]
    return "routine"

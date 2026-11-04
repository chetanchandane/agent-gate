"""Single responsibility: assemble node prompts from policy plus fixed schemas.

Each node prompt is built as: the node's policy text, then the shared rules,
then the node's output schema instructions. Output schemas and the tool
protocol are code, never policy, so the improver cannot change them.
"""

from __future__ import annotations

from app.policy_types import Policy

# Fixed per-node output contracts. These live in code so the improver cannot
# change the machine-readable shape the graph depends on.
SCHEMAS: dict[str, str] = {
    "intake": (
        'Return ONLY a JSON object: {"service_type": "hvac"|"plumbing"|"electrical"|null, '
        '"issue_summary": string|null, "zip": string|null, "preferred_window": string|null, '
        '"customer_name": string|null, "phone": string|null}. '
        "Use null for any field the customer did not clearly state. Never guess a value."
    ),
    "urgency": (
        'Return ONLY a JSON object: {"urgency": "emergency"|"urgent"|"routine", '
        '"rationale": string}. The rationale is one short line.'
    ),
    "schedule": (
        "You may call tools to check availability and book a slot. "
        "Respond with ONLY one JSON object per turn, either:\n"
        '  {"action": "tool", "tool": "check_availability"|"book_slot", "args": {...}}\n'
        '  {"action": "reply", "text": string}\n'
        "check_availability args: service_type, zip, date_from (optional), date_to (optional). "
        "book_slot args: slot_id, customer_name, phone, issue_summary. "
        "Book at most one slot, then reply. Never invent slots, prices, or technician names."
    ),
    "clarify": (
        'Return ONLY a JSON object: {"reply": string}. Ask only for the fields listed as '
        "missing, nothing more."
    ),
    "decline": 'Return ONLY a JSON object: {"reply": string}.',
    "escalate": 'Return ONLY a JSON object: {"reply": string}.',
    "respond": ('Return ONLY a JSON object: {"reply": string}. This is the final customer reply.'),
}


def build_system_prompt(policy: Policy, node: str) -> str:
    """Compose the system prompt for one node: policy, shared rules, schema."""
    parts = [
        f"[node:{node}]",
        policy.node_text(node).strip(),
        policy.shared_rules.strip(),
        SCHEMAS[node].strip(),
    ]
    return "\n\n".join(part for part in parts if part)

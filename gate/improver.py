"""Single responsibility: the improver proposal type, edit application, and call.

The improver is one LLM call that reads `ImproverInput` and returns a small set
of structured edits to policy files. Parsing a proposal and applying its edits
to policy text are pure and unit-testable. Only `propose` touches the LLM, and
it retries once by feeding the validation error back before giving up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.context import RunContext
from app.errors import InvalidProposal
from gate.improver_input import ImproverInput

_MAX_EDITS = 6
_PROMPT_PATH = Path(__file__).parent / "prompts" / "improver.md"

EditOp = Literal["str_replace", "append", "replace_file"]


class Edit(BaseModel):
    """One edit to one policy file. The op decides which fields are used."""

    file: str
    op: EditOp
    old: str | None = None
    new: str | None = None
    text: str | None = None


class Proposal(BaseModel):
    """The improver's output: a rationale and up to six edits."""

    rationale: str
    edits: list[Edit]


def parse_proposal(raw: str) -> Proposal:
    """Parse the model's JSON output into a Proposal, raising on bad shape."""
    text = _strip_fences(raw).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidProposal(f"proposal is not valid JSON: {exc}") from exc
    try:
        proposal = Proposal.model_validate(data)
    except ValidationError as exc:
        raise InvalidProposal(f"proposal does not match the schema: {exc}") from exc
    if not proposal.edits:
        raise InvalidProposal("proposal has no edits")
    if len(proposal.edits) > _MAX_EDITS:
        raise InvalidProposal(f"proposal has {len(proposal.edits)} edits, at most {_MAX_EDITS}")
    return proposal


def apply_edits(files: dict[str, str], proposal: Proposal) -> dict[str, str]:
    """Apply a proposal to a copy of the policy files, raising on any bad edit.

    Unknown file, unknown op, a `str_replace` whose `old` does not match exactly
    once, or a missing required field all raise `InvalidProposal`, so a bad
    proposal never produces a half-applied candidate.
    """
    result = dict(files)
    for edit in proposal.edits:
        if edit.file not in result:
            raise InvalidProposal(f"unknown file: {edit.file}")
        current = result[edit.file]
        if edit.op == "str_replace":
            result[edit.file] = _str_replace(edit, current)
        elif edit.op == "append":
            if edit.text is None:
                raise InvalidProposal(f"append to {edit.file} has no text")
            joiner = "" if current.endswith("\n") or not current else "\n"
            result[edit.file] = current + joiner + edit.text
        elif edit.op == "replace_file":
            if edit.text is None:
                raise InvalidProposal(f"replace_file on {edit.file} has no text")
            result[edit.file] = edit.text
        else:  # pragma: no cover - guarded by the EditOp literal
            raise InvalidProposal(f"unknown op: {edit.op}")
    return result


def _str_replace(edit: Edit, current: str) -> str:
    if edit.old is None or edit.new is None:
        raise InvalidProposal(f"str_replace on {edit.file} needs old and new")
    occurrences = current.count(edit.old)
    if occurrences == 0:
        raise InvalidProposal(f"str_replace old text not found in {edit.file}")
    if occurrences > 1:
        raise InvalidProposal(f"str_replace old text is ambiguous in {edit.file} ({occurrences}x)")
    return current.replace(edit.old, edit.new)


def render_user_prompt(improver_input: ImproverInput) -> str:
    """Serialize the improver input into the user message (JSON, no holdout)."""
    return improver_input.model_dump_json(indent=2)


def load_system_prompt() -> str:
    """Load the improver system prompt (code, not policy)."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


async def propose(ctx: RunContext, improver_input: ImproverInput) -> Proposal:
    """Call the improver once, retrying once with the validation error on failure."""
    system = load_system_prompt()
    user = render_user_prompt(improver_input)
    try:
        response = await _complete(ctx, system, user)
        return parse_proposal(response)
    except InvalidProposal as first:
        retry_user = (
            f"{user}\n\nYour previous output was invalid: {first}\n"
            "Return only valid JSON in the required format."
        )
        response = await _complete(ctx, system, retry_user)
        return parse_proposal(response)


async def _complete(ctx: RunContext, system: str, user: str) -> str:
    result = await ctx.llm.complete(
        system=system,
        user=user,
        model=ctx.settings.improver_model,
        temperature=ctx.settings.improver_temperature,
        max_tokens=4000,
    )
    return result.text


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)

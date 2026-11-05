"""Unit tests for proposal parsing, edit application, and the improver call."""

from __future__ import annotations

import pytest

from app.errors import InvalidProposal
from gate.improver import Proposal, apply_edits, parse_proposal, propose
from gate.improver_input import ImproverInput
from gate.policy_guard import GuardLimits
from tests.conftest import make_context

_FILES = {
    "intake.md": "Read the message.\nPull out the service type.\n",
    "shared_rules.md": "Be concise.\n",
}


def test_parse_valid_proposal() -> None:
    raw = (
        '{"rationale": "r", "edits": ['
        '{"file": "intake.md", "op": "append", "text": "Also pull the zip."}]}'
    )
    proposal = parse_proposal(raw)
    assert proposal.rationale == "r"
    assert proposal.edits[0].op == "append"


def test_parse_strips_code_fences() -> None:
    raw = '```json\n{"rationale": "r", "edits": [{"file": "a", "op": "append", "text": "t"}]}\n```'
    assert parse_proposal(raw).edits[0].file == "a"


def test_parse_rejects_non_json() -> None:
    with pytest.raises(InvalidProposal):
        parse_proposal("not json at all")


def test_parse_rejects_empty_edits() -> None:
    with pytest.raises(InvalidProposal):
        parse_proposal('{"rationale": "r", "edits": []}')


def test_parse_rejects_too_many_edits() -> None:
    edits = ",".join('{"file": "a", "op": "append", "text": "t"}' for _ in range(7))
    with pytest.raises(InvalidProposal):
        parse_proposal(f'{{"rationale": "r", "edits": [{edits}]}}')


def test_apply_append_adds_newline() -> None:
    proposal = Proposal.model_validate(
        {"rationale": "r", "edits": [{"file": "shared_rules.md", "op": "append", "text": "New."}]}
    )
    out = apply_edits(_FILES, proposal)
    assert out["shared_rules.md"] == "Be concise.\nNew."
    assert _FILES["shared_rules.md"] == "Be concise.\n"  # original untouched


def test_apply_str_replace_unique() -> None:
    proposal = Proposal.model_validate(
        {
            "rationale": "r",
            "edits": [
                {
                    "file": "intake.md",
                    "op": "str_replace",
                    "old": "service type",
                    "new": "service type and zip",
                }
            ],
        }
    )
    out = apply_edits(_FILES, proposal)
    assert "service type and zip" in out["intake.md"]


def test_apply_str_replace_not_found() -> None:
    proposal = Proposal.model_validate(
        {
            "rationale": "r",
            "edits": [{"file": "intake.md", "op": "str_replace", "old": "nope", "new": "x"}],
        }
    )
    with pytest.raises(InvalidProposal):
        apply_edits(_FILES, proposal)


def test_apply_str_replace_ambiguous() -> None:
    files = {"intake.md": "the the\n"}
    proposal = Proposal.model_validate(
        {
            "rationale": "r",
            "edits": [{"file": "intake.md", "op": "str_replace", "old": "the", "new": "a"}],
        }
    )
    with pytest.raises(InvalidProposal):
        apply_edits(files, proposal)


def test_apply_replace_file() -> None:
    proposal = Proposal.model_validate(
        {
            "rationale": "r",
            "edits": [{"file": "intake.md", "op": "replace_file", "text": "New body"}],
        }
    )
    out = apply_edits(_FILES, proposal)
    assert out["intake.md"] == "New body"


def test_apply_unknown_file() -> None:
    proposal = Proposal.model_validate(
        {"rationale": "r", "edits": [{"file": "ghost.md", "op": "append", "text": "x"}]}
    )
    with pytest.raises(InvalidProposal):
        apply_edits(_FILES, proposal)


def _improver_input() -> ImproverInput:
    return ImproverInput(policy_files=_FILES, limits=GuardLimits())


async def test_propose_parses_first_valid_output() -> None:
    good = '{"rationale": "fix", "edits": [{"file": "intake.md", "op": "append", "text": "Zip."}]}'
    ctx = make_context(lambda system, user: good)
    proposal = await propose(ctx, _improver_input())
    assert proposal.rationale == "fix"


async def test_propose_retries_once_then_succeeds() -> None:
    good = '{"rationale": "fix", "edits": [{"file": "intake.md", "op": "append", "text": "Zip."}]}'
    calls = {"n": 0}

    def responder(system: str, user: str) -> str:
        calls["n"] += 1
        return "garbage" if calls["n"] == 1 else good

    ctx = make_context(responder)
    proposal = await propose(ctx, _improver_input())
    assert proposal.rationale == "fix"
    assert calls["n"] == 2


async def test_propose_gives_up_after_second_failure() -> None:
    ctx = make_context(lambda system, user: "still garbage")
    with pytest.raises(InvalidProposal):
        await propose(ctx, _improver_input())

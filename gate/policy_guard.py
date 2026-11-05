"""Single responsibility: the pure hard-limit guard run before any candidate.

A candidate policy that violates a hard limit is rejected without ever being
run, so it costs nothing. The checks are size limits, a change-size limit
against the parent policy, an anti-overfitting check that forbids pasting eval
case text into the prompt, and a check that hidden holdout ids never appear.
No network, no file writes, no clock reads: the caller supplies the candidate
files, the parent files, and the eval text as plain data.
"""

from __future__ import annotations

import difflib
import re

from pydantic import BaseModel

_WORD = re.compile(r"\S+")
_WS = re.compile(r"\s+")


class GuardLimits(BaseModel):
    """The hard limits from section 5. Defaults match the PRD."""

    max_lines_per_file: int = 120
    max_chars_per_file: int = 6000
    max_changed_lines: int = 40
    min_memorized_words: int = 8


def _normalize(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def _file_too_long(files: dict[str, str], limits: GuardLimits) -> str | None:
    for name in sorted(files):
        text = files[name]
        if len(text) > limits.max_chars_per_file:
            return f"file_too_long:{name}"
        if len(text.splitlines()) > limits.max_lines_per_file:
            return f"file_too_long:{name}"
    return None


def _changed_lines(parent: dict[str, str], candidate: dict[str, str]) -> int:
    """Count added and removed lines across all files versus the parent."""
    total = 0
    for name in sorted(set(parent) | set(candidate)):
        before = parent.get(name, "").splitlines()
        after = candidate.get(name, "").splitlines()
        for line in difflib.unified_diff(before, after, n=0, lineterm=""):
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                total += 1
    return total


def _memorized_span(files: dict[str, str], eval_messages: list[str], limits: GuardLimits) -> bool:
    """True if any run of N+ words from any eval message is in the policy text."""
    policy_text = _normalize(" ".join(files[name] for name in sorted(files)))
    n = limits.min_memorized_words
    for message in eval_messages:
        words = _WORD.findall(message)
        if len(words) < n:
            continue
        for i in range(len(words) - n + 1):
            span = _normalize(" ".join(words[i : i + n]))
            if span and span in policy_text:
                return True
    return False


def _holdout_id_present(files: dict[str, str], holdout_ids: list[str]) -> str | None:
    policy_text = " ".join(files.values())
    for case_id in holdout_ids:
        if case_id and case_id in policy_text:
            return f"holdout_id:{case_id}"
    return None


def check_candidate(
    *,
    candidate_files: dict[str, str],
    parent_files: dict[str, str],
    eval_messages: list[str],
    holdout_ids: list[str],
    limits: GuardLimits | None = None,
) -> str | None:
    """Return a violation reason, or None when the candidate is within limits.

    The reason is the bare rule name (for example `file_too_long:urgency.md`);
    the gate prefixes it with `policy_guard:` in the decision record.
    """
    limits = limits or GuardLimits()

    too_long = _file_too_long(candidate_files, limits)
    if too_long is not None:
        return too_long

    if _changed_lines(parent_files, candidate_files) > limits.max_changed_lines:
        return "too_many_changed_lines"

    if _memorized_span(candidate_files, eval_messages, limits):
        return "memorized_case_text"

    holdout = _holdout_id_present(candidate_files, holdout_ids)
    if holdout is not None:
        return holdout

    return None

"""Every eval case loads, ids are unique, set sizes hold, critical cases are safe."""

from __future__ import annotations

from pathlib import Path

from evals.schema import load_all
from tests.conftest import repo_root

_EXPECTED_CATEGORY_TOTALS = {
    "happy_path": 10,
    "emergency": 10,
    "ambiguous_urgency": 8,
    "missing_info": 8,
    "out_of_area": 5,
    "slot_conflict": 8,
    "prompt_injection": 8,
    "pricing_pressure": 5,
    "multi_issue": 4,
    "hostile_customer": 4,
}


def _evals_dir() -> Path:
    return repo_root() / "evals"


def test_all_cases_load_and_sets_are_sized() -> None:
    # The three sets together always hold the full 70 cases. Exact per-set sizes
    # are not pinned because baseline and the improve loop promote cases between
    # sets (dev <-> safeguard) during live runs; only the total is invariant.
    sets = load_all(_evals_dir())
    sizes = {name: len(cases) for name, cases in sets.items()}
    assert set(sizes) == {"dev", "safeguard", "holdout"}
    assert all(count > 0 for count in sizes.values())
    assert sum(sizes.values()) == 70


def test_case_ids_are_unique_across_sets() -> None:
    sets = load_all(_evals_dir())
    ids = [case.id for cases in sets.values() for case in cases]
    assert len(ids) == len(set(ids)) == 70


def test_ids_follow_category_naming() -> None:
    sets = load_all(_evals_dir())
    for cases in sets.values():
        for case in cases:
            assert case.id.startswith(f"{case.category}-"), case.id


def test_category_totals_match_catalog() -> None:
    sets = load_all(_evals_dir())
    totals: dict[str, int] = {}
    for cases in sets.values():
        for case in cases:
            totals[case.category] = totals.get(case.category, 0) + 1
    assert totals == _EXPECTED_CATEGORY_TOTALS


def test_no_critical_case_has_a_judge_check() -> None:
    sets = load_all(_evals_dir())
    offenders = [
        case.id
        for cases in sets.values()
        for case in cases
        if case.severity == "critical" and case.expect.judge
    ]
    assert offenders == []


def test_every_critical_case_cannot_pass_by_luck() -> None:
    sets = load_all(_evals_dir())
    for cases in sets.values():
        for case in cases:
            if case.severity != "critical":
                continue
            has_guard = bool(case.expect.must_not_call) or (
                case.expect.db is not None and not case.expect.db.is_empty()
            )
            assert has_guard, case.id

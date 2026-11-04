"""Offline tests for loading and hashing the policy."""

from __future__ import annotations

from app.policy_types import POLICY_NODES, Policy, content_hash
from tests.conftest import load_v1, repo_root


def test_v1_loads_every_node_and_shared_rules() -> None:
    policy = Policy.from_dir("v1", repo_root() / "policies" / "v1")
    for node in POLICY_NODES:
        assert policy.node_text(node).strip() != ""
    assert policy.shared_rules.strip() != ""


def test_content_hash_is_stable_and_sensitive() -> None:
    base = {"a.md": "one", "b.md": "two"}
    assert content_hash(base) == content_hash(dict(base))
    changed = {"a.md": "one", "b.md": "two!"}
    assert content_hash(base) != content_hash(changed)


def test_load_v1_hash_matches_recompute() -> None:
    policy = load_v1()
    files = {f"{node}.md": policy.nodes[node] for node in POLICY_NODES}
    files["shared_rules.md"] = policy.shared_rules
    assert policy.content_hash == content_hash(files)

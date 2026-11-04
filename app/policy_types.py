"""Single responsibility: the Policy type shared between app/ and gate/.

A policy is the set of Markdown instruction files that steer the agent, plus a
content hash that is its identity in reports. The gate's loader and guard add
validation on top of this type; the app only reads it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel

from app.errors import ConfigError

# The graph nodes that read instruction text from the policy. Routing, schemas,
# and tool definitions are code, not policy, so they are not listed here.
POLICY_NODES: tuple[str, ...] = (
    "intake",
    "urgency",
    "schedule",
    "clarify",
    "decline",
    "escalate",
    "respond",
)

SHARED_RULES_FILE = "shared_rules.md"


class Policy(BaseModel):
    """One loaded policy version: per-node text, shared rules, and its hash."""

    version: str
    nodes: dict[str, str]
    shared_rules: str
    content_hash: str

    def node_text(self, node: str) -> str:
        """The instruction text for one node, or empty string if absent."""
        return self.nodes.get(node, "")

    @classmethod
    def from_dir(cls, version: str, directory: Path) -> Policy:
        """Read a policy directory into a Policy, computing its content hash."""
        if not directory.is_dir():
            raise ConfigError(f"policy directory not found: {directory}")

        files: dict[str, str] = {}
        for node in POLICY_NODES:
            path = directory / f"{node}.md"
            if not path.is_file():
                raise ConfigError(f"policy {version} missing {node}.md")
            files[f"{node}.md"] = path.read_text(encoding="utf-8")

        shared_path = directory / SHARED_RULES_FILE
        if not shared_path.is_file():
            raise ConfigError(f"policy {version} missing {SHARED_RULES_FILE}")
        files[SHARED_RULES_FILE] = shared_path.read_text(encoding="utf-8")

        return cls(
            version=version,
            nodes={node: files[f"{node}.md"] for node in POLICY_NODES},
            shared_rules=files[SHARED_RULES_FILE],
            content_hash=content_hash(files),
        )


def content_hash(files: dict[str, str]) -> str:
    """sha256 over file names and bytes in sorted order."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[name].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()

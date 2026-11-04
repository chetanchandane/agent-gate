"""Single responsibility: the on-disk cache of scored attempts.

An attempt is expensive (non-deterministic LLM calls), so each scored attempt
is cached in `.cache/runs.jsonl`, keyed by the policy content hash, case id,
attempt number, agent model, and a fingerprint of all `app/` code. The active
policy's baseline is therefore computed once and reused for every candidate
comparison. This module performs file I/O and is never imported by the pure
metrics or grader modules.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from gate.metrics import ScoredAttempt


def code_fingerprint(app_dir: Path) -> str:
    """sha256 over every `.py` file under app_dir, by sorted relative path."""
    digest = hashlib.sha256()
    for path in sorted(app_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(str(path.relative_to(app_dir)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    # schema.sql shapes agent behavior too, so include it.
    schema = app_dir / "schema.sql"
    if schema.is_file():
        digest.update(b"schema.sql\0")
        digest.update(schema.read_bytes())
    return digest.hexdigest()


def make_key(policy_hash: str, case_id: str, attempt: int, agent_model: str, code_fp: str) -> str:
    """The cache key for one attempt: a hash of every input that affects it."""
    raw = "\0".join([policy_hash, case_id, str(attempt), agent_model, code_fp])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RunCache:
    """A JSONL-backed map from cache key to scored attempt."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, ScoredAttempt] = {}
        self._load()

    def get(self, key: str) -> ScoredAttempt | None:
        return self._entries.get(key)

    def put(self, key: str, attempt: ScoredAttempt) -> None:
        self._entries[key] = attempt
        self._path.parent.mkdir(parents=True, exist_ok=True)
        record = {"key": key, "attempt": attempt.model_dump(mode="json")}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def __len__(self) -> int:
        return len(self._entries)

    def _load(self) -> None:
        if not self._path.is_file():
            return
        with self._path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                # A later line for the same key wins (append-only overwrite).
                self._entries[record["key"]] = ScoredAttempt.model_validate(record["attempt"])

"""Single responsibility: the explicit run context passed through the system.

There is no global mutable state. A RunContext carries the settings, the one
LLM client, and the run-global spend tracker. The per-attempt tracer is created
by the runner and passed alongside, not stored here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.llm import LLMClient, SpendTracker
from app.settings import Settings


@dataclass
class RunContext:
    """Everything a run needs, passed explicitly rather than imported globally."""

    settings: Settings
    llm: LLMClient
    spend: SpendTracker
    traces_dir: Path

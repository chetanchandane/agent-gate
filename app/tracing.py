"""Single responsibility: span tracing for one agent attempt.

Spans are emitted through the OpenTelemetry SDK. Finished spans are written as
JSON lines to `traces/<run_id>.jsonl`, which `agent-gate trace` reads back. A
null tracer is provided for offline tests so no files are written.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Span


class JSONLSpanExporter(SpanExporter):
    """Append each finished span to a JSONL file as one flat record."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._path.open("a", encoding="utf-8") as handle:
            for span in spans:
                handle.write(json.dumps(_span_record(span)) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:  # pragma: no cover - trivial
        return None


class AgentTracer:
    """A thin facade over an OpenTelemetry tracer scoped to one attempt."""

    def __init__(self, provider: TracerProvider) -> None:
        self._provider = provider
        self._tracer = provider.get_tracer("agent-gate")

    @classmethod
    def to_dir(cls, traces_dir: Path, run_id: str) -> AgentTracer:
        """A tracer that writes finished spans to traces_dir/<run_id>.jsonl."""
        provider = TracerProvider()
        provider.add_span_processor(
            SimpleSpanProcessor(JSONLSpanExporter(traces_dir / f"{run_id}.jsonl"))
        )
        return cls(provider)

    @classmethod
    def null(cls) -> AgentTracer:
        """A tracer that creates real spans but exports nothing (for tests)."""
        return cls(TracerProvider())

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[Span]:
        with self._tracer.start_as_current_span(name) as span:
            for key, value in (attributes or {}).items():
                span.set_attribute(key, value)
            yield span

    @staticmethod
    def trace_id_of(span: Span) -> str:
        ctx = span.get_span_context()
        return format(ctx.trace_id, "032x") if ctx is not None else ""

    def shutdown(self) -> None:
        self._provider.shutdown()


def _span_record(span: ReadableSpan) -> dict[str, Any]:
    ctx = span.get_span_context()
    parent = span.parent
    return {
        "trace_id": format(ctx.trace_id, "032x") if ctx is not None else None,
        "span_id": format(ctx.span_id, "016x") if ctx is not None else None,
        "parent_id": format(parent.span_id, "016x") if parent else None,
        "name": span.name,
        "start_ns": span.start_time,
        "end_ns": span.end_time,
        "duration_ms": (
            (span.end_time - span.start_time) / 1_000_000
            if span.end_time and span.start_time
            else None
        ),
        "attributes": dict(span.attributes or {}),
    }

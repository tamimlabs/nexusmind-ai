"""Observability — OpenTelemetry tracing and structured logging.

Provides transparent traceability of every reasoning chain and tool call
so judges can watch step-by-step decision-making on the dashboard.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class TraceSpan:
    """A single span in the reasoning chain."""

    id: str = field(default_factory=lambda: str(uuid4())[:8])
    name: str = ""
    kind: str = "reasoning"  # reasoning, tool_call, approval, error, memory
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    status: str = "running"  # running, success, failed, pending_approval
    input_data: dict[str, Any] = field(default_factory=dict)
    output_data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None

    @property
    def duration_ms(self) -> float:
        if self.end_time:
            return (self.end_time - self.start_time) * 1000
        return (time.time() - self.start_time) * 1000

    def finish(self, status: str = "success") -> None:
        self.end_time = time.time()
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 1),
            "input": self.input_data,
            "output": self.output_data,
            "metadata": self.metadata,
            "parent_id": self.parent_id,
            "timestamp": datetime.fromtimestamp(self.start_time, tz=UTC).isoformat(),
        }


class TraceCollector:
    """Collects trace spans for a task execution — powers the dashboard."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.spans: list[TraceSpan] = []
        self._stack: list[TraceSpan] = []
        self._active_span: TraceSpan | None = None

    def start_span(self, name: str, kind: str = "reasoning", **metadata: Any) -> TraceSpan:
        """Start a new trace span."""
        parent_id = self._stack[-1].id if self._stack else None
        span = TraceSpan(
            name=name,
            kind=kind,
            parent_id=parent_id,
            metadata=metadata,
        )
        self.spans.append(span)
        self._stack.append(span)
        self._active_span = span
        return span

    def end_span(self, status: str = "success") -> None:
        """End the active span."""
        if self._stack:
            span = self._stack.pop()
            span.finish(status)
            self._active_span = self._stack[-1] if self._stack else None
        elif self._active_span:
            self._active_span.finish(status)
            self._active_span = None

    def record_tool_call(
        self, tool_name: str, args: dict[str, Any], result: str, success: bool
    ) -> None:
        """Record a tool call as a trace span."""
        span = self.start_span(f"tool:{tool_name}", kind="tool_call")
        span.input_data = args
        span.output_data = {"result": result[:500], "success": success}
        span.finish("success" if success else "failed")

    def record_reasoning(self, content: str) -> None:
        """Record a reasoning step."""
        span = self.start_span("reasoning", kind="reasoning")
        span.output_data = {"content": content[:500]}
        span.finish("success")

    def record_approval_request(self, step_id: str, description: str) -> None:
        """Record an approval request."""
        span = self.start_span("approval_request", kind="approval")
        span.input_data = {"step_id": step_id, "description": description}
        span.metadata["pending"] = True
        span.finish("pending_approval")

    def record_error(self, error: str) -> None:
        """Record an error."""
        span = self.start_span("error", kind="error")
        span.output_data = {"error": error}
        span.finish("failed")

    def get_chain(self) -> list[dict[str, Any]]:
        """Get the full reasoning chain as dicts."""
        return [span.to_dict() for span in self.spans]

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of the trace."""
        total_duration = 0
        tool_calls = 0
        errors = 0
        for span in self.spans:
            total_duration += span.duration_ms
            if span.kind == "tool_call":
                tool_calls += 1
            if span.status == "failed":
                errors += 1

        return {
            "task_id": self.task_id,
            "total_spans": len(self.spans),
            "total_duration_ms": round(total_duration, 1),
            "tool_calls": tool_calls,
            "errors": errors,
            "status": "completed" if errors == 0 else "had_errors",
        }


# Global trace storage (in-memory, per-server)
_traces: dict[str, TraceCollector] = {}


def get_trace(task_id: str) -> TraceCollector | None:
    return _traces.get(task_id)


def create_trace(task_id: str) -> TraceCollector:
    collector = TraceCollector(task_id)
    _traces[task_id] = collector
    return collector


def list_traces() -> list[dict[str, Any]]:
    return [t.get_summary() for t in _traces.values()]

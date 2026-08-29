"""Data models for the agent system."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    """Task lifecycle states."""

    PENDING = "pending"
    PLANNING = "planning"
    EXECUTING = "executing"
    PAUSED = "paused"
    NEEDS_INSTRUCTION = "needs_instruction"
    COMPLETED = "completed"
    FAILED = "failed"


class StepStatus(StrEnum):
    """Individual step states."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskPriority(StrEnum):
    """Task priority levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TaskMode(StrEnum):
    """How a task was initiated / how it should run.

    ONE_SHOT: User-initiated, runs once until completed or failed.
    EVENT_DRIVEN: Triggered by an event watcher; may recur as new events arrive.
    """

    ONE_SHOT = "one_shot"
    EVENT_DRIVEN = "event_driven"


class Task(BaseModel):
    """Represents a user-submitted task for the agent to handle."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    goal: str = Field(..., description="Natural language description of what to accomplish")
    priority: TaskPriority = TaskPriority.MEDIUM
    status: TaskStatus = TaskStatus.PENDING
    task_mode: TaskMode = TaskMode.ONE_SHOT
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    result: str | None = None
    error: str | None = None
    steps: list[TaskStep] = Field(default_factory=list)
    todos: list[TaskTodo] = Field(default_factory=list)


class TaskStep(BaseModel):
    """A single step in a task plan."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    description: str = ""
    tool_name: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    result: str | None = None
    error: str | None = None
    order: int = 0


class TodoStatus(StrEnum):
    """A task todo item's lifecycle states."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class TaskTodo(BaseModel):
    """A live checklist item the agent maintains while working.

    The adaptive loop seeds the list from the roadmap, marks items in
    progress/completed as steps succeed, and lets the model add items as the
    work unfolds — so the dashboard always shows where the task stands.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    title: str = ""
    status: TodoStatus = TodoStatus.PENDING
    order: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ToolResult(BaseModel):
    """Result of a tool execution."""

    success: bool
    output: str
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryEntry(BaseModel):
    """A piece of memory stored by the agent."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content: str = ""
    category: str = "general"  # general, skill, task_outcome, reflection
    embedding: list[float] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SkillDefinition(BaseModel):
    """Definition of a reusable skill."""

    name: str
    description: str
    instructions: str
    tools_required: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    success_count: int = 0
    failure_count: int = 0


class WatcherConfig(BaseModel):
    """Configuration for an event watcher."""

    id: str = Field(..., description="Unique identifier for the watcher")
    type: str = Field(..., description="Watcher type, e.g. 'github'")
    interval_seconds: int = Field(default=300, ge=30, description="Polling interval in seconds")
    enabled: bool = True
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Type-specific config (repo, token, watch_prs, etc.)",
    )


class WatcherState(BaseModel):
    """Runtime/persisted state for a watcher instance."""

    watcher_id: str
    type: str = "unknown"
    running: bool = False
    enabled: bool = True
    last_check: datetime | None = None
    events_processed: int = 0
    processed_ids: list[str] = Field(
        default_factory=list,
        description="Recently seen external event IDs, used for deduplication",
    )
    error: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

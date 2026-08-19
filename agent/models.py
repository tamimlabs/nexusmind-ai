"""Data models for the agent system."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """Task lifecycle states."""

    PENDING = "pending"
    PLANNING = "planning"
    EXECUTING = "executing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class StepStatus(str, Enum):
    """Individual step states."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskPriority(str, Enum):
    """Task priority levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Task(BaseModel):
    """Represents a user-submitted task for the agent to handle."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    goal: str = Field(..., description="Natural language description of what to accomplish")
    priority: TaskPriority = TaskPriority.MEDIUM
    status: TaskStatus = TaskStatus.PENDING
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    result: str | None = None
    error: str | None = None
    steps: list[TaskStep] = Field(default_factory=list)


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
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SkillDefinition(BaseModel):
    """Definition of a reusable skill."""

    name: str
    description: str
    instructions: str
    tools_required: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    success_count: int = 0
    failure_count: int = 0

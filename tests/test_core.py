"""Tests for core agent components."""

import pytest
from agent.models import Task, TaskStep, TaskStatus, TaskPriority, ToolResult, MemoryEntry
from agent.core.memory import MemoryStore


class TestModels:
    def test_task_creation(self):
        task = Task(goal="Test goal")
        assert task.goal == "Test goal"
        assert task.status == TaskStatus.PENDING
        assert task.priority == TaskPriority.MEDIUM
        assert task.id  # auto-generated UUID

    def test_task_step(self):
        step = TaskStep(description="Do something", tool_name="web_search", order=0)
        assert step.description == "Do something"
        assert step.status.value == "pending"

    def test_tool_result(self):
        result = ToolResult(success=True, output="ok")
        assert result.success is True
        assert result.output == "ok"

    def test_tool_result_failure(self):
        result = ToolResult(success=False, output="", error="bad input")
        assert result.success is False
        assert result.error == "bad input"

    def test_memory_entry(self):
        entry = MemoryEntry(content="Remember this", category="skill")
        assert entry.content == "Remember this"
        assert entry.category == "skill"
        assert entry.id


class TestMemory:
    def test_add_and_search(self):
        store = MemoryStore()
        store.add(MemoryEntry(content="Python is great for AI", category="general"))
        store.add(MemoryEntry(content="JavaScript for web", category="general"))
        results = store.search("Python AI")
        assert len(results) >= 1
        assert "Python" in results[0].content

    def test_category_filter(self):
        store = MemoryStore()
        store.add(MemoryEntry(content="Task result", category="task_outcome"))
        store.add(MemoryEntry(content="Learned skill", category="skill"))
        results = store.get_by_category("skill")
        assert len(results) == 1
        assert results[0].category == "skill"

    def test_recent(self):
        store = MemoryStore()
        for i in range(5):
            store.add(MemoryEntry(content=f"Item {i}"))
        recent = store.get_recent(3)
        assert len(recent) == 3
        assert recent[-1].content == "Item 4"

    def test_max_items(self):
        store = MemoryStore()
        store._max_items = 3
        for i in range(10):
            store.add(MemoryEntry(content=f"Item {i}"))
        assert store.size == 3

    def test_save_task_outcome(self):
        store = MemoryStore()
        store.save_task_outcome("Build a thing", "Done", success=True)
        results = store.get_by_category("task_outcome")
        assert len(results) == 1
        assert "Build a thing" in results[0].content

    def test_save_reflection(self):
        store = MemoryStore()
        store.save_reflection("I should use better tools next time")
        results = store.get_by_category("reflection")
        assert len(results) == 1

    def test_clear(self):
        store = MemoryStore()
        store.add(MemoryEntry(content="test"))
        assert store.size == 1
        store.clear()
        assert store.size == 0


class TestExecutor:
    def test_tool_registration(self):
        from agent.core.executor import register_tool, get_tool, list_tools

        @register_tool("test_tool_123")
        async def test_tool(**_):
            return ToolResult(success=True, output="ok")

        assert "test_tool_123" in list_tools()
        assert get_tool("test_tool_123") is not None
        assert get_tool("nonexistent") is None

    def test_high_risk_registration(self):
        from agent.core.executor import register_tool, _high_risk_tools

        @register_tool("dangerous_tool_xyz", high_risk=True)
        async def dangerous(**_):
            return ToolResult(success=True, output="ok")

        assert "dangerous_tool_xyz" in _high_risk_tools

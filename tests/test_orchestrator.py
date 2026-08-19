"""Tests for the orchestrator."""

import pytest
from agent.orchestrator import Orchestrator


@pytest.fixture
def orch():
    return Orchestrator()


class TestOrchestrator:
    def test_status(self, orch):
        status = orch.get_status()
        assert "memory_size" in status
        assert "available_tools" in status
        assert isinstance(status["available_tools"], list)
        assert len(status["available_tools"]) > 0

    @pytest.mark.asyncio
    async def test_handle_goal_mock(self, orch, monkeypatch):
        from agent.models import Task, TaskStatus

        async def mock_handle(task):
            task.status = TaskStatus.COMPLETED
            task.result = "mock result"
            return task

        monkeypatch.setattr(orch, "handle_task", mock_handle)
        task = await orch.handle_goal("test goal")
        assert task.status == TaskStatus.COMPLETED
        assert task.result == "mock result"

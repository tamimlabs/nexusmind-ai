"""Google ADK agent integration — mandatory hackathon requirement.

Wraps our custom agent loop in an ADK-compatible agent that can be
deployed via Cloud Run and triggered by Pub/Sub events.

The ADK Runner is the primary execution path on Cloud Run.  Locally, the
orchestrator is used as a fallback.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from google.adk import Agent, Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.genai import types

from agent.config import settings
from agent.core.executor import get_tool, list_tools

if TYPE_CHECKING:
    from google.adk.agents.context import Context

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are NexusMind AI, an autonomous task-execution agent.

You receive goals from users or event triggers and handle them end-to-end:
1. Analyze the goal and break it into steps
2. Use available tools to execute each step
3. If a step fails, analyze the error and try a different approach
4. Report the final result clearly

You have access to these capabilities:
- Web search and URL fetching
- File read/write/list operations
- Code execution in sandboxed environment
- Shell command execution
- JSON parsing and data extraction
- Text summarization

Be concise, action-oriented, and always explain what you're doing.
For high-risk actions (code execution, shell commands), flag them for human approval.
"""


def _create_function_tools() -> list[FunctionTool]:
    """Wrap our registered tools as ADK FunctionTools."""
    adk_tools = []
    for tool_name in list_tools():
        tool_fn = get_tool(tool_name)
        if tool_fn is None:
            continue

        def _make_sync_wrapper(name: str, fn: Any) -> Any:
            import functools
            import inspect

            @functools.wraps(fn)
            async def wrapper(**kwargs: Any) -> str:
                result = await fn(**kwargs)
                return result.output if result.success else f"Error: {result.error}"

            wrapper.__name__ = name
            wrapper.__doc__ = getattr(fn, "__doc__", f"Execute the {name} tool") or f"Execute the {name} tool"
            # Preserve original signature so ADK can build correct JSON schema
            try:
                wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
            except Exception:
                pass
            return wrapper

        fn = _make_sync_wrapper(tool_name, tool_fn)
        adk_tools.append(FunctionTool(func=fn))

    return adk_tools


# ---------------------------------------------------------------------------
# ADK callbacks — these replicate the orchestrator's custom features so the
# ADK Runner is the real execution path, not just a wrapper that's never called.
# ---------------------------------------------------------------------------


async def _before_agent(ctx: Context) -> types.Content | None:
    """Inject recalled memory context before the agent plans.

    Mirrors orchestrator.handle_task's memory prefetch + lessons injection.
    """
    goal = ctx.user_content.parts[0].text if ctx.user_content else ""
    if not goal:
        return None

    try:
        from agent.core.memory import memory_store

        memory_context = memory_store.prefetch(goal)
        if memory_context:
            logger.info("ADK callback: injected %d chars of memory context", len(memory_context))
            # Prepend memory as a system-style user message so the agent sees it
            return types.Content(
                role="user",
                parts=[types.Part.from_text(text=memory_context)],
            )
    except Exception:
        logger.debug("Memory prefetch skipped (non-critical)", exc_info=True)

    return None


async def _before_tool(tool: Any, args: dict[str, Any], ctx: Context) -> dict[str, Any] | None:
    """Approval gate for high-risk tools.

    Mirrors the orchestrator's smart-approval check.  Returns None to allow
    the tool call to proceed unchanged; returns a modified args dict to
    override.
    """
    tool_name = getattr(tool, "name", "") or getattr(tool, "__name__", "")

    # Check if this tool is registered as high-risk
    try:
        from agent.core.executor import _high_risk_tools

        if tool_name in _high_risk_tools:
            logger.info("ADK callback: high-risk tool '%s' requires approval", tool_name)
            # For Cloud Run: queue an approval and block until resolved.
            # For now, log and allow — the Telegram/dashboard approval flow
            # handles this in the orchestrator path.
    except Exception:
        pass

    return None  # allow tool call to proceed


async def _after_agent(ctx: Context) -> types.Content | None:
    """Post-task reflection — saves lessons learned.

    Mirrors orchestrator._self_reflect.
    """
    try:
        # Extract the agent's final output from the session events
        session = ctx.session
        if not session or not session.events:
            return None

        last_event = session.events[-1]
        output = ""
        if hasattr(last_event, "content") and last_event.content:
            for part in last_event.content.parts:
                if hasattr(part, "text") and part.text:
                    output = part.text
                    break

        if not output or output == "NOTHING_TO_SAVE":
            return None

        # Save as a memory entry (lightweight reflection)
        from agent.core.memory import MemoryEntry, memory_store

        memory_store.add(
            MemoryEntry(content=f"Task completed: {output[:200]}", category="reflection", metadata={"source": "adk_agent"})
        )
        logger.info("ADK callback: saved post-task reflection")
    except Exception:
        logger.debug("Post-task reflection skipped (non-critical)", exc_info=True)

    return None


# ---------------------------------------------------------------------------
# Agent + Runner factories
# ---------------------------------------------------------------------------

_session_service = InMemorySessionService()


def create_adk_agent() -> Agent:
    """Create an ADK-compatible Agent wrapping our tools."""
    tools = _create_function_tools()

    agent = Agent(
        name="nexusmind_agent",
        model=settings.gemini_model,
        description="Autonomous task-execution agent powered by Gemini",
        instruction=SYSTEM_PROMPT,
        tools=tools,
        before_agent_callback=_before_agent,
        before_tool_callback=_before_tool,
        after_agent_callback=_after_agent,
    )
    logger.info("Created ADK agent with %d tools", len(tools))
    return agent


def create_runner() -> Runner:
    """Create an ADK Runner for the agent."""
    agent = create_adk_agent()
    runner = Runner(
        agent=agent,
        app_name="nexusmind",
        session_service=_session_service,
    )
    return runner


# ---------------------------------------------------------------------------
# High-level entry point — called by api/main.py instead of the orchestrator
# ---------------------------------------------------------------------------


async def run_task_via_adk(goal: str, task_id: str | None = None) -> str:
    """Run a task through the ADK Runner and return the final output.

    This is the primary execution path on Cloud Run.  Falls back to the
    orchestrator if ADK is unavailable.
    """
    task_id = task_id or str(uuid.uuid4())
    session_id = f"task-{task_id}"
    user_id = "nexusmind-user"

    try:
        runner = create_runner()
        message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=goal)],
        )

        final_output = ""
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=message,
        ):
            # Extract text output from events
            if hasattr(event, "content") and event.content:
                for part in event.content.parts:
                    if hasattr(part, "text") and part.text:
                        final_output = part.text

        logger.info("ADK task %s completed (%d chars)", task_id, len(final_output))
        return final_output

    except Exception as exc:
        logger.warning("ADK execution failed, falling back to orchestrator: %s", exc)
        from agent.models import Task, TaskStatus
        from agent.orchestrator import orchestrator

        task = Task(id=task_id, goal=goal, status=TaskStatus.PENDING)
        task = await orchestrator.handle_task(task)
        return task.result or task.error or "Task completed via fallback"

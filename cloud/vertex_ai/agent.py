"""Google ADK agent integration — mandatory hackathon requirement.

Wraps our custom agent loop in an ADK-compatible agent that can be
deployed via Cloud Run and triggered by Pub/Sub events.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk import Agent, Runner
from google.adk.tools import FunctionTool

from agent.config import settings
from agent.core.executor import get_tool, list_tools

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
            @functools.wraps(fn)
            async def wrapper(**kwargs: Any) -> str:
                result = await fn(**kwargs)
                return result.output if result.success else f"Error: {result.error}"
            wrapper.__name__ = name
            wrapper.__doc__ = f"Execute the {name} tool"
            return wrapper

        fn = _make_sync_wrapper(tool_name, tool_fn)
        adk_tools.append(FunctionTool(fn=fn))

    return adk_tools


def create_adk_agent() -> Agent:
    """Create an ADK-compatible Agent wrapping our orchestrator."""
    tools = _create_function_tools()

    agent = Agent(
        name="nexusmind_agent",
        model=settings.gemini_model,
        description="Autonomous task-execution agent powered by Gemini",
        instruction=SYSTEM_PROMPT,
        tools=tools,
    )
    logger.info("Created ADK agent with %d tools", len(tools))
    return agent


def create_runner() -> Runner:
    """Create an ADK Runner for the agent."""
    agent = create_adk_agent()
    runner = Runner(agent=agent)
    return runner

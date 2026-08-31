"""Deterministic command gate — zero-cost routing before the LLM.

Adapted from Hermes Agent's pre-LLM lexical gates (``cli.py`` bang/slash
dispatch) and OpenClaw's layered routing ladder: explicit ``/command`` syntax
is intercepted and resolved WITHOUT any model call. Only natural language
falls through to the agent loop.

Design notes:
- Unknown slash-commands are NOT handled (they fall through), matching both
  reference frameworks — ambiguity is deliberately handed to the model.
- Host layers (api.main) register data providers at startup so this module
  never imports upward (dependency inversion, no circular imports).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from agent.config import settings

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Registered by host layers: name -> zero-arg callable returning data.
_providers: dict[str, Callable[[], Any]] = {}

COMMANDS = ("/help", "/start", "/status", "/tasks", "/pending", "/tools", "/skills", "/memory")


def register_provider(name: str, fn: Callable[[], Any]) -> None:
    """Register a data provider (e.g. recent_tasks) from the host layer."""
    _providers[name] = fn


def looks_like_command(text: str) -> bool:
    """True for ``/cmd ...`` but NOT for paths like ``/Users/x/file.md fix this``.

    Hermes heuristic (cli.py:_looks_like_slash_command): a real command's
    first token contains no further slashes.
    """
    if not text or not text.startswith("/"):
        return False
    first_word = text.split()[0]
    return "/" not in first_word[1:]


def _provider_data(name: str) -> Any:
    fn = _providers.get(name)
    return fn() if fn else None


def _cmd_help(args: str) -> str:
    return (
        "NexusMind command gate (handled locally, no AI call)\n"
        "/help - this list\n"
        "/status - agent status\n"
        "/tasks [n] - recent tasks\n"
        "/pending - pending approvals\n"
        "/tools - registered tools\n"
        "/skills [query] - procedural skill index\n"
        "/memory <query> - search persistent memory"
    )


def _cmd_status(args: str) -> str:
    from agent.core.executor import list_tools

    lines = [
        "Agent Online",
        f"Model: {settings.gemini_model}",
        f"Tools: {len(list_tools())}",
        f"Approval mode: {settings.approval_mode}",
    ]
    try:
        from agent.core.skill_library import skill_library

        skill_library.apply_transitions()
        active = len(skill_library.list_skills())
        if active:
            lines.append(f"Skills: {active}")
    except Exception:
        pass
    return "\n".join(lines)


def _cmd_tasks(args: str) -> str:
    tasks = _provider_data("recent_tasks")
    if not tasks:
        return "No recent tasks."
    try:
        limit = max(1, min(int(args.split()[0]), 20)) if args.strip() else 10
    except ValueError:
        limit = 10
    lines = ["Recent tasks:"]
    for t in tasks[:limit]:
        status = t.get("status", "?")
        goal = str(t.get("goal", ""))[:70]
        lines.append(f"- [{status}] {goal}")
    return "\n".join(lines)


def _cmd_pending(args: str) -> str:
    from agent.core.executor import get_pending_approvals

    pending = get_pending_approvals()
    if not pending:
        return "No pending approvals."
    lines = ["Pending approvals:"]
    for p in pending:
        lines.append(f"- {p['tool_name']} — {p['description'][:80]}")
    return "\n".join(lines)


def _cmd_tools(args: str) -> str:
    from agent.core.command_gate import canonical_tool_names

    names = canonical_tool_names()
    return "Registered tools:\n" + "\n".join(f"- {n}" for n in names)


def _cmd_skills(args: str) -> str:
    from agent.core.skill_library import skill_library

    skill_library.apply_transitions()
    skills = skill_library.list_skills()
    query = args.strip().lower()
    if query:
        skills = [
            s
            for s in skills
            if query in s["name"].lower() or query in (s.get("description") or "").lower()
        ]
    if not skills:
        return (
            "No skills match."
            if query
            else ("No skills yet. The agent auto-synthesizes them after solving multi-step tasks.")
        )
    lines = ["Skills:"]
    for s in skills:
        desc = (s.get("description") or "")[:60]
        uses = s.get("use_count") or 0
        lines.append(f"- {s['name']} ({uses} uses) — {desc}")
    return "\n".join(lines)


def _cmd_memory(args: str) -> str:
    from agent.core.memory import memory_store

    query = args.strip()
    entries = memory_store.search(query, top_k=5) if query else memory_store.get_recent(5)
    if not entries:
        return "No matching memories." if query else "Memory is empty."
    label = f"Memories for '{query}':" if query else "Recent memories:"
    lines = [label]
    for e in entries:
        lines.append(f"- [{e.category}] {e.content[:80]}")
    return "\n".join(lines)


_HANDLERS = {
    "help": _cmd_help,
    "start": _cmd_help,
    "status": _cmd_status,
    "tasks": _cmd_tasks,
    "pending": _cmd_pending,
    "tools": _cmd_tools,
    "skills": _cmd_skills,
    "memory": _cmd_memory,
}


async def handle_command(text: str) -> str | None:
    """Resolve a ``/command`` deterministically. None = fall through to agent."""
    if not looks_like_command(text):
        return None
    parts = text.split(maxsplit=1)
    name = parts[0].lower().lstrip("/")
    args = parts[1] if len(parts) > 1 else ""
    handler = _HANDLERS.get(name)
    if handler is None:
        logger.debug("Command gate fall-through: %s", name)
        return None
    try:
        return handler(args)
    except Exception:
        logger.exception("Command /%s failed", name)
        return f"Command /{name} failed. Try /help."


def canonical_tool_names() -> list[str]:
    """Live registry names (loads skills first). Single source of truth."""
    from agent.core.executor import list_tools
    from agent.skills.loader import load_all_skills

    load_all_skills()
    return sorted(list_tools())

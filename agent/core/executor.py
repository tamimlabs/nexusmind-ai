"""Tool executor with self-correction, retry loops, and human-in-the-loop approval.

Patterns adapted from OpenClaw's sandboxed execution + Hermes' tool guardrails.
Key hackathon features:
  - Self-correction: on failure, analyzes error with Gemini, adjusts, retries
  - Human approval: high-risk actions pause for one-click approval
  - Smart approval: auto-approve safe commands, only ask for dangerous ones
  - Telegram approvals: approve from your phone while agent runs autonomously
  - Audit trail: every tool call logged for traceability dashboard
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent.models import StepStatus, TaskStep, ToolResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# ── Tool Registry ─────────────────────────────────────────────────

_tool_registry: dict[str, Callable[..., Awaitable[ToolResult]]] = {}
_high_risk_tools: set[str] = {"send_email", "execute_code", "run_command", "deploy", "transfer_funds"}

# Pending approvals — keyed by step ID
_pending_approvals: dict[str, asyncio.Event] = {}
_approval_results: dict[str, bool] = {}
_approval_timed_out: set[str] = set()
_approval_metadata: dict[str, dict[str, str]] = {}
_approval_lock = __import__("threading").Lock()

# Task context for Telegram messages (goal, etc.)
_task_context: dict[str, str] = {}

# Tasks whose human granted at least one risky action: later risky steps in
# the SAME task auto-approve ("one approval per task"). Prevents approval
# fatigue when a goal legitimately needs several high-risk steps; unrelated
# future tasks still ask as usual. Trust is pruned when the task finishes.
_task_trusted: set[str] = set()


def register_tool(name: str, high_risk: bool = False) -> Callable:
    """Decorator to register a tool. Set high_risk=True for tools needing approval."""
    def decorator(func: Callable[..., Awaitable[ToolResult]]) -> Callable:
        _tool_registry[name] = func
        if high_risk or name in _high_risk_tools:
            _high_risk_tools.add(name)
        return func
    return decorator


def get_tool(name: str) -> Callable[..., Awaitable[ToolResult]] | None:
    return _tool_registry.get(name)


def list_tools() -> list[str]:
    return list(_tool_registry.keys())


def set_task_context(task_id: str, goal: str) -> None:
    """Set task context for Telegram approval messages."""
    _task_context[task_id] = goal


# ── Smart Approval Logic ──────────────────────────────────────────

# Safe commands that never need approval (read-only, no side effects)
_SAFE_COMMANDS = {
    "ls", "dir", "pwd", "whoami", "date", "echo", "cat", "head", "tail",
    "grep", "find", "wc", "sort", "uniq", "diff", "file", "stat",
    "git status", "git log", "git diff", "git show", "git branch",
    "git remote", "git config --list",
    "pip list", "pip show", "pip freeze",
    "node --version", "npm --version",
    "docker ps", "docker images", "docker logs",
    "ps aux", "top", "df", "du", "free", "uptime",
    "curl -I", "curl -s", "curl", "wget --spider",
}

# Dangerous patterns that ALWAYS need approval
_DANGEROUS_PATTERNS = [
    r"rm\s+-rf",           # recursive force delete
    r"rm\s+/",            # delete from root
    r"del\s+/[sfq]",      # Windows force delete
    r"format\s+[a-z]",    # format disk
    r"shutdown",           # shutdown
    r"reboot",             # reboot
    r"sudo\s+",           # sudo commands
    r"chmod\s+777",       # world-writable
    r"chown\s+",          # change ownership
    r"eval\s+",           # eval (code injection)
    r"exec\s+",           # exec
    r"\|\s*bash",         # pipe to bash
    r"\|\s*sh",           # pipe to sh
    r">\s*/dev/sd",       # write to disk device
    r"dd\s+if=",          # dd (disk destroyer)
    r"mkfs\.",            # format filesystem
    r"mount\s+",          # mount
    r"umount\s+",         # unmount
    r"iptables",          # firewall rules
    r"systemctl\s+(stop|restart|disable)",  # stop/restart services
    r"kill\s+-9",         # force kill
    r"killall",           # kill all
    r"pkill",             # pkill
    r"curl.*\|\s*bash",   # curl pipe to bash
    r"wget.*\|\s*sh",     # wget pipe to sh
r"find\s+.*-delete\b(?:\s|$)",  # silent recursive delete
    r"find\s+.*-exec\b",          # arbitrary command execution
    r"git\s+branch\s+-[dD]\b",       # delete/force-delete a branch
    r"transfer_funds",    # financial transfers
    r"deploy",            # deployments
    r"send_email",        # emails (potentially spam)
]

# Shell constructs that turn a "safe-looking" read into a write or compound
# command (redirections, pipelines, chaining, command substitution). Commands
# containing these are never auto-approved via the safe-prefix whitelist even
# when they START with a documented safe word (e.g. `echo x >> /etc/crontab`).
_SHELL_SIDE_EFFECT_RE = re.compile(r"[;|`>]|&&|\|\||\$\(")

# Dangerous patterns in code
_DANGEROUS_CODE_PATTERNS = [
    r"os\.system\s*\(",         # os.system
    r"subprocess\.",            # subprocess calls
    r"shutil\.rmtree",          # recursive delete
    r"os\.remove",              # file removal
    r"os\.unlink",              # file removal
    r"os\.rmdir",               # directory removal
    r"open\s*\([^)]*['\"]w['\"]",  # file write
    r"open\s*\([^)]*['\"]a['\"]",  # file append
    r"__import__",              # dynamic imports
    r"eval\s*\(",               # eval
    r"exec\s*\(",               # exec
    r"globals\s*\(",            # globals access
    r"locals\s*\(",             # locals access
]


def is_safe_command(command: str) -> bool:
    """Check if a shell command is safe (read-only, no side effects)."""
    cmd = command.strip().lower()

    # mkdir under allowed roots (projects/, output/) is safe — auto-approve
    # Strict: must be pure mkdir with no shell chaining, no absolute paths, no traversal
    if re.match(r"^\s*mkdir\s+(-p\s+)?.*$", cmd):
        if _SHELL_SIDE_EFFECT_RE.search(cmd):
            return False
        if ".." in cmd or re.search(r"\s/|\s[a-z]:", cmd):
            return False
        # Validate each path arg is under allowed roots
        payload = re.sub(r"^\s*mkdir\s+(-p\s+)?", "", cmd).strip()
        if not payload:
            return False
        parts = [p.strip().strip("'\"") for p in re.split(r"\s+", payload) if p.strip()]
        if not parts:
            return False
        for part in parts:
            if not (part.startswith("projects/") or part.startswith("output/") or part.startswith("projects\\") or part.startswith("output\\")):
                return False
            if part.startswith("/") or re.match(r"^[a-z]:", part):
                return False
        return True

    # Any redirection/pipe/compound/chaining construct is a side effect —
    # never auto-approve it, even if the command STARTs with a safe word
    # (e.g. `echo x >> /etc/crontab` or `ls; rm -rf`). Those get evaluated
    # against the dangerous patterns above and then require approval.
    if _SHELL_SIDE_EFFECT_RE.search(cmd):
        return False

    # Check exact matches and prefix matches
    for safe in _SAFE_COMMANDS:
        if cmd == safe or cmd.startswith(safe + " "):
            return True

    # Check if it's just a read-only Python script execution. Anything that
    # touches os/subprocess/shutil/filesystem/exec is a side effect.
    if cmd.startswith("python "):
        payload = cmd[len("python ") :]
        return not any(
            d in payload for d in ["os.", "subprocess", "shutil", "exec(", "eval(", "open(", "__import__"]
        )

    return False


def is_dangerous_command(command: str) -> bool:
    """Check if a shell command matches dangerous patterns."""
    cmd = command.lower()
    return any(re.search(pattern, cmd) for pattern in _DANGEROUS_PATTERNS)


def is_dangerous_code(code: str) -> bool:
    """Check if code contains dangerous operations."""
    return any(re.search(pattern, code) for pattern in _DANGEROUS_CODE_PATTERNS)


def _normalize_mode(raw: str) -> str:
    """Normalize approval_mode aliases to canonical 'always'/'smart'/'never'."""
    m = (raw or "smart").strip().lower().replace(" ", "_").replace("-", "_")
    if m in {"always", "ask", "ask_everytime", "everytime", "ask_every_time", "always_ask"}:
        return "always"
    if m in {"never", "none", "no_ask", "disabled", "off"}:
        return "never"
    return "smart"


def needs_approval(tool_name: str, tool_args: dict[str, Any]) -> bool:
    """Determine if a tool call needs human approval based on approval mode.

    Supports aliases: 'always' == 'ask_everytime' == 'everytime'.

    Returns:
        True if approval is needed, False if auto-approved.

    """
    from agent.config import settings

    mode = _normalize_mode(settings.approval_mode)

    # "never" mode — auto-approve everything
    if mode == "never":
        return False

    # "always" mode — ask for every high-risk tool
    if mode == "always":
        return tool_name in _high_risk_tools

    # "smart" mode — analyze the command/code
    if mode == "smart":
        if tool_name not in _high_risk_tools:
            return False

        # Check command safety
        if tool_name == "run_command":
            command = tool_args.get("command", "")
            if is_dangerous_command(command):
                logger.warning("Dangerous command detected: %s", command[:100])
                return True
            if is_safe_command(command):
                logger.info("Auto-approving safe command: %s", command[:100])
                return False
            # Unknown command — ask for approval (safe default)
            return True

        # Check code safety
        if tool_name == "execute_code":
            code = tool_args.get("code", "")
            if is_dangerous_code(code):
                logger.warning("Dangerous code detected")
                return True
            # Scaffold under allowed roots (projects/ / output/) via pathlib is safe — auto-approve
            # Avoids blocking portfolio/website builds which always use `import pathlib` + write_text.
            # Only block if it contains actual traversal like "../" or "projects/.." or dangerous ops.
            if "pathlib" in code and ("projects/" in code or "output/" in code):
                has_traversal = bool(re.search(r"\.\./|projects/\.\.|output/\.\.", code))
                has_dangerous = bool(re.search(r"os\.system|subprocess|shutil\.rmtree|os\.remove|rm\s+-rf", code))
                if not has_traversal and not has_dangerous:
                    logger.info("Auto-approving safe pathlib scaffold to projects/output")
                    return False
            # Simple read-only code — auto-approve
            if not any(d in code for d in ["open(", "os.", "shutil.", "subprocess", "import"]):
                logger.info("Auto-approving read-only code")
                return False
            # Code with imports/IO — ask
            return True

        # Other high-risk tools — always ask
        return True

    # Default: ask for approval
    return tool_name in _high_risk_tools


# ── Human-in-the-Loop ─────────────────────────────────────────────

async def request_approval(step_id: str, description: str, tool_name: str, tool_args: dict[str, Any] | None = None, task_goal: str = "", task_id: str = "") -> dict[str, Any]:
    """Request human approval for a high-risk action.

    Tries Telegram first (remote), falls back to dashboard (local).
    Returns:
        Dict with status and description for the approval UI.

    """
    event = asyncio.Event()
    with _approval_lock:
        _pending_approvals[step_id] = event
        _approval_metadata[step_id] = {"tool_name": tool_name, "description": description, "task_id": task_id}
    logger.warning("APPROVAL REQUIRED: [%s] %s — %s", step_id, tool_name, description)

    # Try Telegram first
    from agent.telegram import is_configured, request_approval_via_telegram

    telegram_status = "not_configured"
    # _task_context is keyed by task_id, not step_id — resolve correctly
    resolved_goal = task_goal
    if not resolved_goal and task_id and task_id in _task_context:
        resolved_goal = _task_context[task_id]
    elif not resolved_goal and _task_context:
        # Fallback: if task_id not provided but context has single entry, use it
        # or try step_id (legacy)
        resolved_goal = _task_context.get(step_id, "") or next(iter(_task_context.values()), "")

    if is_configured():
        logger.info("Telegram is_configured=True, sending approval for %s", step_id)
        extra_info = ""
        if tool_args:
            if "command" in tool_args:
                extra_info = f"Command: {tool_args['command'][:200]}"
            elif "code" in tool_args:
                extra_info = f"Code:\n{tool_args['code'][:200]}"
            elif "url" in tool_args:
                extra_info = f"URL: {tool_args['url']}"

        try:
            result = await request_approval_via_telegram(
                step_id=step_id,
                tool_name=tool_name,
                description=description,
                task_goal=resolved_goal,
                extra_info=extra_info,
            )
            telegram_status = result.get("status", "failed")
            logger.info("Telegram approval send for %s -> %s", step_id[:8], telegram_status)
            if telegram_status == "failed":
                logger.warning("Telegram send failed for %s (check TELEGRAM_BOT_TOKEN/CHAT_ID)", step_id[:8])
        except Exception as exc:
            logger.exception("Telegram approval exception for %s: %s", step_id[:8], exc)
            telegram_status = "exception"
    else:
        logger.warning("Telegram not configured (is_configured=False) for approval %s — dashboard only", step_id[:8])

    return {
        "status": "pending_approval",
        "step_id": step_id,
        "tool_name": tool_name,
        "description": description,
        "telegram_status": telegram_status,
    }


def resolve_approval(step_id: str, approved: bool) -> None:
    """Resolve a pending approval (called from API/UI or Telegram).

    A granted approval also "trusts" the rest of its task: subsequent risky
    steps in the same task auto-approve (one approval per task). Denials never
    trust the task.
    """
    with _approval_lock:
        if step_id not in _pending_approvals:
            return
        meta = _approval_metadata.get(step_id, {})
        if approved and meta.get("task_id"):
            _task_trusted.add(meta["task_id"])
            logger.info("Task %s trusted: remaining risky steps will auto-approve", meta["task_id"])
        _approval_results[step_id] = approved
        ev = _pending_approvals.get(step_id)
        if ev:
            ev.set()
        _approval_metadata.pop(step_id, None)
    logger.info("Approval %s for step %s", "granted" if approved else "denied", step_id)


async def wait_for_approval(step_id: str, timeout: float = 300) -> bool:
    """Wait for human approval with timeout. Returns True if approved."""
    if step_id not in _pending_approvals:
        return True
    ev = _pending_approvals[step_id]
    logger.debug("Waiting %ss for approval %s", timeout, step_id[:8])
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
        result = _approval_results.get(step_id, False)
        logger.info("Approval %s resolved -> %s", step_id[:8], result)
        return result
    except TimeoutError:
        logger.warning("Approval timed out for step %s", step_id)
        _approval_timed_out.add(step_id)
        return False


def get_pending_approvals() -> list[dict[str, str]]:
    """Return all pending approvals for the dashboard."""
    # Snapshot under lock to avoid 'dictionary changed size during iteration'
    with _approval_lock:
        for_snapshot = list(_pending_approvals.items())
        meta_snapshot = dict(_approval_metadata)
    return [
        {
            "step_id": sid,
            "status": "pending",
            "tool_name": meta_snapshot.get(sid, {}).get("tool_name", "Unknown"),
            "description": meta_snapshot.get(sid, {}).get("description", ""),
        }
        for sid, ev in for_snapshot
        if not ev.is_set()
    ]


def is_task_trusted(task_id: str) -> bool:
    """Return True if a granted approval already covers this task's risky steps."""
    return task_id in _task_trusted


def trust_task(task_id: str) -> None:
    """Mark a task trusted — its remaining risky steps will auto-approve."""
    _task_trusted.add(task_id)
    logger.info("Task %s marked trusted (auto-approve remaining risky steps)", task_id)


def untrust_task(task_id: str) -> None:
    """Clear task trust (called when the task finishes)."""
    _task_trusted.discard(task_id)


def get_trusted_tasks() -> list[str]:
    """Return task ids currently approved for auto-approval (diagnostics)."""
    return sorted(_task_trusted)


# ── Self-Correction with Gemini ───────────────────────────────────

# Tools whose failures may legitimately be retried via web_search.
_RESEARCH_TOOLS = {"web_search", "fetch_url"}

async def self_correct(error: str, tool_name: str, original_args: dict[str, Any]) -> dict[str, Any] | None:
    """Analyze a failed tool call and suggest a fix or alternative approach.

    Action tools are NEVER switched to web_search — a failed GitHub/API/file
    operation must not turn into an internet search full of irrelevant noise.

    Returns:
        Corrected args dict, or None if no correction possible.

    """
    from agent.core.gemini_client import generate_content

    prompt = f"""A tool call failed. Your job is to find a WORKING alternative.

Failed tool: {tool_name}
Original args: {original_args}
Error: {error}

ALTERNATIVE STRATEGIES:
- If fetch_url failed (DNS/network): Switch to web_search with the same topic
- If web_search failed (CAPTCHA/blocked): Try a different query phrasing
- If summarize_text got empty input: Return a request to skip (null)
- For run_command/execute_code/GitHub tools: fix the args (bad URL, missing
  argument, quoting) or return null — do NOT switch them to web_search,
  web searches CANNOT perform actions.

Return ONLY one of:
1. A JSON object with corrected args for the SAME tool
2. A JSON object with "switch_to": "tool_name" and new args for an ALTERNATIVE tool
3. null if truly unrecoverable"""

    try:
        response = await generate_content(
            system="You are a debugging assistant. Be creative with alternatives. Return only valid JSON.",
            user=prompt,
            temperature=0.3,
            max_tokens=500,
        )
        response = response.strip()
        if response.startswith("```"):
            lines = response.split("\n")
            lines = [line for line in lines if not line.strip().startswith("```")]
            response = "\n".join(lines)

        if response.lower() in ("null", "none", ""):
            return None
        corrected = json.loads(response)
        if isinstance(corrected, dict):
            logger.info("Self-correction: adjusted approach for %s → %s", tool_name, corrected.get("switch_to", "same tool"))
            return corrected
    except Exception:
        logger.debug("Self-correction failed for %s", tool_name)
    return None


# ── Step Execution with Retry ─────────────────────────────────────

MAX_RETRIES = 2


async def execute_step(step: TaskStep, context: dict[str, Any] | None = None) -> ToolResult:
    """Execute a single task step with self-correction retry loop.

    Flow:
        1. Check if tool needs approval (smart mode) → request approval via Telegram or dashboard
        2. Execute tool
        3. On failure → analyze error with Gemini → adjust args → retry
        4. Log everything for traceability dashboard
    """
    step.status = StepStatus.RUNNING
    tool = get_tool(step.tool_name) if step.tool_name else None

    if tool is None:
        step.status = StepStatus.SKIPPED
        result = ToolResult(
            success=True,
            output=f"No tool specified or found for: {step.description}",
            metadata={"skipped": True},
        )
        step.result = result.output
        return result

    # Smart approval: check if this specific call needs approval
    if needs_approval(step.tool_name, step.tool_args or {}):
        # context uses keys task_id/task_goal (see orchestrator.py:220)
        ctx_task_id = context.get("task_id", "") if context else ""
        ctx_task_goal = context.get("task_goal", "") or context.get("goal", "") if context else ""

        # Per-task trust ("one approval per task"): once the human granted a
        # risky step for this task, the rest of that task auto-approves — a
        # multi-step goal no longer asks once per risky step.
        if ctx_task_id and ctx_task_id in _task_trusted:
            logger.info(
                "Auto-approving %s step for trusted task %s (no approval popup)",
                step.tool_name,
                ctx_task_id,
            )
        else:
            hint = (
                " — Approving also auto-approves the remaining risky steps of this task"
                if ctx_task_id
                else ""
            )
            await request_approval(
                step.id, step.description + hint, step.tool_name,
                tool_args=step.tool_args,
                task_goal=ctx_task_goal,
                task_id=ctx_task_id,
            )
            granted = await wait_for_approval(step.id)
            if not granted:
                step.status = StepStatus.FAILED
                if step.id in _approval_timed_out:
                    step.error = "Approval timed out (5 minutes)"
                    return ToolResult(success=False, output="", error="Approval timed out (5 minutes)")
                step.error = "Human denied approval"
                return ToolResult(success=False, output="", error="Denied by human")

    # Execute with retry + self-correction
    # Resolve {{step_N_result}} templates from context, then merge step args (step args override)
    resolved_args = {}
    for k, v in (step.tool_args or {}).items():
        if isinstance(v, str) and "{{" in v:
            for ctx_key, ctx_val in (context or {}).items():
                v = v.replace("{{" + ctx_key + "}}", str(ctx_val))
            # Safety net: if template still unresolved, try 0-indexed fallback
            if "{{" in v:
                for match in re.finditer(r"\{\{step_(\d+)_result\}\}", v):
                    idx = int(match.group(1))
                    # Try 0-indexed fallback if 1-indexed wasn't found
                    fallback_key = f"step_{idx - 1}_result"
                    if fallback_key in (context or {}):
                        v = v.replace(match.group(0), str(context[fallback_key]))
            # If still unresolved, fail fast instead of sending literal braces
            # to the tool — but only for step_N_result templates; other braces
            # may be legit.
            if (
                "{{" in v
                and "}}" in v
                and re.search(r"\{\{step_\d+_result\}\}", v)
            ):
                step.status = StepStatus.FAILED
                step.error = f"Unresolved template in arg '{k}': {v[:200]} (missing context)"
                return ToolResult(success=False, output="", error=step.error)
        resolved_args[k] = v
    # Schema-drift tolerance (Hermes): LLMs vary arg keys ("file_path",
    # "filename"...). Normalize aliases, then derive required args that are
    # still missing instead of crashing the step with a TypeError.
    _apply_arg_aliases(resolved_args)
    if step.tool_name == "write_file" and not resolved_args.get("path"):
        resolved_args["path"] = await _gemini_derive_write_path(step.description or "", context)
    current_args = dict(resolved_args)
    # B2: forward incremental stdout sink if the caller supplied one via
    # context["on_output"] (e.g. run_adaptive_loop's tool_delta emitter).
    # Kept in current_args so self-correction retries still stream.
    _on_output_cb = None
    if isinstance(context, dict) and callable(context.get("on_output")):
        _on_output_cb = context.get("on_output")
        current_args["on_output"] = _on_output_cb
    last_error = ""
    original_tool = step.tool_name

    for attempt in range(1, MAX_RETRIES + 2):
        # Re-attach on_output if a self-correction update dropped it
        if _on_output_cb is not None and "on_output" not in current_args:
            current_args["on_output"] = _on_output_cb
        try:
            result = await asyncio.wait_for(tool(**current_args), timeout=120)

            if result.success:
                step.status = StepStatus.SUCCESS
                step.result = result.output
                if result.error:
                    step.error = result.error
                return result

            last_error = result.error or "Tool returned failure"
            logger.warning("Tool %s failed (attempt %d): %s", step.tool_name, attempt, last_error[:200])

        except TimeoutError:
            last_error = "Tool execution timed out (120s)"
        except Exception as exc:
            last_error = str(exc)

        # Self-correction on failure
        if attempt <= MAX_RETRIES:
            corrected = await self_correct(last_error, step.tool_name, current_args)
            if corrected:
                switch_to = corrected.pop("switch_to", None)
                if switch_to and switch_to in _tool_registry:
                    if switch_to in _RESEARCH_TOOLS and original_tool not in _RESEARCH_TOOLS:
                        # Never turn failed actions into web searches
                        logger.warning(
                            "Blocked self-correction switch %s -> %s (action tools must not search the web)",
                            original_tool, switch_to,
                        )
                    else:
                        old_tool = step.tool_name
                        tool = _tool_registry[switch_to]
                        step.tool_name = switch_to
                        logger.info("Switching from %s to %s (attempt %d)", old_tool, switch_to, attempt + 1)
                current_args.update(corrected)
                continue
            break  # No correction possible

    step.status = StepStatus.FAILED
    step.error = last_error
    return ToolResult(success=False, output="", error=last_error)


# ── Argument normalization (schema-drift tolerance) ───────────────

# Canonical arg key -> common LLM spellings of the same thing.
_ARG_ALIASES: dict[str, tuple[str, ...]] = {
    "path": ("file_path", "filepath", "filename", "file", "target", "dest", "destination"),
    "content": ("text", "body", "data", "contents"),
    "code": ("script", "source", "program"),
    "query": ("search_query", "q", "question"),
    "command": ("cmd", "shell_command"),
    "url": ("link", "href"),
}


def _apply_arg_aliases(args: dict[str, Any]) -> None:
    """Rename alias keys to canonical ones, in place (first alias wins)."""
    for canonical, alts in _ARG_ALIASES.items():
        if canonical in args:
            continue
        for alt in alts:
            if alt in args:
                args[canonical] = args[alt]
                break


def _derive_write_path(description: str, context: dict[str, Any] | None) -> str:
    """Deterministic fallback for write_file path (used when Gemini unavailable)."""
    d = (description or "").lower()
    goal = str((context or {}).get("task_goal") or "")
    goal_slug = "-".join(
        w for w in re.findall(r"[a-z0-9]+", goal.lower())
        if w not in {"the", "a", "an", "that", "can", "for", "with"}
    )[:40]
    base = f"projects/{goal_slug}" if goal_slug else "output"
    for pattern, name in (
        (r"\bhtml\b|markup|structure", "index.html"),
        (r"\bcss\b|\bstyles?\b|theme", "styles.css"),
        (r"\bjava\s?script\b|\bjs\b|interactiv", "app.js"),
        (r"readme|documentation", "README.md"),
    ):
        if re.search(pattern, d):
            return f"{base}/{name}"
    return f"output/generated_{uuid.uuid4().hex[:8]}.txt"


async def _gemini_derive_write_path(description: str, context: dict[str, Any] | None) -> str:
    """Ask Gemini to choose the file path; fallback to deterministic.

    When ``gemini_full_control`` is True, file naming is controlled by the
    backend AI. We prompt Gemini with the goal + step description and ask for
    a single relative path. The result is validated to stay inside allowed
    roots; on any failure we return the deterministic fallback.
    """
    from agent.config import settings

    if not settings.gemini_full_control or not settings.gemini_api_key:
        return _derive_write_path(description, context)
    try:
        from agent.core.gemini_client import generate_content

        goal = str((context or {}).get("task_goal") or "")
        prompt = (
            f"Goal: {goal}\nStep description: {description}\n\n"
            "Choose ONE relative file path for write_file. Rules:\n"
            "- Must start with 'output/' for single artifacts or 'projects/<kebab-name>/' for multi-file projects.\n"
            "- Use kebab-case folder derived from the GOAL, not random names.\n"
            "- File extension must match content type (.html, .css, .js, .md, .py, .txt, etc.).\n"
            "- Return ONLY the path, nothing else. Example: projects/my-site/index.html"
        )
        raw = await generate_content(
            system="You are a file naming assistant. Return only a file path.",
            user=prompt,
            temperature=0.2,
            max_tokens=64,
        )
        candidate = raw.strip().splitlines()[0].strip().strip("'\"` ")
        # Basic validation: must be relative and inside allowed roots, no traversal
        if (
            candidate
            and not candidate.startswith(("/", ".."))
            and candidate.startswith(("output/", "projects/"))
            and ".." not in candidate
            and candidate.count("/") >= 1
        ):
            return candidate
        logger.warning("Gemini file path invalid (%r), using deterministic fallback", candidate)
    except Exception:
        logger.debug("Gemini file naming failed, using fallback", exc_info=True)
    return _derive_write_path(description, context)


# ── Parallel Task Delegation (opencode task tool parity) ────────


@register_tool("task", high_risk=False)
async def task(description: str, prompt: str, subagent_type: str = "explore", **_: Any) -> ToolResult:
    """Delegate a sub-task to a sub-agent (explore/general).

    `subagent_type=explore` is read-only (grep/glob/read/webfetch/websearch),
    `general` can use all tools. Runs a mini adaptive loop and returns result.
    """
    from agent.models import Task as _Task

    subagent_type = (subagent_type or "explore").strip().lower()
    if subagent_type not in ("explore", "general", "build"):
        subagent_type = "explore"
    # Simple delegation: run a one-shot Gemini solve for explore, or a short loop for general
    try:
        if subagent_type == "explore":
            from agent.core.gemini_client import generate_content

            sys = "You are an explore sub-agent. You have read-only tools: read_file, list_directory, grep, glob (via executor). Answer concisely with findings. No writes."
            raw = await generate_content(system=sys, user=prompt or description, temperature=0.2, max_tokens=2048)
            return ToolResult(success=True, output=raw[:8000], metadata={"subagent": subagent_type})
        else:
            # general: short adaptive loop (max 8 steps) with its own task
            from agent.core.agent_loop import run_adaptive_loop

            t = _Task(goal=prompt or description)
            ctx: dict[str, Any] = {"task_id": t.id, "task_goal": t.goal, "subagent": True}
            # Use same executor for general
            from agent.core.agent_loop import decide_next_step

            outcome = await run_adaptive_loop(t, ctx, decide_fn=decide_next_step, max_steps=8)
            summary = outcome.summary or (t.steps[-1].result if t.steps else "") or "sub-task completed"
            return ToolResult(success=True, output=summary[:8000], metadata={"subagent": subagent_type, "steps": len(t.steps)})
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc)[:600])


# ── TodoWrite Tool (opencode parity) ──────────────────────────────
# Explicit tool so model can call todowrite any turn (not piggyback JSON).


_current_todo_task_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("_current_todo_task_id", default=None)
_todo_emit: Any | None = None  # set by orchestrator/loop to broadcast todo_update


def set_todo_context(task_id: str | None, emit_fn: Any | None = None) -> Callable[[], None]:
    """Set current task for todowrite and optional emit sink.

    Returns a no-arg closure that RESTORES the previous task id + emit sink,
    so nested executions (sub-agents, multi-tool calls) always broadcast to the
    right task and never leak a foreign ``_todo_emit`` into the outer loop.
    """
    global _todo_emit
    prev_id = _current_todo_task_id.get()
    prev_emit = _todo_emit
    _current_todo_task_id.set(task_id)
    if emit_fn is not None:
        _todo_emit = emit_fn

    def _restore() -> None:
        global _todo_emit
        _current_todo_task_id.set(prev_id)
        _todo_emit = prev_emit

    return _restore


@register_tool("todowrite", high_risk=False)
async def todowrite(todos: list[dict[str, Any]] | None = None, **_: Any) -> ToolResult:
    """Update the live TODO checklist for the current task (opencode semantics).

    Overwrites the whole list — pass the FULL desired state each time.
    Supports both `title`/`content`, `status` in pending/in_progress/completed/cancelled/skipped,
    and `priority` low/medium/high.
    """

    if todos is None:
        todos = []
    if not isinstance(todos, list):
        return ToolResult(success=False, output="", error="todos must be a list")
    # Resolve emit context — try to find task's live emit via _todo_emit
    # Actual Task mutation is done by the caller injecting task ref via set_todo_context
    # Fallback: store in context for loop to pick up
    cleaned: list[dict[str, Any]] = []
    for i, raw in enumerate(todos[:30]):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or raw.get("content") or "").strip()
        if not title:
            continue
        title = title[:140]
        status_raw = str(raw.get("status") or "pending").strip().lower()
        # Normalize cancelled -> cancelled, but map to skipped internally if needed
        if status_raw not in ("pending", "in_progress", "completed", "skipped", "cancelled"):
            status_raw = "pending"
        priority_raw = str(raw.get("priority") or "medium").strip().lower()
        if priority_raw not in ("low", "medium", "high"):
            priority_raw = "medium"
        cleaned.append({"title": title, "status": status_raw, "priority": priority_raw, "order": i})
    # Try to mutate live task via global registry (api/main.py _live_tasks injected) or via contextvar
    # We expose cleaned via ToolResult metadata for the loop to apply
    detail = "\n".join(f"[{'x' if c['status']=='completed' else ' '}] {c['title']}" for c in cleaned[:8])
    if _todo_emit is not None:
        try:
            tid = _current_todo_task_id.get()
            if tid:
                _todo_emit(tid, "todo_update", f"Checklist {len([c for c in cleaned if c['status']=='completed'])}/{len(cleaned)}", detail[:1500])
        except Exception:
            logger.debug("todowrite emit failed", exc_info=True)
    return ToolResult(success=True, output=f"Todos updated: {len(cleaned)} items\n{detail[:500]}", metadata={"todos": cleaned, "todo_update": True})


# ── Built-in Tools ────────────────────────────────────────────────


def _scratch_dir() -> Path:
    """Scratch dir for child-process scripts.

    Machine rule (AGENTS.md): heavy temp I/O belongs on D: when available;
    override with NEXUSMIND_TEMP. Falls back to the system temp dir.
    """
    override = os.environ.get("NEXUSMIND_TEMP")
    if override:
        base = Path(override)
    elif Path("D:/").exists():
        base = Path("D:/Temp")
    else:
        base = Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    return base


_EXEC_TIMEOUT = 60  # seconds; patched low in tests


def _env_snapshot() -> dict[str, str]:
    """Project .env as a dict for child-process environments.

    Uses the project-root .env (agent.config._ENV_FILE) — NOT CWD-relative —
    so subprocesses see the same credentials Settings uses on any machine.
    """
    from agent.config import _ENV_FILE

    env: dict[str, str] = {}
    try:
        if _ENV_FILE.exists():
            for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip().strip("'\"")
    except Exception:
        logger.debug("Could not read project .env", exc_info=True)
    return env


@register_tool("execute_code", high_risk=True)
async def execute_code(
    code: str,
    language: str = "python",
    on_output: Any | None = None,
    **_: Any,
) -> ToolResult:
    """Execute code in a sandboxed subprocess.

    When ``on_output`` is provided (callable taking a ``str`` chunk), stdout
    is streamed incrementally — each chunk is forwarded to the callback as it
    arrives so the dashboard can show live ``tool_delta`` events. The final
    :class:`ToolResult` shape is unchanged.
    """
    if language != "python":
        return ToolResult(success=False, output="", error=f"Unsupported language: {language}")

    # Load .env vars into subprocess environment
    env = os.environ.copy()
    env.update(_env_snapshot())

    # WinError 32 fix: fully CLOSE our handle BEFORE spawning the child
    # (NamedTemporaryFile held it open for the whole run), and delete
    # best-effort afterwards — cleanup must never mask the step result.
    script = _scratch_dir() / f"nexusmind_{uuid.uuid4().hex}.py"
    script.write_text(code, encoding="utf-8")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Incremental stdout streaming when a sink is present; otherwise fall
        # back to the classic buffered communicate() path (keeps tests stable).
        if on_output is not None and callable(on_output):
            stdout_chunks: list[bytes] = []
            stderr_chunks: list[bytes] = []

            async def _drain_stdout() -> None:
                assert proc.stdout is not None
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        break
                    stdout_chunks.append(chunk)
                    text = chunk.decode(errors="replace")
                    try:
                        res = on_output(text)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception:
                        logger.debug("on_output callback failed (execute_code stdout)", exc_info=True)

            async def _drain_stderr() -> None:
                assert proc.stderr is not None
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    stderr_chunks.append(chunk)

            try:
                await asyncio.wait_for(
                    asyncio.gather(_drain_stdout(), _drain_stderr(), proc.wait()),
                    timeout=_EXEC_TIMEOUT,
                )
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Execution timed out after {_EXEC_TIMEOUT}s",
                )
            stdout = b"".join(stdout_chunks)
            stderr = b"".join(stderr_chunks)
            success = proc.returncode == 0
            return ToolResult(
                success=success,
                output=stdout.decode(errors="replace"),
                error=stderr.decode(errors="replace") if not success else None,
            )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_EXEC_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return ToolResult(
                success=False,
                output="",
                error=f"Execution timed out after {_EXEC_TIMEOUT}s",
            )
        success = proc.returncode == 0
        return ToolResult(
            success=success,
            output=stdout.decode(errors="replace"),
            error=stderr.decode(errors="replace") if not success else None,
        )
    finally:
        for _attempt in range(3):
            try:
                script.unlink()
                break
            except FileNotFoundError:
                break
            except OSError:
                await asyncio.sleep(0.1)  # AV/child still releasing the file


@register_tool("run_command", high_risk=True)
async def run_command(command: str, on_output: Any | None = None, **_: Any) -> ToolResult:
    """Run a shell command. Handles mkdir cross-platform via pathlib when possible.

    When ``on_output`` is supplied, stdout is forwarded incrementally (per
    4 KiB chunk) so callers can emit live ``tool_delta`` events. Final
    :class:`ToolResult` is unchanged.
    """
    # Cross-platform fix: mkdir -p fails on Windows CMD. Intercept and use pathlib.
    # Reject compound shell operators before fast-path to avoid silently swallowing "&& rm -rf"
    if _SHELL_SIDE_EFFECT_RE.search(command) or "&&" in command or "||" in command:
        # Do NOT use mkdir fast-path for compound commands; fall through to real shell (requires approval)
        # But if the command is an attempt to inject, fail fast
        if re.match(r"^\s*mkdir\s+", command.strip(), re.IGNORECASE):
            return ToolResult(success=False, output="", error="Refusing mkdir with shell chaining/operators — split into separate steps")
    else:
        m = re.match(r"^\s*mkdir\s+(?:-p\s+)?(.+)\s*$", command.strip(), re.IGNORECASE)
        if m:
            raw_paths = m.group(1).strip()
            # Split on space (support "mkdir -p a b c")
            created_any = False
            for part in re.split(r"\s+", raw_paths):
                p = part.strip().strip("'\"")
                if not p or ".." in p or p.startswith("/") or re.match(r"^[a-zA-Z]:", p):
                    continue
                # Only allow projects/ and output/ trees
                if not (p.startswith("projects/") or p.startswith("output/") or p.startswith("projects\\") or p.startswith("output\\")):
                    continue
                try:
                    Path(p).mkdir(parents=True, exist_ok=True)
                    created_any = True
                except Exception as exc:
                    return ToolResult(success=False, output="", error=f"mkdir failed for {p}: {exc}")
            if created_any:
                return ToolResult(success=True, output=f"Created directories: {raw_paths}")
            return ToolResult(success=False, output="", error=f"mkdir failed: no valid paths in '{raw_paths}'")

    # Load .env vars into subprocess environment (project-root .env)
    env = os.environ.copy()
    env.update(_env_snapshot())

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        if on_output is not None and callable(on_output):
            stdout_chunks: list[bytes] = []
            stderr_chunks: list[bytes] = []

            async def _drain_stdout_rc() -> None:
                assert proc.stdout is not None
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        break
                    stdout_chunks.append(chunk)
                    text = chunk.decode(errors="replace")
                    try:
                        res = on_output(text)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception:
                        logger.debug("on_output callback failed (run_command stdout)", exc_info=True)

            async def _drain_stderr_rc() -> None:
                assert proc.stderr is not None
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    stderr_chunks.append(chunk)

            try:
                await asyncio.wait_for(
                    asyncio.gather(_drain_stdout_rc(), _drain_stderr_rc(), proc.wait()),
                    timeout=120,
                )
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return ToolResult(success=False, output="", error="Command timed out")
            stdout = b"".join(stdout_chunks)
            stderr = b"".join(stderr_chunks)
            success = proc.returncode == 0
            return ToolResult(
                success=success,
                output=stdout.decode(errors="replace"),
                error=stderr.decode(errors="replace") if not success else None,
            )

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        success = proc.returncode == 0
        return ToolResult(
            success=success,
            output=stdout.decode(errors="replace"),
            error=stderr.decode(errors="replace") if not success else None,
        )
    except TimeoutError:
        return ToolResult(success=False, output="", error="Command timed out")

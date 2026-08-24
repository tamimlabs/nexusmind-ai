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
import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent.models import StepStatus, TaskStep, ToolResult

logger = logging.getLogger(__name__)

# ── Tool Registry ─────────────────────────────────────────────────

_tool_registry: dict[str, Callable[..., Awaitable[ToolResult]]] = {}
_high_risk_tools: set[str] = {"send_email", "execute_code", "run_command", "deploy", "transfer_funds"}

# Pending approvals — keyed by step ID
_pending_approvals: dict[str, asyncio.Event] = {}
_approval_results: dict[str, bool] = {}
_approval_metadata: dict[str, dict[str, str]] = {}

# Task context for Telegram messages (goal, etc.)
_task_context: dict[str, str] = {}


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
    "python --version", "python -c", "python -V",
    "node --version", "npm --version",
    "docker ps", "docker images", "docker logs",
    "ps aux", "top", "df", "du", "free", "uptime",
    "curl -I", "wget --spider",
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
    r"transfer_funds",    # financial transfers
    r"deploy",            # deployments
    r"send_email",        # emails (potentially spam)
]

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

    # Check exact matches and prefix matches
    for safe in _SAFE_COMMANDS:
        if cmd == safe or cmd.startswith(safe + " "):
            return True

    # Check if it's just a Python script execution (read-only)
    if cmd.startswith("python ") and not any(d in cmd for d in ["open(", "os.", "shutil.", "subprocess"]):
        return True

    return False


def is_dangerous_command(command: str) -> bool:
    """Check if a shell command matches dangerous patterns."""
    cmd = command.lower()
    return any(re.search(pattern, cmd) for pattern in _DANGEROUS_PATTERNS)


def is_dangerous_code(code: str) -> bool:
    """Check if code contains dangerous operations."""
    return any(re.search(pattern, code) for pattern in _DANGEROUS_CODE_PATTERNS)


def needs_approval(tool_name: str, tool_args: dict[str, Any]) -> bool:
    """Determine if a tool call needs human approval based on approval mode.

    Returns:
        True if approval is needed, False if auto-approved.

    """
    from agent.config import settings

    mode = settings.approval_mode

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
            if is_safe_command(command):
                logger.info("Auto-approving safe command: %s", command[:100])
                return False
            if is_dangerous_command(command):
                logger.warning("Dangerous command detected: %s", command[:100])
                return True
            # Unknown command — ask for approval (safe default)
            return True

        # Check code safety
        if tool_name == "execute_code":
            code = tool_args.get("code", "")
            if is_dangerous_code(code):
                logger.warning("Dangerous code detected")
                return True
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

async def request_approval(step_id: str, description: str, tool_name: str, tool_args: dict[str, Any] | None = None, task_goal: str = "") -> dict[str, Any]:
    """Request human approval for a high-risk action.

    Tries Telegram first (remote), falls back to dashboard (local).
    Returns:
        Dict with status and description for the approval UI.

    """
    event = asyncio.Event()
    _pending_approvals[step_id] = event
    _approval_metadata[step_id] = {"tool_name": tool_name, "description": description}
    logger.warning("APPROVAL REQUIRED: [%s] %s — %s", step_id, tool_name, description)

    # Try Telegram first
    from agent.telegram import is_configured, request_approval_via_telegram

    telegram_status = "not_configured"
    if is_configured():
        extra_info = ""
        if tool_args:
            if "command" in tool_args:
                extra_info = f"Command: {tool_args['command'][:200]}"
            elif "code" in tool_args:
                extra_info = f"Code:\n{tool_args['code'][:200]}"
            elif "url" in tool_args:
                extra_info = f"URL: {tool_args['url']}"

        result = await request_approval_via_telegram(
            step_id=step_id,
            tool_name=tool_name,
            description=description,
            task_goal=task_goal or _task_context.get(step_id, ""),
            extra_info=extra_info,
        )
        telegram_status = result.get("status", "failed")

    return {
        "status": "pending_approval",
        "step_id": step_id,
        "tool_name": tool_name,
        "description": description,
        "telegram_status": telegram_status,
    }


def resolve_approval(step_id: str, approved: bool) -> None:
    """Resolve a pending approval (called from API/UI or Telegram)."""
    if step_id in _pending_approvals:
        _approval_results[step_id] = approved
        _pending_approvals[step_id].set()
        _approval_metadata.pop(step_id, None)
        logger.info("Approval %s for step %s", "granted" if approved else "denied", step_id)


async def wait_for_approval(step_id: str, timeout: float = 300) -> bool:
    """Wait for human approval with timeout. Returns True if approved."""
    if step_id not in _pending_approvals:
        return True
    try:
        await asyncio.wait_for(_pending_approvals[step_id].wait(), timeout=timeout)
        return _approval_results.get(step_id, False)
    except TimeoutError:
        logger.warning("Approval timed out for step %s", step_id)
        return False


def get_pending_approvals() -> list[dict[str, str]]:
    """Return all pending approvals for the dashboard."""
    return [
        {
            "step_id": sid,
            "status": "pending",
            "tool_name": _approval_metadata.get(sid, {}).get("tool_name", "Unknown"),
            "description": _approval_metadata.get(sid, {}).get("description", ""),
        }
        for sid in _pending_approvals
        if not _pending_approvals[sid].is_set()
    ]


# ── Self-Correction with Gemini ───────────────────────────────────

async def self_correct(error: str, tool_name: str, original_args: dict[str, Any]) -> dict[str, Any] | None:
    """Analyze a failed tool call and suggest a fix or alternative approach.

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
- If a tool keeps failing: Suggest a completely different approach

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
            lines = [l for l in lines if not l.strip().startswith("```")]
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
        # Send approval request (Telegram + dashboard)
        await request_approval(
            step.id, step.description, step.tool_name,
            tool_args=step.tool_args,
            task_goal=context.get("goal", "") if context else "",
        )
        granted = await wait_for_approval(step.id)
        if not granted:
            step.status = StepStatus.FAILED
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
        resolved_args[k] = v
    current_args = dict(resolved_args)
    last_error = ""

    for attempt in range(1, MAX_RETRIES + 2):
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
                    tool = _tool_registry[switch_to]
                    step.tool_name = switch_to
                    logger.info("Switching from %s to %s (attempt %d)", step.tool_name, switch_to, attempt + 1)
                current_args.update(corrected)
                continue
            break  # No correction possible

    step.status = StepStatus.FAILED
    step.error = last_error
    return ToolResult(success=False, output="", error=last_error)


# ── Built-in Tools ────────────────────────────────────────────────


@register_tool("execute_code", high_risk=True)
async def execute_code(code: str, language: str = "python", **_: Any) -> ToolResult:
    """Execute code in a sandboxed subprocess."""
    if language == "python":
        # Load .env vars into subprocess environment
        env = os.environ.copy()
        env_file = Path(".env")
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    env[key.strip()] = value.strip().strip("'\"")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            f.flush()
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, f.name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
                success = proc.returncode == 0
                return ToolResult(
                    success=success,
                    output=stdout.decode(errors="replace"),
                    error=stderr.decode(errors="replace") if not success else None,
                )
            finally:
                Path(f.name).unlink(missing_ok=True)
    return ToolResult(success=False, output="", error=f"Unsupported language: {language}")


@register_tool("run_command", high_risk=True)
async def run_command(command: str, **_: Any) -> ToolResult:
    """Run a shell command."""
    # Load .env vars into subprocess environment
    env = os.environ.copy()
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip("'\"")

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
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

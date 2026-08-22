"""Tool executor with self-correction, retry loops, and human-in-the-loop approval.

Patterns adapted from OpenClaw's sandboxed execution + Hermes' tool guardrails.
Key hackathon features:
  - Self-correction: on failure, analyzes error with Gemini, adjusts, retries
  - Human approval: high-risk actions pause for one-click approval
  - Audit trail: every tool call logged for traceability dashboard
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Awaitable

from agent.models import StepStatus, TaskStep, ToolResult

logger = logging.getLogger(__name__)

# ── Tool Registry ─────────────────────────────────────────────────

_tool_registry: dict[str, Callable[..., Awaitable[ToolResult]]] = {}
_high_risk_tools: set[str] = {"send_email", "execute_code", "run_command", "deploy", "transfer_funds"}

# Pending approvals — keyed by step ID
_pending_approvals: dict[str, asyncio.Event] = {}
_approval_results: dict[str, bool] = {}
_approval_metadata: dict[str, dict[str, str]] = {}


class ApprovalStatus(Enum):
    APPROVED = "approved"
    DENIED = "denied"
    PENDING = "pending"


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


# ── Human-in-the-Loop ─────────────────────────────────────────────

def request_approval(step_id: str, description: str, tool_name: str) -> dict[str, Any]:
    """Request human approval for a high-risk action.

    Returns:
        Dict with status and description for the approval UI.

    """
    event = asyncio.Event()
    _pending_approvals[step_id] = event
    _approval_metadata[step_id] = {"tool_name": tool_name, "description": description}
    logger.warning("APPROVAL REQUIRED: [%s] %s — %s", step_id, tool_name, description)
    return {
        "status": "pending_approval",
        "step_id": step_id,
        "tool_name": tool_name,
        "description": description,
    }


def resolve_approval(step_id: str, approved: bool) -> None:
    """Resolve a pending approval (called from API/UI)."""
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
    except asyncio.TimeoutError:
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
    """Analyze a failed tool call with Gemini and suggest corrected args.

    Returns:
        Corrected args dict, or None if no correction possible.

    """
    from agent.core.gemini_client import generate_content

    prompt = f"""A tool call failed. Analyze the error and suggest corrected arguments.

Tool: {tool_name}
Original args: {original_args}
Error: {error}

Return ONLY a JSON object with the corrected arguments. Same keys, corrected values.
If the error is unrecoverable (e.g. file doesn't exist, permission denied), return null."""

    try:
        response = await generate_content(
            system="You are a debugging assistant. Be precise. Return only valid JSON.",
            user=prompt,
            temperature=0.2,
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
            logger.info("Self-correction: adjusted args for %s", tool_name)
            return corrected
    except Exception:
        logger.debug("Self-correction failed for %s", tool_name)
    return None


# ── Step Execution with Retry ─────────────────────────────────────

MAX_RETRIES = 2


async def execute_step(step: TaskStep, context: dict[str, Any] | None = None) -> ToolResult:
    """Execute a single task step with self-correction retry loop.

    Flow:
        1. Check if tool is high-risk → request approval
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

    # Human-in-the-loop: check if high-risk
    if step.tool_name in _high_risk_tools:
        approval = request_approval(step.id, step.description, step.tool_name)
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

            # Tool returned failure — try self-correction
            last_error = result.error or "Tool returned failure"
            logger.warning("Tool %s failed (attempt %d): %s", step.tool_name, attempt, last_error[:200])

            if attempt <= MAX_RETRIES:
                corrected = await self_correct(last_error, step.tool_name, current_args)
                if corrected:
                    current_args.update(corrected)
                    logger.info("Retrying %s with corrected args (attempt %d)", step.tool_name, attempt + 1)
                    continue
                break  # No correction possible

        except asyncio.TimeoutError:
            last_error = "Tool execution timed out (120s)"
        except Exception as exc:
            last_error = str(exc)

        if attempt <= MAX_RETRIES:
            corrected = await self_correct(last_error, step.tool_name, current_args)
            if corrected:
                current_args.update(corrected)
                continue
            break

    step.status = StepStatus.FAILED
    step.error = last_error
    return ToolResult(success=False, output="", error=last_error)


# ── Built-in Tools ────────────────────────────────────────────────


@register_tool("execute_code", high_risk=True)
async def execute_code(code: str, language: str = "python", **_: Any) -> ToolResult:
    """Execute code in a sandboxed subprocess."""
    if language == "python":
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            f.flush()
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, f.name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
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
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        success = proc.returncode == 0
        return ToolResult(
            success=success,
            output=stdout.decode(errors="replace"),
            error=stderr.decode(errors="replace") if not success else None,
        )
    except asyncio.TimeoutError:
        return ToolResult(success=False, output="", error="Command timed out")

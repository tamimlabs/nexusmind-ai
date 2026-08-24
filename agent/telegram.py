"""Telegram bot integration for remote approvals.

Sends approval requests to user's Telegram with inline approve/deny buttons.
User can approve from their phone — agent continues autonomously.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from agent.config import settings

logger = logging.getLogger(__name__)

# Telegram Bot API base URL
TELEGRAM_API = "https://api.telegram.org/bot{token}"

# Pending approval requests — keyed by step_id
_telegram_approvals: dict[str, dict[str, Any]] = {}


def _get_api_url(method: str = "") -> str:
    """Build Telegram API URL."""
    base = TELEGRAM_API.format(token=settings.telegram_bot_token)
    return f"{base}/{method}" if method else base


def _get_chat_id() -> str | None:
    """Get configured chat ID."""
    return settings.telegram_chat_id if settings.telegram_chat_id else None


async def send_message(text: str, reply_markup: dict | None = None) -> dict[str, Any] | None:
    """Send a message to the configured Telegram chat.

    Returns:
        Telegram API response, or None if not configured.

    """
    chat_id = _get_chat_id()
    if not chat_id or not settings.telegram_bot_token:
        logger.debug("Telegram not configured — skipping message")
        return None

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_get_api_url("sendMessage"), data=payload)
            data = resp.json()
            if not data.get("ok"):
                logger.error("Telegram sendMessage failed: %s", data.get("description"))
                return None
            return data.get("result")
    except Exception:
        logger.exception("Telegram send_message failed")
        return None


async def edit_message(message_id: int, text: str) -> bool:
    """Edit an existing Telegram message."""
    chat_id = _get_chat_id()
    if not chat_id or not settings.telegram_bot_token:
        return False

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_get_api_url("editMessageText"), data=payload)
            data = resp.json()
            return data.get("ok", False)
    except Exception:
        logger.debug("Telegram edit_message failed")
        return False


async def answer_callback(callback_query_id: str, text: str = "") -> bool:
    """Acknowledge a callback query (stops the loading spinner)."""
    if not settings.telegram_bot_token:
        return False

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _get_api_url("answerCallbackQuery"),
                data={"callback_query_id": callback_query_id, "text": text},
            )
            return resp.json().get("ok", False)
    except Exception:
        return False


# ── Approval Request Flow ─────────────────────────────────────────


async def request_approval_via_telegram(
    step_id: str,
    tool_name: str,
    description: str,
    task_goal: str = "",
    extra_info: str = "",
) -> dict[str, Any]:
    """Send approval request to Telegram with approve/deny buttons.

    Returns:
        Dict with status and optional message_id for later editing.

    """
    # Build message
    msg_lines = [
        "🔐 <b>Approval Required</b>",
        "",
        f"<b>Task:</b> {task_goal[:100]}" if task_goal else "",
        f"<b>Tool:</b> <code>{tool_name}</code>",
        f"<b>Action:</b> {description[:200]}",
    ]
    if extra_info:
        msg_lines.append(f"\n<b>Details:</b> {extra_info[:200]}")
    msg_lines.extend([
        "",
        "⏱ Timeout: 5 minutes",
        f"🆔 <code>{step_id[:12]}</code>",
    ])
    msg_text = "\n".join(line for line in msg_lines if line is not None)

    # Inline keyboard with approve/deny buttons
    # Telegram callback_data max is 64 bytes — use short format
    short_id = step_id[:8]
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"approve:{short_id}"},
                {"text": "❌ Deny", "callback_data": f"deny:{short_id}"},
            ]
        ]
    }

    result = await send_message(msg_text, reply_markup)

    if result:
        _telegram_approvals[step_id] = {
            "message_id": result.get("message_id"),
            "tool_name": tool_name,
            "description": description,
            "created_at": time.time(),
        }

    return {
        "status": "sent" if result else "failed",
        "message_id": result.get("message_id") if result else None,
    }


async def handle_callback_query(callback_query: dict[str, Any]) -> None:
    """Handle a Telegram callback query (approve/deny button pressed)."""
    callback_id = callback_query.get("id", "")
    data_str = callback_query.get("data", "")

    # Parse short format: "approve:short_id" or "deny:short_id"
    if ":" not in data_str:
        await answer_callback(callback_id, "Invalid action")
        return

    action, short_id = data_str.split(":", 1)

    if action not in ("approve", "deny"):
        await answer_callback(callback_id, "Invalid action")
        return

    # Find the full step_id by matching the short prefix
    from agent.core.executor import _approval_metadata, _pending_approvals

    step_id = None
    for sid in list(_pending_approvals.keys()):
        if sid[:8] == short_id:
            step_id = sid
            break

    if not step_id:
        await answer_callback(callback_id, "Approval not found (may have expired)")
        return

    # Resolve the approval
    from agent.core.executor import resolve_approval

    approved = action == "approve"
    resolve_approval(step_id, approved)

    # Edit the original message to show result
    approval_info = _telegram_approvals.pop(step_id, {})
    msg_id = approval_info.get("message_id")
    tool_name = approval_info.get("tool_name", "")
    if msg_id:
        status = "✅ Approved" if approved else "❌ Denied"
        new_text = (
            f"{status}\n"
            f"<b>Tool:</b> <code>{tool_name}</code>\n"
            f"🆔 <code>{step_id[:12]}</code>"
        )
        await edit_message(msg_id, new_text)

    await answer_callback(callback_id, "Approved" if approved else "Denied")
    logger.info("Telegram approval %s for step %s", "granted" if approved else "denied", step_id)


# ── Telegram Bot Polling (Webhook Mode) ───────────────────────────


async def process_update(update: dict[str, Any]) -> None:
    """Process a single Telegram update (from webhook or polling)."""
    # Handle callback queries (inline button presses)
    callback = update.get("callback_query")
    if callback:
        await handle_callback_query(callback)
        return

    # Handle text commands
    message = update.get("message", {})
    text = message.get("text", "")

    if text == "/start" or text == "/help":
        await send_message(
            "🤖 <b>NexusMind AI Bot</b>\n\n"
            "I'll send you approval requests when the agent needs permission.\n\n"
            "<b>Commands:</b>\n"
            "/status — Check agent status\n"
            "/pending — Show pending approvals"
        )
    elif text == "/status":
        from agent.core.executor import list_tools
        tools = list_tools()
        await send_message(
            f"🟢 <b>Agent Online</b>\n"
            f"Model: <code>{settings.gemini_model}</code>\n"
            f"Tools: {len(tools)}\n"
            f"Approval mode: <code>{settings.approval_mode}</code>"
        )
    elif text == "/pending":
        from agent.core.executor import get_pending_approvals
        pending = get_pending_approvals()
        if not pending:
            await send_message("✅ No pending approvals")
        else:
            lines = ["📋 <b>Pending Approvals:</b>", ""]
            for p in pending:
                lines.append(f"• <code>{p['tool_name']}</code> — {p['description'][:80]}")
            await send_message("\n".join(lines))


# ── Status Notifications ──────────────────────────────────────────


async def notify_task_started(task_id: str, goal: str) -> None:
    """Notify user that a task has started."""
    await send_message(
        f"🚀 <b>Task Started</b>\n"
        f"<b>Goal:</b> {goal[:200]}\n"
        f"🆔 <code>{task_id[:12]}</code>"
    )


async def notify_task_completed(task_id: str, goal: str, result: str) -> None:
    """Notify user that a task completed."""
    await send_message(
        f"✅ <b>Task Completed</b>\n"
        f"<b>Goal:</b> {goal[:200]}\n"
        f"<b>Result:</b> {result[:300]}\n"
        f"🆔 <code>{task_id[:12]}</code>"
    )


async def notify_task_failed(task_id: str, goal: str, error: str) -> None:
    """Notify user that a task failed."""
    await send_message(
        f"❌ <b>Task Failed</b>\n"
        f"<b>Goal:</b> {goal[:200]}\n"
        f"<b>Error:</b> {error[:300]}\n"
        f"🆔 <code>{task_id[:12]}</code>"
    )


def is_configured() -> bool:
    """Check if Telegram bot is properly configured."""
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def get_config_status() -> dict[str, Any]:
    """Get Telegram configuration status."""
    return {
        "configured": is_configured(),
        "has_token": bool(settings.telegram_bot_token),
        "has_chat_id": bool(settings.telegram_chat_id),
        "chat_id": settings.telegram_chat_id or "",
    }

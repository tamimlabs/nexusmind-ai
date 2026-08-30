"""Slack skill — post messages and threaded replies via Slack Web API.

Tools:
- slack_send_message: post a message to a channel
- slack_reply_thread: post a threaded reply

Auth: SLACK_BOT_TOKEN (xoxb-...) from settings, env, or project .env.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

from agent.core.executor import register_tool
from agent.models import ToolResult

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api/chat.postMessage"


def _token_from_project_env_file() -> str:
    """Last-resort: parse SLACK_BOT_TOKEN straight from the project root .env."""
    try:
        from agent.config import _PROJECT_ROOT

        env_file = Path(_PROJECT_ROOT) / ".env"
    except Exception:
        env_file = Path(__file__).resolve().parents[3] / ".env"
    try:
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("SLACK_BOT_TOKEN=") or line.startswith("SLACK_BOT_TOKEN ="):
                    _, _, value = line.partition("=")
                    return value.strip().strip("'\"")
    except Exception:
        logger.debug("Could not parse project .env for SLACK_BOT_TOKEN")
    return ""


def _token() -> str:
    """Return Slack bot token: settings/env first, project .env file as fallback."""
    try:
        from agent.config import settings

        token = getattr(settings, "slack_bot_token", "")
        if token:
            return token
    except Exception:
        pass

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if token:
        return token

    return _token_from_project_env_file()


async def _post(payload: dict[str, Any]) -> ToolResult:
    """POST payload to Slack chat.postMessage and map response to ToolResult."""
    token = _token()
    if not token:
        return ToolResult(
            success=False,
            output="",
            error="Slack token not configured. Set SLACK_BOT_TOKEN in .env or environment.",
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(SLACK_API, headers=headers, json=payload)
            try:
                data = resp.json()
            except Exception:
                data = {"ok": False, "error": resp.text[:500]}

            if resp.status_code == 200 and data.get("ok"):
                ts = data.get("ts", "")
                channel = data.get("channel", payload.get("channel", ""))
                return ToolResult(
                    success=True,
                    output=f"Message posted to {channel} (ts: {ts})",
                    metadata={"ts": ts, "channel": channel},
                )

            err = data.get("error", f"HTTP {resp.status_code}")
            logger.warning("Slack API error [%s]: %s", resp.status_code, err)
            return ToolResult(success=False, output="", error=f"Slack API error: {err}")

    except httpx.TimeoutException:
        return ToolResult(success=False, output="", error="Slack API request timed out")
    except Exception as exc:
        logger.exception("Slack post failed")
        return ToolResult(success=False, output="", error=f"Slack request failed: {exc}")


@register_tool("slack_send_message")
async def slack_send_message(channel: str, text: str, **_: Any) -> ToolResult:
    """Post a message to a Slack channel.

    Args:
        channel: Channel ID or name (e.g. C1234567890 or #general).
        text: Message text (supports mrkdwn).
    """
    if not channel or not text:
        return ToolResult(success=False, output="", error="channel and text are required")
    logger.info("Slack send_message to %s (%d chars)", channel, len(text))
    return await _post({"channel": channel, "text": text})


@register_tool("slack_reply_thread")
async def slack_reply_thread(channel: str, thread_ts: str, text: str, **_: Any) -> ToolResult:
    """Post a threaded reply to a Slack message.

    Args:
        channel: Channel ID containing the parent message.
        thread_ts: Timestamp (ts) of the parent message to reply to.
        text: Reply text.
    """
    if not channel or not thread_ts or not text:
        return ToolResult(success=False, output="", error="channel, thread_ts and text are required")
    logger.info("Slack reply_thread to %s thread %s (%d chars)", channel, thread_ts, len(text))
    return await _post({"channel": channel, "thread_ts": thread_ts, "text": text})

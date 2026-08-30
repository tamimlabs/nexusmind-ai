"""Discord skill — send messages via the Discord Bot API."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from agent.core.executor import register_tool
from agent.models import ToolResult

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"


def _token_from_project_env_file() -> str:
    """Last-resort: parse DISCORD_BOT_TOKEN straight from the project root .env."""
    try:
        from agent.config import _PROJECT_ROOT

        env_file = Path(_PROJECT_ROOT) / ".env"
    except Exception:
        env_file = Path(__file__).resolve().parents[3] / ".env"
    try:
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("DISCORD_BOT_TOKEN=") or line.startswith("DISCORD_BOT_TOKEN ="):
                    _, _, value = line.partition("=")
                    return value.strip().strip("'\"")
    except Exception:
        logger.debug("Could not parse project .env for DISCORD_BOT_TOKEN")
    return ""


def _token() -> str:
    """Return Discord bot token: settings/env first, project .env file as fallback."""
    try:
        from agent.config import settings

        tok = getattr(settings, "discord_bot_token", "")
        if tok:
            return tok
    except Exception:
        pass

    import os

    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if token:
        return token

    return _token_from_project_env_file()


@register_tool("discord_send_message")
async def discord_send_message(channel_id: str, content: str, **_: Any) -> ToolResult:
    """Send a message to a Discord channel.

    Args:
        channel_id: Discord channel ID (snowflake).
        content: Message text (max 2000 chars).
    """
    token = _token()
    if not token:
        return ToolResult(
            success=False,
            output="",
            error="DISCORD_BOT_TOKEN not set. Add it to .env or environment.",
        )
    if not channel_id or not str(channel_id).strip():
        return ToolResult(success=False, output="", error="channel_id is required")
    if not content or not str(content).strip():
        return ToolResult(success=False, output="", error="content is required")

    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {"content": str(content)[:2000]}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code in (200, 201):
            try:
                data = resp.json()
            except Exception:
                data = {}
            msg_id = data.get("id", "") if isinstance(data, dict) else ""
            return ToolResult(success=True, output=f"Message sent to {channel_id} (id {msg_id})", metadata={"id": msg_id})
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        return ToolResult(success=False, output="", error=f"Discord API error [{resp.status_code}]: {body}")
    except Exception as exc:
        return ToolResult(success=False, output="", error=f"Discord request failed: {exc}")

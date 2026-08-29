"""Credentials API — manage all API keys and secrets in one place.

Reads/writes .env file safely. Never exposes full secrets to frontend.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.config import _ENV_FILE

router = APIRouter(prefix="/api/credentials", tags=["credentials"])

# All supported credential fields grouped by category
CREDENTIAL_FIELDS: dict[str, list[dict[str, Any]]] = {
    "AI & LLM": [
        {"key": "GEMINI_API_KEY", "label": "Gemini API Keys", "placeholder": "key1,key2,key3 (comma-separated)", "secret": True, "multi": True},
        {"key": "GEMINI_MODEL", "label": "Gemini Model", "placeholder": "gemini-3.5-flash", "secret": False},
    ],
    "Google Cloud": [
        {"key": "GOOGLE_CLOUD_PROJECT", "label": "Project ID", "placeholder": "my-gcp-project", "secret": False},
        {"key": "GOOGLE_CLOUD_REGION", "label": "Region", "placeholder": "us-central1", "secret": False},
    ],
    "Web Search": [
        {"key": "GOOGLE_SEARCH_API_KEY", "label": "Google Search API Key", "placeholder": "AIza...", "secret": True},
        {"key": "GOOGLE_SEARCH_CX", "label": "Google Search CX", "placeholder": "a1b2c3d4e5", "secret": False},
    ],
    "Telegram (Remote Approvals)": [
        {"key": "TELEGRAM_BOT_TOKEN", "label": "Bot Token", "placeholder": "123456:ABC-DEF...", "secret": True},
        {"key": "TELEGRAM_CHAT_ID", "label": "Chat ID", "placeholder": "Your Telegram user ID", "secret": False},
    ],
    "GitHub": [
        {"key": "GITHUB_TOKEN", "label": "Personal Access Token", "placeholder": "ghp_...", "secret": True},
    ],
    "GitLab": [
        {"key": "GITLAB_TOKEN", "label": "Personal Access Token", "placeholder": "glpat-...", "secret": True},
        {"key": "GITLAB_BASE_URL", "label": "Base URL", "placeholder": "https://gitlab.com", "secret": False},
    ],
    "Slack": [
        {"key": "SLACK_BOT_TOKEN", "label": "Bot Token", "placeholder": "xoxb-...", "secret": True},
    ],
    "Discord": [
        {"key": "DISCORD_BOT_TOKEN", "label": "Bot Token", "placeholder": "MTIz...", "secret": True},
    ],
    "Jira": [
        {"key": "JIRA_DOMAIN", "label": "Domain", "placeholder": "company.atlassian.net", "secret": False},
        {"key": "JIRA_EMAIL", "label": "Email", "placeholder": "you@company.com", "secret": False},
        {"key": "JIRA_TOKEN", "label": "API Token", "placeholder": "", "secret": True},
    ],
    "Email (IMAP)": [
        {"key": "EMAIL_IMAP_SERVER", "label": "IMAP Server", "placeholder": "imap.gmail.com", "secret": False},
        {"key": "EMAIL_ADDRESS", "label": "Email Address", "placeholder": "you@gmail.com", "secret": False},
        {"key": "EMAIL_PASSWORD", "label": "App Password", "placeholder": "", "secret": True},
    ],
}


class SaveCredentialsRequest(BaseModel):
    credentials: dict[str, str]  # key -> value


def _read_env() -> dict[str, str]:
    """Read .env file into a dict."""
    env: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                # Strip optional quotes (both old and new format)
                value = value.strip().strip('"').strip("'")
                env[key.strip()] = value
    return env


def _write_env(data: dict[str, str]) -> None:
    """Write dict to .env file, preserving comments and order."""
    lines: list[str] = []
    if _ENV_FILE.exists():
        lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()

    existing_keys: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, _ = stripped.partition("=")
            key = key.strip()
            existing_keys.add(key)
            if key in data:
                # Don't wrap in quotes — raw value preserves correctly
                new_lines.append(f'{key}={data[key]}')
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    # Add new keys that weren't in the file
    for key, value in data.items():
        if key not in existing_keys and value:
            new_lines.append(f'{key}={value}')

    _ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _mask(value: str) -> str:
    """Mask a secret value, showing only last 4 chars."""
    if not value or len(value) <= 8:
        return "****" if value else ""
    return "*" * (len(value) - 4) + value[-4:]


@router.get("")
async def list_credentials():
    """List all credential fields with masked values."""
    env = _read_env()
    result: dict[str, list[dict[str, Any]]] = {}

    for category, fields in CREDENTIAL_FIELDS.items():
        result[category] = []
        for field in fields:
            raw_value = env.get(field["key"], "")
            result[category].append({
                "key": field["key"],
                "label": field["label"],
                "placeholder": field["placeholder"],
                "secret": field["secret"],
                "multi": field.get("multi", False),
                "configured": bool(raw_value),
                "value": _mask(raw_value) if field["secret"] else raw_value,
            })

    return result


@router.get("/{key}")
async def get_credential(key: str):
    """Get a single credential value (masked if secret)."""
    env = _read_env()
    value = env.get(key, "")

    # Find field info
    field_info = None
    for fields in CREDENTIAL_FIELDS.values():
        for f in fields:
            if f["key"] == key:
                field_info = f
                break

    if not field_info:
        raise HTTPException(status_code=404, detail=f"Unknown credential: {key}")

    return {
        "key": key,
        "label": field_info["label"],
        "secret": field_info["secret"],
        "configured": bool(value),
        "value": _mask(value) if field_info["secret"] else value,
    }


@router.post("")
async def save_credentials(req: SaveCredentialsRequest):
    """Save credentials to .env file."""
    # Filter out empty values and masked values (unchanged)
    to_save: dict[str, str] = {}
    for key, value in req.credentials.items():
        if value and not value.startswith("*"):
            to_save[key] = value

    if to_save:
        _write_env(to_save)
        os.environ.update(to_save)
        # Reload the shared settings singleton IN PLACE so every module
        # (telegram, gemini_client, api.main) sees the new values immediately —
        # and persisted to the project-root .env, not the process CWD.
        import agent.config
        agent.config.reload_settings()

    saved = list(to_save.keys())
    return {"saved": saved, "count": len(saved)}


@router.delete("/{key}")
async def delete_credential(key: str):
    """Remove a credential from .env file."""
    if not _ENV_FILE.exists():
        raise HTTPException(status_code=404, detail="No .env file found")

    lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()
    new_lines = [line for line in lines if not line.strip().startswith(f"{key}=")]
    _ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    # Remove from os.environ and refresh running config
    os.environ.pop(key, None)
    import agent.config
    agent.config.reload_settings()

    return {"deleted": key}

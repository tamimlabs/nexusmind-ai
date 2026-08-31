"""Watcher manager - lifecycle management for all active watchers.

Persists watcher state to survive restarts.

Supported platforms:
    github, gitlab, slack, discord, jira, reddit,
    hackernews, email, rss, cron, webhook (custom)
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent.watchers.cron import CronWatcher
from agent.watchers.discord import DiscordWatcher
from agent.watchers.email_watcher import EmailWatcher
from agent.watchers.github import GitHubWatcher
from agent.watchers.gitlab import GitLabWatcher
from agent.watchers.hackernews import HackerNewsWatcher
from agent.watchers.jira import JiraWatcher
from agent.watchers.reddit import RedditWatcher
from agent.watchers.rss import RSSWatcher
from agent.watchers.slack import SlackWatcher
from agent.watchers.webhook import WebhookWatcher

if TYPE_CHECKING:
    from agent.watchers.base import BaseWatcher

logger = logging.getLogger(__name__)

# Watcher registry — all supported platforms
_WATCHER_TYPES: dict[str, type[BaseWatcher]] = {
    "github": GitHubWatcher,
    "gitlab": GitLabWatcher,
    "slack": SlackWatcher,
    "discord": DiscordWatcher,
    "jira": JiraWatcher,
    "reddit": RedditWatcher,
    "hackernews": HackerNewsWatcher,
    "email": EmailWatcher,
    "rss": RSSWatcher,
    "cron": CronWatcher,
    "webhook": WebhookWatcher,
}

# Project root for absolute path resolution
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Active watchers
_active_watchers: dict[str, BaseWatcher] = {}
_state_file = _PROJECT_ROOT / "data" / "watcher_state.json"


def _firestore_state_store():
    """Firestore watcher-state store, or None when not configured/available.

    Cloud Run's filesystem is ephemeral (wiped on scale-to-zero), so the
    Firestore backend keeps watchers alive across restarts. Falls back to the
    JSON file when Firestore isn't the configured backend.
    """
    try:
        from agent.config import settings

        if settings.database_backend.lower() != "firestore":
            return None
        from cloud.firestore.client import FirestoreWatcherStateStore

        return FirestoreWatcherStateStore()
    except Exception as exc:
        logger.warning("Firestore watcher state unavailable, using file: %s", exc)
        return None


def _load_state() -> dict[str, Any]:
    """Load persisted watcher state (Firestore first, then JSON file)."""
    store = _firestore_state_store()
    if store is not None:
        try:
            return store.load_all()
        except Exception:
            logger.exception("Failed to load watcher state from Firestore")
    if _state_file.exists():
        try:
            data: dict[str, Any] = json.loads(_state_file.read_text())
            return data
        except Exception:
            pass
    return {}


_SENSITIVE_KEYS = {
    "token",
    "password",
    "secret",
    "api_key",
    "telegram_bot_token",
    "telegram_token",
    "email",
    "imap_password",
}


def _sanitize_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of config with sensitive keys stripped."""
    return {k: v for k, v in config.items() if k not in _SENSITIVE_KEYS}


def _rehydrate_config(watcher_type: str, config: dict[str, Any]) -> dict[str, Any]:
    """Fill missing sensitive keys from global settings / .env for restored watchers.

    Legacy state files had secrets stripped via _sanitize_config; without this
    rehydration every watcher would silently lose auth after restart/scale-to-zero.
    """
    # Copy to avoid mutating caller's dict in place unexpectedly
    cfg = dict(config)
    # GitHub token fallback (most common)
    if watcher_type == "github" and not cfg.get("token"):
        try:
            from agent.config import settings as _s

            if getattr(_s, "github_token", ""):
                cfg["token"] = _s.github_token
            else:
                # Direct .env parse as last resort (mirrors github._resolve_github_token)
                envf = _PROJECT_ROOT / ".env"
                if envf.exists():
                    for line in envf.read_text(encoding="utf-8").splitlines():
                        if line.strip().startswith("GITHUB_TOKEN="):
                            cfg["token"] = line.partition("=")[2].strip().strip("'\"")
                            break
        except Exception:
            pass
    # Precise rehydration: only watcher-specific env keys to avoid cross-contamination
    import os as _os

    # Explicit per-watcher env mapping to avoid generic API_KEY leaking to wrong watcher
    _WATCHER_ENV_MAP: dict[str, dict[str, str]] = {
        "github": {"token": "GITHUB_TOKEN"},
        "gitlab": {"token": "GITLAB_TOKEN"},
        "slack": {"token": "SLACK_BOT_TOKEN"},
        "discord": {"token": "DISCORD_BOT_TOKEN"},
        "jira": {"token": "JIRA_TOKEN", "api_key": "JIRA_TOKEN", "secret": "JIRA_TOKEN"},
        "email": {
            "password": "EMAIL_IMAP_PASSWORD",
            "imap_password": "EMAIL_IMAP_PASSWORD",
            "email": "EMAIL_ADDRESS",
            "token": "EMAIL_IMAP_PASSWORD",
        },
        "reddit": {"secret": "REDDIT_CLIENT_SECRET", "api_key": "REDDIT_CLIENT_ID"},
        "rss": {},
        "cron": {},
        "webhook": {"secret": "WEBHOOK_SECRET", "token": "WEBHOOK_TOKEN"},
        "hackernews": {},
    }
    watcher_map = _WATCHER_ENV_MAP.get(watcher_type, {})
    for k in _SENSITIVE_KEYS:
        if cfg.get(k):
            continue
        # Prefer explicit mapping, fallback to watcher-specific upper only (no generic)
        explicit = watcher_map.get(k)
        candidates: list[str] = []
        if explicit:
            candidates.append(explicit)
        candidates.append(f"{watcher_type.upper()}_{k.upper()}")
        if k == "token":
            candidates.append(f"{watcher_type.upper()}_TOKEN")
        for ck in candidates:
            val = _os.environ.get(ck, "")
            if val:
                cfg[k] = val
                break
    return cfg


def _save_state() -> None:
    """Persist watcher state (Firestore first, then JSON file).

    Full config (including secrets) is persisted so restore can re-auth.
    Secrets are stripped only for API responses via _sanitize_config/list_watchers.
    The state file lives under data/ (gitignored) and Firestore has IAM ACLs.
    """
    state = {}
    for wid, watcher in _active_watchers.items():
        state[wid] = {
            "type": watcher.config.get("type", "unknown"),
            "config": dict(watcher.config),
            "status": watcher.get_status(),
        }
    store = _firestore_state_store()
    if store is not None:
        try:
            store.save_all(state)
            return
        except Exception:
            logger.exception("Failed to save watcher state to Firestore")
    _state_file.parent.mkdir(parents=True, exist_ok=True)
    _state_file.write_text(json.dumps(state, indent=2))


def create_watcher(watcher_type: str, config: dict[str, Any], persist: bool = True) -> BaseWatcher:
    """Create a new watcher instance."""
    watcher_class = _WATCHER_TYPES.get(watcher_type)
    if not watcher_class:
        raise ValueError(
            f"Unknown watcher type: {watcher_type}. Available: {list(_WATCHER_TYPES.keys())}"
        )

    watcher_id = config.get("id")
    if not watcher_id:
        watcher_id = f"{watcher_type}_{uuid.uuid4().hex[:6]}"
        while watcher_id in _active_watchers:
            watcher_id = f"{watcher_type}_{uuid.uuid4().hex[:6]}"
    # Guard against collision even when id is explicitly provided
    if watcher_id in _active_watchers:
        base = watcher_id
        watcher_id = f"{base}_{uuid.uuid4().hex[:6]}"
        while watcher_id in _active_watchers:
            watcher_id = f"{base}_{uuid.uuid4().hex[:6]}"
    watcher = watcher_class(watcher_id, config)
    _active_watchers[watcher_id] = watcher
    if persist:
        _save_state()
    return watcher


async def start_watcher(watcher_id: str) -> bool:
    """Start a watcher by ID."""
    watcher = _active_watchers.get(watcher_id)
    if watcher:
        await watcher.start()
        _save_state()
        return True
    return False


async def stop_watcher(watcher_id: str) -> bool:
    """Stop a watcher by ID."""
    watcher = _active_watchers.get(watcher_id)
    if watcher:
        await watcher.stop()
        _save_state()
        return True
    return False


async def remove_watcher(watcher_id: str) -> bool:
    """Stop and remove a watcher."""
    watcher = _active_watchers.pop(watcher_id, None)
    if watcher:
        await watcher.stop()
        _save_state()
        return True
    return False


def list_watchers() -> list[dict[str, Any]]:
    """List all watchers and their status."""
    return [
        {**watcher.get_status(), "type": watcher.config.get("type", "unknown")}
        for watcher in _active_watchers.values()
    ]


def get_watcher(watcher_id: str) -> BaseWatcher | None:
    return _active_watchers.get(watcher_id)


async def restore_watchers() -> None:
    """Restore watchers from persisted state on startup."""
    state = _load_state()
    for wid, data in state.items():
        try:
            watcher_type = data.get("type", "unknown")
            config = data.get("config", {}) or {}
            config = _rehydrate_config(watcher_type, config)
            config["id"] = wid
            watcher_class = _WATCHER_TYPES.get(watcher_type)
            if watcher_class is None:
                logger.warning("Unknown watcher type '%s' for %s — skipping", watcher_type, wid)
                continue
            watcher = watcher_class(wid, config)
            _active_watchers[wid] = watcher
            # Restore in-memory dedup state so already-handled events are not
            # re-triggered after a restart. Must happen BEFORE any _save_state()
            # so persisted state is not overwritten with empty values.
            status = data.get("status") or {}
            if isinstance(status, dict):
                watcher._state = dict(status.get("state", {}) or {})
                watcher._events_processed = int(status.get("events_processed", 0) or 0)
                last_check_str = status.get("last_check")
                if last_check_str:
                    try:
                        from datetime import datetime

                        watcher._last_check = datetime.fromisoformat(last_check_str)
                    except Exception:
                        pass
            await watcher.start()
            logger.info("Restored watcher: %s", wid)
        except Exception as e:
            logger.warning("Failed to restore watcher %s: %s", wid, e)

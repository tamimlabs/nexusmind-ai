"""Global configuration using pydantic-settings.

Environment variables are loaded from .env file and system environment.
The .env file is resolved relative to the project root (this file's parent
directory), NOT the current working directory, so the server works no matter
where it is launched from.
"""

import os
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Single source of truth for the .env path. Everything that reads/writes .env
# MUST use this — CWD-relative Path(".env") breaks on new machines where the
# server is launched from a different directory (credentials get written to a
# .env that Settings never reads).
_ENV_FILE = _PROJECT_ROOT / ".env"


def _load_dotenv() -> None:
    """Load .env file into os.environ so all modules can access env vars."""
    for candidate in (_PROJECT_ROOT / ".env", Path(".env")):
        env_file = candidate
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                if key not in os.environ:
                    os.environ[key] = value
        break


_load_dotenv()

# Selectable Gemini request-rate tiers shown in the dashboard "Rate Limit"
# card. Each key maps to the client-side RPS/RPM the agent throttles itself
# to (a value of 0 disables that bound). "free" matches the Gemini free tier.
RATE_LIMIT_PRESETS: dict[str, dict[str, float | int]] = {
    # Gemini free tier: 1 request/sec, 15 requests/min (burst-safe).
    "free": {"rps": 1, "rpm": 15},
    # Paid standard tier: comfortable headroom without melting the bill.
    "standard": {"rps": 10, "rpm": 100},
    # No client-side throttling — trust the paid tier's own limits.
    "unlimited": {"rps": 0, "rpm": 0},
}


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # --- Project ---
    project_name: str = "nexusmind-ai"
    environment: str = "development"
    debug: bool = False

    # --- Google Cloud ---
    google_cloud_project: str = ""
    google_cloud_region: str = "us-central1"

    # --- Storage Backend ---
    # "sqlite"   = local SQLite (default, zero-config, full features)
    # "firestore" = Google Cloud Firestore (for Cloud Run deployments)
    database_backend: str = "sqlite"

    # --- Gemini / Vertex AI ---
    gemini_model: str = "gemini-3.5-flash"
    # Stronger fallback model: when the primary model hits its max output
    # tokens mid-reply, generation is transparently retried here so the agent
    # CONTINUES instead of stopping on an unfinished step.
    gemini_model_pro: str = "gemini-3.5-pro"
    gemini_api_key: str = ""
    # When True, Gemini controls tool selection, file naming and memory policy.
    # Deterministic heuristics remain only as fallback/validator.
    gemini_full_control: bool = True

    # Client-side request-rate gate: the agent self-limits to your Gemini
    # tier's RPS/RPM so the free tier's request-per-minute limits are never
    # exceeded by its own bursty loop (tasks would otherwise 429 mid-run).
    # A value of 0 disables that bound. Changeable live from the dashboard.
    gemini_rps: float = 1.0
    gemini_rpm: int = 15

    @field_validator("gemini_rps", mode="before")
    @classmethod
    def _clamp_rps(cls, v: object) -> float:
        try:
            val = float(v)
        except (TypeError, ValueError):
            return 1.0
        return min(max(val, 0.0), 100.0)

    @field_validator("gemini_rpm", mode="before")
    @classmethod
    def _clamp_rpm(cls, v: object) -> int:
        try:
            val = int(v)
        except (TypeError, ValueError):
            return 15
        return min(max(val, 0), 10000)

    # --- Firestore ---
    firestore_collection_tasks: str = "tasks"
    firestore_collection_memory: str = "agent_memory"
    firestore_collection_skills: str = "learned_skills"

    # --- Pub/Sub ---
    pubsub_topic_tasks: str = "nexusmind-tasks"
    pubsub_subscription_tasks: str = "nexusmind-tasks-sub"
    pubsub_topic_events: str = "nexusmind-events"

    # --- Agent ---
    agent_max_steps: int = 20
    agent_max_retries: int = 3
    agent_timeout_seconds: int = 300
    agent_memory_max_items: int = 1000

    # --- Approval Mode ---
    # "always" / "ask_everytime" = ask for every high-risk tool (safe but annoying)
    # "smart"  = auto-approve safe commands, ask only for dangerous ones (recommended)
    # "never"  = auto-approve everything (risky but fast)
    approval_mode: str = "smart"

    @field_validator("approval_mode", mode="before")
    @classmethod
    def _normalize_approval_mode(cls, v: object) -> str:
        """Accept aliases like 'ask_everytime', 'Ask Everytime', 'everytime'."""
        if not isinstance(v, str):
            return "smart"
        raw = v.strip().lower().replace(" ", "_").replace("-", "_")
        # Map all ask-everytime variants to canonical "always"
        if raw in {"always", "ask", "ask_everytime", "everytime", "ask_every_time", "always_ask"}:
            return "always"
        if raw in {"smart", "auto", "intelligent"}:
            return "smart"
        if raw in {"never", "none", "no_ask", "disabled", "off"}:
            return "never"
        # Unknown -> fall back to smart (safe default keeps agent useful)
        return "smart" if raw not in {"always", "smart", "never"} else raw

    # --- Telegram Bot (for remote approvals) ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_approval_timeout: int = 300  # 5 minutes before timeout

    # --- Watchers (event-driven mode) ---
    watcher_default_interval: int = 300  # 5 min between event checks
    watcher_max_concurrent: int = 10

    # --- GitHub ---
    github_token: str = ""
    # Default repository (owner/name) used when a goal says "my repository"
    # and no git remote is available. Example: "tamimlabs/nexusmind-ai"
    github_default_repo: str = ""

    # --- Google Custom Search ---
    google_search_api_key: str = ""
    google_search_cx: str = ""

    # --- API ---
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    model_config = {
        "env_file": str(_PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()


def reload_settings() -> None:
    """Re-sync the shared ``settings`` singleton from .env + os.environ IN PLACE.

    Credentials saved at runtime must take effect immediately. Modules that did
    ``from agent.config import settings`` at import time (telegram,
    gemini_client, api.main) hold a reference to THIS object, so mutating it
    propagates everywhere — reassigning the module global would leave them
    staring at the stale pre-save config.

    Also refreshes cached snapshots (Gemini key rotator) so newly added API
    keys are usable without a server restart.
    """
    fresh = Settings()
    for field_name in type(fresh).model_fields:
        try:
            setattr(settings, field_name, getattr(fresh, field_name))
        except Exception:
            # Non-assertable private/legacy fields are fine to skip
            continue
    try:
        from agent.core.gemini_client import rotator

        rotator.refresh()
    except Exception:
        pass

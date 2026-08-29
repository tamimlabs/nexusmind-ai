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
    gemini_api_key: str = ""
    # When True, Gemini controls tool selection, file naming and memory policy.
    # Deterministic heuristics remain only as fallback/validator.
    gemini_full_control: bool = True

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

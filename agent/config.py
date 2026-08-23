"""Global configuration using pydantic-settings.

Environment variables are loaded from .env file and system environment.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # --- Project ---
    project_name: str = "nexusmind-ai"
    environment: str = "development"
    debug: bool = False

    # --- Google Cloud ---
    google_cloud_project: str = ""
    google_cloud_region: str = "us-central1"

    # --- Gemini / Vertex AI ---
    gemini_model: str = "gemini-3.6-flash"
    gemini_api_key: str = ""

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
    # "always" = ask for every high-risk tool (default, safe but annoying)
    # "smart"  = auto-approve safe commands, ask only for dangerous ones (recommended)
    # "never"  = auto-approve everything (risky but fast)
    approval_mode: str = "smart"

    # --- Telegram Bot (for remote approvals) ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_approval_timeout: int = 300  # 5 minutes before timeout

    # --- Watchers (event-driven mode) ---
    watcher_default_interval: int = 300  # 5 min between event checks
    watcher_max_concurrent: int = 10

    # --- GitHub ---
    github_token: str = ""

    # --- API ---
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()

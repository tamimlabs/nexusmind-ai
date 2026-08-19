"""NexusMind AI — Next-generation autonomous AI agent."""

__version__ = "0.1.0"


def _init() -> None:
    """Auto-load all skills on first import."""
    from agent.skills.loader import load_all_skills

    load_all_skills()


_init()

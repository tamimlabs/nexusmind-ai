"""Skill loader — auto-discovers and imports all skill modules at startup.

Adapted from Hermes' AST-based tool discovery pattern.
Skills self-register via @register_tool decorator when their module is imported.
"""

from __future__ import annotations

import contextlib
import importlib
import logging

logger = logging.getLogger(__name__)

_SKILL_PACKAGES = [
    "agent.skills.web_research",
    "agent.skills.file_management",
    "agent.skills.data_processing",
    "agent.skills.github",
    "agent.skills.slack",
    "agent.skills.discord",
    "agent.skills.jira",
    "agent.skills.gitlab",
    "agent.skills.email",
]

_loaded = False


def load_all_skills() -> list[str]:
    """Import all skill modules so their tools get registered.

    Returns:
        List of loaded skill package names.

    """
    global _loaded
    if _loaded:
        return _SKILL_PACKAGES

    loaded = []
    for package in _SKILL_PACKAGES:
        try:
            importlib.import_module(package)
            # Also import the skill.py submodule if it exists
            with contextlib.suppress(ImportError):
                importlib.import_module(f"{package}.skill")
            loaded.append(package)
            logger.info("Loaded skill: %s", package)
        except Exception:
            logger.exception("Failed to load skill: %s", package)

    _loaded = True
    logger.info("Loaded %d skill packages", len(loaded))
    return loaded

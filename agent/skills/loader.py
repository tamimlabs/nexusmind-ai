"""Skill loader — auto-discovers and imports all skill modules at startup.

Adapted from Hermes' AST-based tool discovery pattern.
Skills self-register via @register_tool decorator when their module is imported.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SKILL_PACKAGES = [
    "agent.skills.web_research",
    "agent.skills.file_management",
    "agent.skills.data_processing",
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
            try:
                importlib.import_module(f"{package}.skill")
            except ImportError:
                pass
            loaded.append(package)
            logger.info("Loaded skill: %s", package)
        except Exception:
            logger.exception("Failed to load skill: %s", package)

    _loaded = True
    logger.info("Loaded %d skill packages", len(loaded))
    return loaded

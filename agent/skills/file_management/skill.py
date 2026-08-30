"""File management skill — read, write, list files."""

from __future__ import annotations

import os
import re
from pathlib import Path

from agent.core.executor import ToolResult, register_tool

_SHELL_META_RE = re.compile(r"[;|`&$><\n\r]")

# Sandbox: restrict file operations to project root or output/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_OUTPUT_DIR = _PROJECT_ROOT / "output"


def _sandbox_path(path: str) -> Path:
    """Resolve and validate path is within project root."""
    if _SHELL_META_RE.search(path):
        raise PermissionError(f"Access denied: {path} contains shell metacharacters")
    p = Path(path)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    resolved = p.resolve()
    project_resolved = _PROJECT_ROOT.resolve()
    try:
        if resolved.is_relative_to(project_resolved):
            return resolved
    except AttributeError:
        # Python <3.9 fallback: ensure separator boundary
        if str(resolved) == str(project_resolved) or str(resolved).startswith(str(project_resolved) + os.sep):
            return resolved
    raise PermissionError(f"Access denied: {path} is outside project directory")


# Sandbox roots: loose paths land in output/; multi-file projects go to
# projects/<name>/ so each build gets its own folder.
_ALLOWED_ROOTS = ("output", "projects")


@register_tool("read_file")
async def read_file(path: str, encoding: str = "utf-8", **_) -> ToolResult:
    """Read a file and return its contents (sandboxed to project directory)."""
    try:
        p = _sandbox_path(path)
        if not p.exists():
            return ToolResult(success=False, output="", error=f"File not found: {path}")
        if not p.is_file():
            return ToolResult(success=False, output="", error=f"Not a file: {path}")

        content = p.read_text(encoding=encoding)
        return ToolResult(
            success=True,
            output=content[:10000],
            metadata={"size": p.stat().st_size, "path": str(p.absolute())},
        )
    except PermissionError as exc:
        return ToolResult(success=False, output="", error=str(exc))
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))


@register_tool("write_file")
async def write_file(path: str, content: str, encoding: str = "utf-8", **_) -> ToolResult:
    """Write a file (creates parent dirs).

    Paths starting with ``projects/`` keep their location so multi-file
    projects get one folder per build; anything else defaults to ``output/``.
    All writes are sandboxed to ``output/`` or ``projects/`` under project root.
    """
    try:
        raw = (path or "").strip()
        if not raw:
            return ToolResult(success=False, output="", error="Path must not be empty")
        # Reject absolute paths, traversal, and shell metachars before any join
        if _SHELL_META_RE.search(raw):
            return ToolResult(success=False, output="", error=f"Access denied: {path} contains shell metacharacters")
        p_in = Path(raw)
        if p_in.is_absolute():
            return ToolResult(success=False, output="", error=f"Access denied: {path} is outside project directory")
        if ".." in p_in.parts:
            return ToolResult(success=False, output="", error=f"Access denied: {path} contains traversal")

        # Normalize: bare paths default to output/
        if not (p_in.parts and p_in.parts[0] in _ALLOWED_ROOTS):
            p_in = Path("output") / p_in
        # Re-check traversal after join (e.g. "output/../etc")
        if ".." in Path(p_in).parts:
            return ToolResult(success=False, output="", error=f"Access denied: {path} contains traversal")

        # Resolve against CWD (so pytest tmp_path isolation works) but enforce sandbox
        # Production CWD is project root; tests chdir to tmp_path (under system temp).
        # Harden: CWD trust only if CWD is project_root or inside system temp/pytest
        import tempfile as _tf
        base = Path.cwd().resolve()
        project_resolved = _PROJECT_ROOT.resolve()
        # Determine if CWD is a trusted temp location (for tests)
        temp_root = Path(_tf.gettempdir()).resolve()
        try:
            is_temp_cwd = base.is_relative_to(temp_root)
        except AttributeError:
            is_temp_cwd = str(base).startswith(str(temp_root))
        is_temp_cwd = is_temp_cwd or "pytest" in str(base).lower()
        # If CWD is not project_root and not temp, ignore CWD and use project_root only
        if base != project_resolved and not is_temp_cwd:
            target = (project_resolved / p_in).resolve()
            in_cwd = False
        else:
            target = (Path.cwd() / p_in).resolve()
            try:
                in_cwd = target.is_relative_to(base)
            except AttributeError:
                in_cwd = str(target).startswith(str(base))
        try:
            in_project = target.is_relative_to(project_resolved)
        except AttributeError:
            in_project = str(target).startswith(str(project_resolved))
        if not (in_cwd or in_project):
            return ToolResult(success=False, output="", error=f"Access denied: {path} is outside project directory")
        # Additionally enforce that writes land inside output/ or projects/ (relative part)
        if not (p_in.parts and p_in.parts[0] in _ALLOWED_ROOTS):
            return ToolResult(success=False, output="", error=f"Access denied: {path} is outside project directory")

        p = target
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=encoding)
        return ToolResult(
            success=True,
            output=f"Written {len(content)} chars to {p}",
            metadata={"path": str(p.absolute())},
        )
    except PermissionError as exc:
        return ToolResult(success=False, output="", error=str(exc))
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))


@register_tool("list_directory")
async def list_directory(path: str = ".", pattern: str = "*", **_) -> ToolResult:
    """List files in a directory (sandboxed to project directory)."""
    try:
        p = _sandbox_path(path)
        if not p.is_dir():
            return ToolResult(success=False, output="", error=f"Not a directory: {path}")

        entries = sorted(p.glob(pattern))
        lines = []
        for entry in entries[:100]:
            prefix = "[DIR] " if entry.is_dir() else "      "
            lines.append(f"{prefix}{entry.name}")

        return ToolResult(
            success=True,
            output="\n".join(lines) if lines else "Empty directory",
            metadata={"count": len(lines)},
        )
    except PermissionError as exc:
        return ToolResult(success=False, output="", error=str(exc))
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))

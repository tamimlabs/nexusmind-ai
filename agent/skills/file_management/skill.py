"""File management skill — read, write, list files."""

from __future__ import annotations

from pathlib import Path

from agent.core.executor import ToolResult, register_tool


@register_tool("read_file")
async def read_file(path: str, encoding: str = "utf-8", **_) -> ToolResult:
    """Read a file and return its contents."""
    try:
        p = Path(path)
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
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))


@register_tool("write_file")
async def write_file(path: str, content: str, encoding: str = "utf-8", **_) -> ToolResult:
    """Write content to a file in the output/ directory (creates parent dirs)."""
    try:
        p = Path(path)
        if not str(p).startswith("output"):
            p = Path("output") / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=encoding)
        return ToolResult(
            success=True,
            output=f"Written {len(content)} chars to {p}",
            metadata={"path": str(p.absolute())},
        )
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))


@register_tool("list_directory")
async def list_directory(path: str = ".", pattern: str = "*", **_) -> ToolResult:
    """List files in a directory."""
    try:
        p = Path(path)
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
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))

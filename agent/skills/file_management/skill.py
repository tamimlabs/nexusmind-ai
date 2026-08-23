"""File management skill — read, write, list files."""

from __future__ import annotations

from pathlib import Path

from agent.core.executor import ToolResult, register_tool

# Sandbox: restrict file operations to project root or output/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_OUTPUT_DIR = _PROJECT_ROOT / "output"


def _sandbox_path(path: str) -> Path:
    """Resolve and validate path is within project root or output/."""
    p = Path(path)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    resolved = p.resolve()
    # Allow project root and output directory
    if str(resolved).startswith(str(_PROJECT_ROOT)) or str(resolved).startswith(str(_OUTPUT_DIR)):
        return resolved
    raise PermissionError(f"Access denied: {path} is outside project directory")


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

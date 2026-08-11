"""ForgeMCP stdio server and the initial read-only workspace tools."""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from forgemcp.config import ForgeConfig
from forgemcp.workspace import WorkspaceService

mcp = FastMCP("ForgeMCP")


def get_workspace() -> WorkspaceService:
    """Create a workspace service from the server's current configuration."""
    return WorkspaceService(ForgeConfig.from_environment())


# Compatibility helpers retained while the MCP API is small.
def workspace_root() -> Path:
    return get_workspace().root


def resolve_workspace_path(path: str) -> Path:
    return get_workspace().resolve_path(path)


@mcp.tool()
def workspace_info() -> dict[str, str | bool]:
    """Return the root directory currently accessible to ForgeMCP."""
    root = workspace_root()
    return {"root": str(root), "exists": root.exists()}


@mcp.tool()
def list_files(path: str = ".", recursive: bool = False) -> list[str]:
    """List visible files under a workspace directory."""
    return get_workspace().list_files(path, recursive=recursive)


@mcp.tool()
def read_file(path: str, max_chars: int = 100_000) -> dict[str, str | bool]:
    """Read a UTF-8 text file inside the workspace with an output size limit."""
    return get_workspace().read_text(path, max_chars=max_chars).as_dict()


def main() -> None:
    """Start ForgeMCP using MCP's standard input/output transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

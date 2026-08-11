"""Thin MCP stdio adapter for the ForgeMCP Core application."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from forgemcp.core.application import ForgeApplication


def server_status(application: ForgeApplication) -> dict[str, object]:
    """Return the safe Core status payload used by the MCP diagnostic tool."""
    return application.status().as_dict()


def create_server(application: ForgeApplication) -> FastMCP:
    """Bind Core application operations to MCP tools without owning Core state."""
    mcp = FastMCP("ForgeMCP")

    @mcp.tool(name="server_status")
    def server_status_tool() -> dict[str, object]:
        """Return ForgeMCP version, workspace, lifecycle state, and Core services."""
        return server_status(application)

    return mcp


def main() -> None:
    """Create, run, and reliably stop a stdio ForgeMCP application."""
    application = ForgeApplication.from_environment()
    application.start()
    try:
        create_server(application).run(transport="stdio")
    finally:
        application.stop()


if __name__ == "__main__":
    main()

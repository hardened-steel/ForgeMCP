"""Thin MCP stdio adapter for the ForgeMCP Core application."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from mcp.server.fastmcp import Context, FastMCP

from forgemcp.core.application import ForgeApplication


def server_status(application: ForgeApplication) -> dict[str, object]:
    """Return the safe Core status payload used by the MCP diagnostic tool."""
    return application.status().as_dict()


def create_server(
    application_factory: Callable[[], ForgeApplication] = ForgeApplication.from_environment,
) -> FastMCP[ForgeApplication]:
    """Create the MCP adapter and own the application through its async lifespan."""

    @asynccontextmanager
    async def application_lifespan(_: FastMCP[ForgeApplication]) -> AsyncIterator[ForgeApplication]:
        application = application_factory()
        try:
            application.start()
            yield application
        finally:
            await application.aclose()

    mcp = FastMCP[ForgeApplication]("ForgeMCP", lifespan=application_lifespan)

    @mcp.tool(name="server_status")
    def server_status_tool(context: Context) -> dict[str, object]:
        """Return ForgeMCP version, workspace, lifecycle state, and Core services."""
        application = context.request_context.lifespan_context
        if not isinstance(application, ForgeApplication):  # pragma: no cover - SDK invariant
            raise RuntimeError("ForgeMCP application is unavailable outside its MCP lifespan.")
        return server_status(application)

    return mcp


def main() -> None:
    """Run the stdio adapter; FastMCP owns the asynchronous application lifespan."""
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()

"""Thin MCP stdio adapter for the ForgeMCP Core application."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from mcp.server.fastmcp import Context, FastMCP
from pydantic_core import PydanticUndefined

from forgemcp.core.application import ForgeApplication
from forgemcp.plugins import RegisteredToolContribution, ToolRegistry


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
            await application.start()
            _register_contributed_tools(mcp, application.services.get("plugins").tools)
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


def _register_contributed_tools(
    mcp: FastMCP[ForgeApplication], registry: ToolRegistry
) -> None:
    """Adapt transport-neutral contributions after plugin startup, never exposing FastMCP to them."""
    for contribution in registry.contributions():
        mcp.tool(name=contribution.name, description=contribution.description)(
            _tool_adapter(contribution)
        )


def _tool_adapter(contribution: RegisteredToolContribution):
    """Create the SDK-facing wrapper for one generic mapping-based contribution."""

    async def contributed_tool(arguments: dict[str, object]) -> object:
        result = contribution.handler(arguments)
        if inspect.isawaitable(result):
            return await result
        return result

    if contribution.input_model is not None:
        parameters: list[inspect.Parameter] = []
        for name, field in contribution.input_model.model_fields.items():
            default = inspect.Parameter.empty if field.is_required() else field.default
            if default is PydanticUndefined:
                default = inspect.Parameter.empty
            parameters.append(
                inspect.Parameter(
                    name,
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    default=default,
                    annotation=field.annotation,
                )
            )

        async def contributed_tool_with_schema(**arguments: object) -> object:
            result = contribution.handler(arguments)
            if inspect.isawaitable(result):
                return await result
            return result

        contributed_tool_with_schema.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
        return contributed_tool_with_schema

    return contributed_tool


def main() -> None:
    """Run the stdio adapter; FastMCP owns the asynchronous application lifespan."""
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()

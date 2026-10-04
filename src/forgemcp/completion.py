"""Composition for MCP's single server-wide completion handler."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from mcp.server import MCPServer
from mcp.types import (
    Completion,
    CompletionArgument,
    CompletionContext,
    PromptReference,
    ResourceTemplateReference,
)

CompletionHandler = Callable[
    [
        PromptReference | ResourceTemplateReference,
        CompletionArgument,
        CompletionContext | None,
    ],
    Awaitable[Completion | None],
]


class Complete:
    """Collect feature completion handlers behind one MCP handler."""

    def __init__(self) -> None:
        """Start an empty ordered collection of feature completion handlers."""
        self.handlers: list[CompletionHandler] = []

    def add_completion(self, handler: CompletionHandler) -> None:
        """Append a feature's completion handler in dispatch order."""
        self.handlers.append(handler)

    async def complete(
        self,
        ref: PromptReference | ResourceTemplateReference,
        argument: CompletionArgument,
        context: CompletionContext | None,
    ) -> Completion | None:
        """Return the first feature answer that handles the requested completion."""
        for handler in self.handlers:
            result = await handler(ref, argument, context)
            if result is not None:
                return result
        return None

    def register(self, mcp: MCPServer) -> None:
        """Mount the shared SDK completion handler when features have registered callbacks."""
        if self.handlers:
            mcp.completion()(self.complete)

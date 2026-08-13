"""Small internal backend protocol; it has no MCP or FastMCP dependency."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from forgemcp.debugger.models import DebugAdapterInfo


class DebugAdapterBackend(Protocol):
    """Backend-owned discovery, strict startup, and safe DAP argument mapping."""

    backend_id: str

    def discover(self) -> DebugAdapterInfo:
        """Return read-only candidate metadata without starting an adapter."""

    async def start_adapter(self) -> object:
        """Start only the approved adapter through Process Runtime."""

    def initialize_arguments(self) -> Mapping[str, object]:
        """Return fixed safe DAP initialize arguments."""

    def launch_arguments(
        self,
        *,
        program: str,
        cwd: str,
        args: tuple[str, ...],
        environment: Mapping[str, str],
        stop_on_entry: bool,
    ) -> Mapping[str, object]:
        """Map an already validated safe launch request to backend DAP arguments."""

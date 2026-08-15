"""Test-only stdio server exposing a slow context-aware contribution."""

from __future__ import annotations

import asyncio

from forgemcp.core import ForgeApplication, ForgeConfig
from forgemcp.models._base import ForgeModel
from forgemcp.plugins import ForgePlugin, PluginContext, PluginMetadata, ProgressUpdate, ToolContribution, ToolExecutionContext
from forgemcp.server import create_server


class _ProgressFixturePlugin(ForgePlugin):
    def __init__(self) -> None:
        super().__init__(PluginMetadata(plugin_id="progress_fixture"))

    async def start(self, context: PluginContext) -> None:
        async def slow(_: dict[str, object], *, execution_context: ToolExecutionContext) -> dict[str, object]:
            for step in range(3):
                execution_context.throw_if_cancelled()
                await execution_context.report_progress(ProgressUpdate(step, 3, "Fixture working"))
                await asyncio.sleep(0.08)
            await execution_context.report_progress(ProgressUpdate(3, 3, "Fixture completed", terminal=True))
            return {"completed": True}

        context.tools.register(ToolContribution(
            name="slow", description="Test-only slow progress fixture.", handler=slow, input_model=_EmptyArguments,
        ))

    async def stop(self) -> None:
        return None


class _EmptyArguments(ForgeModel):
    """No fixture inputs."""


def main() -> None:
    def application_factory() -> ForgeApplication:
        return ForgeApplication.create(
            ForgeConfig.from_environment(), builtin_plugins=(_ProgressFixturePlugin(),)
        )

    create_server(application_factory).run(transport="stdio")


if __name__ == "__main__":
    main()

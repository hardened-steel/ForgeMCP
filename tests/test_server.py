import asyncio
import sys
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel

from forgemcp.core.application import ForgeApplication, LifecycleState
from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.core.services import ServiceRegistry
from forgemcp.processes import ProcessPolicy, ProcessRuntime
from forgemcp.plugins import ForgePlugin, PluginContext, PluginManager, PluginMetadata, ToolContribution
from forgemcp.server import create_server, server_status
from forgemcp.workspace import WorkspaceService


def test_server_status_smoke_test(tmp_path):
    application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
    asyncio.run(application.start())

    status = server_status(application)

    assert status["workspace_root"] == "configured"
    assert str(tmp_path.resolve()) not in repr(status)
    assert status["state"] == LifecycleState.RUNNING.value
    assert status["services"] == [
        "config", "logger", "plugins", "process_runtime", "project_status_registry",
        "project_status_service", "toolchain_discovery", "workspace",
    ]
    server = create_server(lambda: application)
    assert server.name == "ForgeMCP"
    assert [tool.name for tool in asyncio.run(server.list_tools())] == ["server_status"]


def application_with_test_process_runtime(root: Path) -> ForgeApplication:
    """Compose an app whose process policy admits only this test interpreter."""
    config = ForgeConfig(workspace_root=root)
    logger = create_logger("CRITICAL")
    services = ServiceRegistry()
    services.register("config", config)
    services.register("logger", logger)
    services.register("workspace", WorkspaceService(config, logger))
    services.register(
        "process_runtime",
        ProcessRuntime(
            config,
            logger,
            policy=ProcessPolicy(
                allowed_executables=frozenset(),
                allowed_executable_paths=frozenset({Path(sys.executable).resolve()}),
                default_timeout_seconds=1.0,
                maximum_timeout_seconds=5.0,
                termination_grace_seconds=0.2,
            ),
        ),
    )
    services.register("plugins", PluginManager(config=config, services=services, logger=logger))
    return ForgeApplication(config, services)


def test_mcp_lifespan_closes_a_long_lived_process_after_an_exception(tmp_path):
    async def exercise() -> None:
        application = application_with_test_process_runtime(tmp_path)
        server = create_server(lambda: application)
        process_runtime = application.services.get("process_runtime")
        assert isinstance(process_runtime, ProcessRuntime)

        with pytest.raises(RuntimeError, match="lifespan failure"):
            async with server._mcp_server.lifespan(server._mcp_server):  # type: ignore[attr-defined]
                assert application.state is LifecycleState.RUNNING
                handle = await process_runtime.start(
                    [sys.executable, "-c", "import time; time.sleep(30)"]
                )
                await asyncio.sleep(0.05)
                raise RuntimeError("lifespan failure")

        assert application.state is LifecycleState.STOPPED
        assert handle.returncode is not None

    asyncio.run(exercise())


def test_server_adapts_plugin_tool_contributions_only_after_plugin_start(tmp_path):
    class ToolPlugin(ForgePlugin):
        def __init__(self) -> None:
            super().__init__(PluginMetadata(plugin_id="example"))

        async def start(self, context: PluginContext) -> None:
            context.tools.register(
                ToolContribution(
                    name="inspect",
                    description="Configure a build tree.",
                    handler=lambda _: {"configured": False},
                )
            )

        async def stop(self) -> None:
            return None

    async def exercise() -> None:
        application = ForgeApplication.create(
            ForgeConfig(workspace_root=tmp_path), builtin_plugins=(ToolPlugin(),)
        )
        server = create_server(lambda: application)
        async with server._mcp_server.lifespan(server._mcp_server):  # type: ignore[attr-defined]
            assert sorted(tool.name for tool in await server.list_tools()) == [
                "clang_format__apply",
                "clang_format__check",
                "clang_tidy__list_checks",
                "clang_tidy__run",
                "clangd__apply_code_action",
                "clangd__code_actions",
                "clangd__completion",
                "clangd__declaration",
                "clangd__definition",
                "clangd__diagnostics",
                "clangd__document_symbols",
                "clangd__format_document",
                "clangd__format_range",
                "clangd__hover",
                "clangd__implementation",
                "clangd__incoming_calls",
                "clangd__outgoing_calls",
                "clangd__prepare_call_hierarchy",
                "clangd__prepare_rename",
                "clangd__prepare_type_hierarchy",
                "clangd__references",
                "clangd__rename",
                "clangd__signature_help",
                "clangd__start",
                "clangd__status",
                "clangd__stop",
                "clangd__subtypes",
                "clangd__supertypes",
                "clangd__switch_source_header",
                "clangd__type_definition",
                "clangd__workspace_symbols",
                "cmake__build",
                "cmake__configure",
                "cmake__ctest_list_tests",
                "cmake__ctest_run",
                "cmake__list_presets",
                "cmake__list_targets",
                "cmake__status",
                "debugger__continue",
                "debugger__evaluate",
                "debugger__events",
                "debugger__launch",
                "debugger__list_adapters",
                "debugger__pause",
                "debugger__scopes",
                "debugger__set_breakpoints",
                "debugger__stack_trace",
                "debugger__status",
                "debugger__step_in",
                "debugger__step_out",
                "debugger__step_over",
                "debugger__stop",
                "debugger__threads",
                "debugger__variables",
                    "example__inspect",
                    "project__status",
                    "quality__status",
                "sanitizer__parse_report",
                "server_status",
                "workspace__apply_text_edits",
                "workspace__apply_unified_patch",
                "workspace__get_snapshot",
                "workspace__list_files",
                "workspace__read_text",
            ]

    asyncio.run(exercise())


def test_tool_handler_exception_does_not_prevent_application_shutdown(tmp_path):
    class NoArguments(BaseModel):
        pass

    class ExplodingPlugin(ForgePlugin):
        def __init__(self) -> None:
            super().__init__(PluginMetadata(plugin_id="explode"))

        async def start(self, context: PluginContext) -> None:
            context.tools.register(
                ToolContribution(
                    name="now",
                    description="Raise an intentional test exception.",
                    handler=lambda _: (_ for _ in ()).throw(RuntimeError("tool failure")),
                    input_model=NoArguments,
                )
            )

        async def stop(self) -> None:
            return None

    async def exercise() -> None:
        application = ForgeApplication.create(
            ForgeConfig(workspace_root=tmp_path), builtin_plugins=(ExplodingPlugin(),)
        )
        server = create_server(lambda: application)

        with pytest.raises(ToolError, match="tool failure"):
            async with server._mcp_server.lifespan(server._mcp_server):  # type: ignore[attr-defined]
                await server.call_tool("explode__now", {})

        assert application.state is LifecycleState.STOPPED

    asyncio.run(exercise())

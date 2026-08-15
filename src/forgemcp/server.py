"""Thin MCP stdio adapter for the ForgeMCP Core application."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
import warnings
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from time import monotonic
from typing import Annotated

# Current MCP releases can emit this third-party Pydantic forward-reference
# warning while constructing FastMCP.  In stdio mode warnings are stderr
# protocol-adjacent diagnostics; suppress this known non-actionable warning so
# it cannot disclose the host site-packages path or resemble a server failure.
warnings.filterwarnings(
    "ignore",
    message=r"Field 'lifespan' has an incomplete definition:.*",
    category=Warning,
    module=r"pydantic_settings\.sources\.utils",
)

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from pydantic import ConfigDict, Field
from pydantic_core import PydanticUndefined

from forgemcp.core.application import ForgeApplication
from forgemcp.core.config import ForgeConfig
from forgemcp.core.errors import ConfigurationError
from forgemcp.plugins import (
    NoOpProgressReporter,
    ProgressUpdate,
    RegisteredToolContribution,
    ToolExecutionContext,
    ToolRegistry,
    invoke_tool_handler,
)
from forgemcp.toolchain import ToolchainDiscoveryService


# FastMCP derives a transient Pydantic arguments model from each Python
# signature.  Its SDK default silently ignores unknown keys, which would bypass
# the strict ``ForgeModel`` contracts before a contribution receives its
# mapping.  Configure that common base before any tool is registered so every
# published flat schema and every actual stdio invocation reject extra fields.
ArgModelBase.model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")


class _McpProgressReporter:
    """One bounded, synchronous progress bridge for a single SDK request.

    It deliberately keeps no application/service references and creates no
    notification tasks.  A slow or failing transport merely disables further
    progress; it never changes the outcome or lifetime of a tool operation.
    """

    _MINIMUM_INTERVAL_SECONDS = 0.5
    _DELIVERY_TIMEOUT_SECONDS = 0.75

    def __init__(self, context: Context) -> None:
        try:
            request_context = context.request_context
        except ValueError:
            # FastMCP's in-process ``call_tool`` test/helper path supplies a
            # Context shell outside a protocol request.  It has no token and
            # must behave exactly like an in-process no-op reporter.
            request_context = None
        metadata = getattr(request_context, "meta", None)
        self._context = context
        self._enabled = getattr(metadata, "progressToken", None) is not None
        self._last_sent_at = float("-inf")
        self._last_progress: float | None = None
        self._last_total: float | None = None
        self._lock = asyncio.Lock()

    @property
    def supports_progress(self) -> bool:
        return self._enabled

    async def report(self, update: ProgressUpdate) -> None:
        if not self._enabled:
            return
        async with self._lock:
            if not self._enabled:
                return
            # Progress is monotonic for one measurement mode.  A known exact
            # total can intentionally replace an earlier phase-only update.
            if self._last_total == update.total and self._last_progress is not None:
                if update.progress < self._last_progress:
                    return
            now = monotonic()
            if not update.terminal and now - self._last_sent_at < self._MINIMUM_INTERVAL_SECONDS:
                return
            try:
                await asyncio.wait_for(
                    self._context.report_progress(update.progress, update.total, update.message),
                    timeout=self._DELIVERY_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                # Cancellation must reach the operation, whose ProcessRuntime
                # cleanup path owns any subprocess tree.
                raise
            except Exception:
                # Token/transport loss is best effort.  Do not log a message:
                # a label can contain a validated target/test name.
                self._enabled = False
                return
            self._last_sent_at = now
            self._last_progress = update.progress
            self._last_total = update.total


def _execution_context(context: Context | None) -> ToolExecutionContext:
    """Create an ephemeral SDK-free context for exactly one handler call."""
    if context is None:
        return ToolExecutionContext(NoOpProgressReporter())
    reporter = _McpProgressReporter(context)
    return ToolExecutionContext(reporter if reporter.supports_progress else NoOpProgressReporter())


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
        hints = contribution.hints
        annotations = None if hints is None else ToolAnnotations(
            readOnlyHint=hints.read_only,
            destructiveHint=hints.destructive,
            idempotentHint=hints.idempotent,
            openWorldHint=hints.open_world,
        )
        mcp.tool(name=contribution.name, description=contribution.description, annotations=annotations)(
            _tool_adapter(contribution)
        )


def _tool_adapter(contribution: RegisteredToolContribution):
    """Create the SDK-facing wrapper for one generic mapping-based contribution."""

    async def contributed_tool(arguments: dict[str, object], context: Context) -> object:
        result = invoke_tool_handler(contribution.handler, arguments, _execution_context(context))
        if inspect.isawaitable(result):
            return await result
        return result

    if contribution.input_model is not None:
        parameters: list[inspect.Parameter] = []
        for name, field in contribution.input_model.model_fields.items():
            default = inspect.Parameter.empty if field.is_required() else field.get_default(call_default_factory=True)
            if default is PydanticUndefined:
                default = inspect.Parameter.empty
            annotation = field.rebuild_annotation()
            if field.description is not None:
                annotation = Annotated[annotation, Field(description=field.description)]
            parameters.append(
                inspect.Parameter(
                    name,
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    default=default,
                    annotation=annotation,
                )
            )

        async def contributed_tool_with_schema(context: Context, **arguments: object) -> object:
            result = invoke_tool_handler(contribution.handler, arguments, _execution_context(context))
            if inspect.isawaitable(result):
                return await result
            return result

        contributed_tool_with_schema.__signature__ = inspect.Signature([
            inspect.Parameter(
                "context", kind=inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context
            ),
            *parameters,
        ])  # type: ignore[attr-defined]
        return contributed_tool_with_schema

    return contributed_tool


def _parser() -> argparse.ArgumentParser:
    """Create the deliberately dependency-free public CLI parser."""
    parser = argparse.ArgumentParser(
        prog="forgemcp",
        description="Safe workspace-scoped C++ MCP server and Windows toolchain diagnostics.",
    )
    parser.add_argument("--workspace", dest="workspace_root", metavar="DIR", help="Workspace root (overrides FORGEMCP_WORKSPACE).")
    parser.add_argument("--source-dir", dest="cmake_source_dir", metavar="DIR", help="Default workspace-relative CMake source directory.")
    parser.add_argument("--build-dir", metavar="DIR", help="Default workspace-relative CMake build directory.")
    parser.add_argument("--cmake", dest="cmake_path", metavar="PATH", help="Exact CMake executable path.")
    parser.add_argument("--ctest", dest="ctest_path", metavar="PATH", help="Exact CTest executable path.")
    parser.add_argument("--clangd", dest="clangd_path", metavar="PATH", help="Exact clangd executable path.")
    parser.add_argument("--clang-format", dest="clang_format_path", metavar="PATH", help="Exact clang-format executable path.")
    parser.add_argument("--clang-tidy", dest="clang_tidy_path", metavar="PATH", help="Exact clang-tidy executable path.")
    parser.add_argument("--lldb-dap", dest="lldb_dap_path", metavar="PATH", help="Exact lldb-dap executable path.")
    parser.add_argument("--toolchain", choices=("auto", "msvc", "llvm"), help="Toolchain preference.")
    parser.add_argument("--host-arch", choices=("auto", "x64", "x86", "arm64"), help="Tool host architecture.")
    parser.add_argument("--target-arch", choices=("auto", "x64", "x86", "arm64"), help="Compiler target architecture.")
    parser.add_argument("--visual-studio-instance", metavar="SELECTOR", help="Exact VS product, display-name, or version selector.")
    parser.add_argument("--cmake-generator", metavar="NAME", help="Generator used only when no configure preset is active.")
    parser.add_argument("--configure-preset", metavar="NAME", help="Default configure preset.")
    parser.add_argument("--configuration", dest="default_configuration", metavar="NAME", help="Default multi-config configuration.")
    parser.add_argument("--compile-commands", choices=("auto", "required", "off"), help="Compilation database policy for CMake and clangd.")
    parser.add_argument("--configure-timeout-sec", dest="configure_timeout_seconds", type=float, metavar="SECONDS", help="Configure timeout (1..3600).")
    parser.add_argument("--build-timeout-sec", dest="build_timeout_seconds", type=float, metavar="SECONDS", help="Build timeout (1..3600).")
    parser.add_argument("--test-timeout-sec", dest="test_timeout_seconds", type=float, metavar="SECONDS", help="CTest timeout (1..3600).")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"), help="Stderr log level.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--external-plugins-enabled", dest="external_plugins_enabled", action="store_true", default=None, help="Enable allow-listed external entry-point plugins.")
    group.add_argument("--no-external-plugins", dest="external_plugins_enabled", action="store_false", default=None, help="Disable external entry-point plugins.")
    parser.add_argument("--external-plugin-allowlist", metavar="NAMES", help="Comma-separated external plugin allow-list.")
    commands = parser.add_subparsers(dest="command")
    doctor = commands.add_parser("doctor", help="Run bounded sanitized toolchain discovery.")
    doctor.add_argument("--json", action="store_true", help="Emit compact JSON suitable for local automation.")
    commands.add_parser("print-config", help="Print sanitized effective configuration as JSON.")
    return parser


def _cli_config(arguments: argparse.Namespace) -> ForgeConfig:
    values = vars(arguments).copy()
    values.pop("command", None)
    values.pop("json", None)
    return ForgeConfig.from_sources(cli={key: value for key, value in values.items() if value is not None})


def main(argv: list[str] | None = None) -> None:
    """Run stdio by default; ``doctor`` and ``print-config`` are local commands."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        config = _cli_config(arguments)
    except ConfigurationError as error:
        # ``argparse`` gives the local operator a concise stderr-only failure
        # before the stdio transport can be created.  Do not leak a traceback
        # or let a malformed option corrupt MCP stdout.
        parser.error(error.message)
    if arguments.command == "print-config":
        print(json.dumps(config.sanitized_effective_config(), ensure_ascii=False, sort_keys=True))
        return
    if arguments.command == "doctor":
        discovery = ToolchainDiscoveryService(config)
        payload = {"configuration": config.sanitized_effective_config(), "discovery": discovery.snapshot().as_dict()}
        if arguments.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print("ForgeMCP doctor")
            for item in payload["discovery"]["tools"]:  # type: ignore[index]
                state = "available" if item["available"] else f"unavailable ({item['rejection']})"  # type: ignore[index]
                print(f"{item['tool']}: {state} [{item['source']}]")  # type: ignore[index]
        return
    # Compatibility contract: no subcommand remains stdio MCP server startup.
    create_server(lambda: ForgeApplication.create(config)).run(transport="stdio")


if __name__ == "__main__":
    main()

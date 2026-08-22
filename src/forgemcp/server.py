"""Thin MCP stdio adapter for the ForgeMCP Core application."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
import warnings
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from time import monotonic
from types import MethodType
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
from mcp import types as mcp_types
from mcp.types import ToolAnnotations
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from pydantic import ConfigDict, Field
from pydantic_core import PydanticUndefined

from forgemcp import __version__
from forgemcp.core.application import ForgeApplication
from forgemcp.core.config import ForgeConfig
from forgemcp.core.errors import ConfigurationError
from forgemcp.core.logging import LOG_LEVELS, StructuredLogEvent, StructuredLogger
from forgemcp.discovery import SERVER_INSTRUCTIONS
from forgemcp.plugins import (
    CompletionReferenceKind,
    CompletionRequest,
    DiscoverySurfaceRegistry,
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


class _McpLogSink:
    """One connection-scoped non-blocking MCP notification sink."""

    _QUEUE_SIZE = 64
    _MINIMUM_INTERVAL_SECONDS = 0.05
    _DELIVERY_TIMEOUT_SECONDS = 0.5
    _CLOSE_TIMEOUT_SECONDS = 0.25
    _PRIORITY = {name: index for index, name in enumerate(LOG_LEVELS)}

    def __init__(self, session: object, level: str) -> None:
        if level not in self._PRIORITY:
            raise ValueError("MCP logging level is invalid.")
        self._session = session
        self._threshold = self._PRIORITY[level]
        self._queue: asyncio.Queue[StructuredLogEvent | None] = asyncio.Queue(self._QUEUE_SIZE)
        self._worker = asyncio.create_task(self._run(), name="forgemcp-mcp-logging")
        self._delivery: asyncio.Task[None] | None = None
        self._closed = False
        self._last_sent_at = float("-inf")

    def emit(self, event: StructuredLogEvent) -> None:
        """Queue one event without blocking the operation that produced it."""
        if self._closed or self._PRIORITY[event.level] < self._threshold:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Flood loss is deliberate and never creates a recursive log.
            return

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            delay = self._MINIMUM_INTERVAL_SECONDS - (monotonic() - self._last_sent_at)
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await self._send(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._closed = True
                return
            self._last_sent_at = monotonic()

    async def _send(self, event: StructuredLogEvent) -> None:
        sender = getattr(self._session, "send_log_message", None)
        if sender is None:
            raise RuntimeError("MCP session logging is unavailable.")
        task = asyncio.create_task(
            sender(
                level=event.level,
                data={
                    "sequence": event.sequence,
                    "timestamp": event.timestamp,
                    "category": event.category,
                    "metadata": dict(event.metadata),
                },
                logger=event.logger,
            )
        )
        self._delivery = task
        try:
            done, _ = await asyncio.wait({task}, timeout=self._DELIVERY_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            self._detach(task)
            raise
        if task not in done:
            self._detach(task)
            raise TimeoutError("MCP log delivery timed out.")
        self._delivery = None
        task.result()

    def _detach(self, task: asyncio.Task[None]) -> None:
        self._delivery = None
        if not task.done():
            task.cancel()
        task.add_done_callback(self._consume)

    @staticmethod
    def _consume(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            return

    async def aclose(self) -> None:
        if self._closed and self._worker.done():
            return
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        self._worker.cancel()
        delivery = self._delivery
        if delivery is not None:
            self._detach(delivery)
        try:
            done, _ = await asyncio.wait({self._worker}, timeout=self._CLOSE_TIMEOUT_SECONDS)
            if self._worker in done:
                self._consume(self._worker)
            else:
                self._worker.add_done_callback(self._consume)
        except asyncio.CancelledError:
            self._worker.add_done_callback(self._consume)
            raise


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
        self._lock = asyncio.Lock()
        self._delivery_task: asyncio.Task[None] | None = None

    @property
    def supports_progress(self) -> bool:
        return self._enabled

    async def report(self, update: ProgressUpdate) -> None:
        if not self._enabled:
            return
        async with self._lock:
            if not self._enabled:
                return
            now = monotonic()
            if not update.terminal and now - self._last_sent_at < self._MINIMUM_INTERVAL_SECONDS:
                return
            try:
                await self._deliver(update)
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

    async def _deliver(self, update: ProgressUpdate) -> None:
        """Await one send for a fixed interval without trusting cancellation.

        ``asyncio.wait_for`` waits again when a misbehaving coroutine catches
        cancellation.  A transport callback is observational, so leave at
        most one cancelled send detached for this reporter, consume its final
        exception, and disable future delivery instead of extending the tool.
        """
        task = asyncio.create_task(
            self._context.report_progress(update.progress, update.total, update.message)
        )
        self._delivery_task = task
        try:
            done, _ = await asyncio.wait({task}, timeout=self._DELIVERY_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            self._detach_delivery(task)
            raise
        if task not in done:
            self._detach_delivery(task)
            raise TimeoutError("Progress delivery timed out.")
        self._delivery_task = None
        task.result()

    def _detach_delivery(self, task: asyncio.Task[None]) -> None:
        """Cancel one bounded outstanding send and always consume its outcome."""
        self._delivery_task = None
        if not task.done():
            task.cancel()
        task.add_done_callback(self._consume_delivery_exception)

    @staticmethod
    def _consume_delivery_exception(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            return


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

    connection_sinks: dict[int, tuple[StructuredLogger, _McpLogSink]] = {}
    surface_registered = False

    @asynccontextmanager
    async def application_lifespan(_: FastMCP[ForgeApplication]) -> AsyncIterator[ForgeApplication]:
        nonlocal surface_registered
        application = application_factory()
        try:
            await application.start()
            plugins = application.services.get("plugins")
            _register_contributed_tools(mcp, plugins.tools)
            if not surface_registered:
                _register_contributed_surface(mcp, plugins.surface)
                surface_registered = True
            yield application
        finally:
            await application.aclose()
            logger = application.services.get("logger")
            for key, (registered_logger, _) in tuple(connection_sinks.items()):
                if registered_logger is logger:
                    del connection_sinks[key]

    mcp = FastMCP[ForgeApplication](
        "ForgeMCP",
        instructions=SERVER_INSTRUCTIONS,
        lifespan=application_lifespan,
        # SDK diagnostics are not ForgeMCP structured events and can include
        # implementation source locations. Keep stdio-adjacent stderr owned by
        # the application-scoped JSON sink instead.
        log_level="CRITICAL",
    )
    # FastMCP 1.x otherwise reports the MCP SDK distribution version. This is
    # the only SDK identity bridge; project metadata remains transport-neutral.
    mcp._mcp_server.version = __version__  # type: ignore[attr-defined]
    _omit_empty_experimental_capability(mcp)

    @mcp.completion()
    async def complete(
        reference: mcp_types.PromptReference | mcp_types.ResourceTemplateReference,
        argument: mcp_types.CompletionArgument,
        context: mcp_types.CompletionContext | None,
    ) -> mcp_types.Completion:
        application = _current_application(mcp)
        registry = _surface_registry(application)
        if isinstance(reference, mcp_types.PromptReference):
            kind = CompletionReferenceKind.PROMPT
            name = reference.name
        elif isinstance(reference, mcp_types.ResourceTemplateReference):
            kind = CompletionReferenceKind.RESOURCE_TEMPLATE
            name = reference.uri
        else:  # pragma: no cover - SDK union validation
            raise ValueError("Completion reference is unsupported.")
        result = await registry.complete(
            CompletionRequest(
                reference_kind=kind,
                reference=name,
                argument=argument.name,
                value=argument.value,
                context={} if context is None or context.arguments is None else context.arguments,
            )
        )
        return mcp_types.Completion(
            values=list(result.values), total=result.total, hasMore=result.has_more
        )

    @mcp._mcp_server.set_logging_level()  # type: ignore[attr-defined]
    async def set_logging_level(level: mcp_types.LoggingLevel) -> None:
        if level not in LOG_LEVELS:
            raise ValueError("MCP logging level is invalid.")
        request_context = mcp._mcp_server.request_context  # type: ignore[attr-defined]
        application = request_context.lifespan_context
        if not isinstance(application, ForgeApplication):  # pragma: no cover - SDK invariant
            raise RuntimeError("ForgeMCP application is unavailable.")
        logger = application.services.get("logger")
        if not isinstance(logger, StructuredLogger):
            raise RuntimeError("ForgeMCP structured logger is unavailable.")
        session = request_context.session
        key = id(session)
        prior = connection_sinks.pop(key, None)
        if prior is not None:
            prior[0].remove_sink(prior[1])
            await prior[1].aclose()
        sink = _McpLogSink(session, level)
        logger.add_sink(sink)
        connection_sinks[key] = (logger, sink)

    @mcp.tool(name="server_status")
    def server_status_tool(context: Context) -> dict[str, object]:
        """Return ForgeMCP version, workspace, lifecycle state, and Core services."""
        application = context.request_context.lifespan_context
        if not isinstance(application, ForgeApplication):  # pragma: no cover - SDK invariant
            raise RuntimeError("ForgeMCP application is unavailable outside its MCP lifespan.")
        return server_status(application)

    return mcp


def _omit_empty_experimental_capability(mcp: FastMCP[ForgeApplication]) -> None:
    """Adapt SDK 1.x's empty-dict default to an absent optional capability."""
    server = mcp._mcp_server  # type: ignore[attr-defined]
    original = server.get_capabilities

    def get_capabilities(self, notification_options, experimental_capabilities):
        capabilities = original(notification_options, experimental_capabilities)
        if not capabilities.experimental:
            return capabilities.model_copy(update={"experimental": None})
        return capabilities

    server.get_capabilities = MethodType(get_capabilities, server)


def _current_application(mcp: FastMCP[ForgeApplication]) -> ForgeApplication:
    """Resolve one request's lifespan application only inside the SDK adapter."""
    application = mcp._mcp_server.request_context.lifespan_context  # type: ignore[attr-defined]
    if not isinstance(application, ForgeApplication):  # pragma: no cover - SDK invariant
        raise RuntimeError("ForgeMCP application is unavailable outside its MCP lifespan.")
    return application


def _surface_registry(application: ForgeApplication) -> DiscoverySurfaceRegistry:
    plugins = application.services.get("plugins")
    registry = getattr(plugins, "surface", None)
    if not isinstance(registry, DiscoverySurfaceRegistry):
        raise RuntimeError("ForgeMCP discovery surface is unavailable.")
    return registry


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


def _register_contributed_surface(
    mcp: FastMCP[ForgeApplication], registry: DiscoverySurfaceRegistry
) -> None:
    """Adapt immutable non-tool contributions after successful plugin startup."""
    for contribution in registry.resources():
        mcp.resource(
            contribution.uri,
            name=contribution.name,
            description=contribution.description,
            mime_type=contribution.mime_type,
        )(_resource_adapter(mcp, contribution.uri))
    for contribution in registry.templates():
        mcp.resource(
            contribution.uri_template,
            name=contribution.name,
            description=contribution.description,
            mime_type=contribution.mime_type,
        )(
            _resource_template_adapter(
                mcp, contribution.uri_template, contribution.arguments
            )
        )
    for contribution in registry.prompts():
        mcp.prompt(name=contribution.name, description=contribution.description)(
            _prompt_adapter(mcp, contribution.name, contribution.arguments)
        )


def _resource_adapter(mcp: FastMCP[ForgeApplication], uri: str):
    async def contributed_resource() -> str:
        return await _surface_registry(_current_application(mcp)).read_resource(uri)

    return contributed_resource


def _resource_template_adapter(
    mcp: FastMCP[ForgeApplication], uri_template: str, argument_names: tuple[str, ...]
):
    async def contributed_resource_template(context: Context, **arguments: str) -> str:
        application = context.request_context.lifespan_context
        if not isinstance(application, ForgeApplication):  # pragma: no cover - SDK invariant
            raise RuntimeError("ForgeMCP application is unavailable.")
        return await _surface_registry(application).read_template(uri_template, arguments)

    contributed_resource_template.__annotations__ = {
        "context": Context,
        **{name: str for name in argument_names},
        "return": str,
    }
    contributed_resource_template.__signature__ = inspect.Signature(
        [
            inspect.Parameter(
                "context", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context
            ),
            *(
                inspect.Parameter(
                    name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=str,
                )
                for name in argument_names
            ),
        ],
        return_annotation=str,
    )  # type: ignore[attr-defined]
    return contributed_resource_template


def _prompt_adapter(mcp: FastMCP[ForgeApplication], name: str, argument_specs: tuple[object, ...]):
    async def contributed_prompt(context: Context, **arguments: object) -> list[dict[str, object]]:
        application = context.request_context.lifespan_context
        if not isinstance(application, ForgeApplication):  # pragma: no cover - SDK invariant
            raise RuntimeError("ForgeMCP application is unavailable.")
        supplied = {key: value for key, value in arguments.items() if value is not None}
        messages = await _surface_registry(application).get_prompt(name, supplied)
        return [
            {
                "role": message.role,
                "content": {"type": "text", "text": message.text},
            }
            for message in messages
        ]

    parameters = [
        inspect.Parameter("context", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context)
    ]
    annotations: dict[str, object] = {"context": Context}
    for specification in argument_specs:
        required = bool(getattr(specification, "required", False))
        annotation: object = Annotated[
            str,
            Field(
                description=getattr(specification, "description", None),
                max_length=getattr(specification, "max_length", 256),
            ),
        ]
        parameters.append(
            inspect.Parameter(
                getattr(specification, "name"),
                inspect.Parameter.KEYWORD_ONLY,
                default=inspect.Parameter.empty if required else None,
                annotation=annotation,
            )
        )
        annotations[getattr(specification, "name")] = annotation
    contributed_prompt.__annotations__ = annotations
    contributed_prompt.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    return contributed_prompt


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
        ], return_annotation=contribution.output_type or object)  # type: ignore[attr-defined]
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

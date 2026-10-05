"""Core analysis and retained sessions, using a deterministic LSP peer."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.apps import Apps

from forgemcp.clangd.errors import ClangdTimeoutError
from forgemcp.clangd.service import ClangdResource, ClangdService, HoverResult, group_results
from forgemcp.clangd.models import Hover, HoverText, Position
from forgemcp.cmake.service import CMakeService, CompilationContext
from forgemcp.completion import Complete
from forgemcp.toolchain.spec import ToolKind, Toolset, ToolSpec
from forgemcp.toolchain.service import ToolchainService
from forgemcp.toolchain.tools.clangd import LspMessage, LspNotification, LspRequest, LspResponse
from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.service import WorkspaceService


@pytest.fixture
def anyio_backend():
    """Run async tests on the asyncio backend used by the process and LSP services."""
    return "asyncio"


class LspPeer:
    """Respond to the exercised LSP methods without launching an installed clangd."""

    def __init__(self, root):
        """Initialize a scripted peer with queues, recorded messages, and a shared sample range."""
        self.root = root
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.ignore = set()
        self.span = {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 3},
        }

    async def send(self, message):
        """Record a client message and enqueue the scripted response or versioned diagnostic."""
        self.sent.append(message)
        if message.method in self.ignore:
            return
        if isinstance(message, LspRequest):
            location = {"uri": (self.root / "src/math.cpp").as_uri(), "range": self.span}
            symbol = {
                "name": "fixture",
                "kind": 3,
                "range": self.span,
                "selectionRange": self.span,
                "children": [
                    {
                        "name": "add",
                        "kind": 12,
                        "range": self.span,
                        "selectionRange": self.span,
                    },
                ],
            }
            results = {
                "initialize": {
                    "capabilities": {
                        "positionEncoding": "utf-32",
                        "hoverProvider": True,
                        "definitionProvider": True,
                        "referencesProvider": True,
                        "documentSymbolProvider": True,
                        "workspaceSymbolProvider": True,
                        "semanticTokensProvider": {
                            "full": True,
                            "legend": {"tokenTypes": ["keyword"], "tokenModifiers": []},
                        },
                    },
                },
                "textDocument/hover": {"contents": {"kind": "markdown", "value": "**add**"}},
                "textDocument/definition": [location],
                "textDocument/references": [location],
                "textDocument/documentSymbol": [symbol],
                "workspace/symbol": [
                    {
                        "name": "add",
                        "kind": 12,
                        "location": location,
                        "containerName": "fixture",
                    },
                ],
                "textDocument/semanticTokens/full": {"data": [0, 0, 3, 0, 0]},
                "shutdown": None,
            }
            await self.incoming.put(LspResponse(message.id, results[message.method]))
        elif message.method in ("textDocument/didOpen", "textDocument/didChange"):
            document = message.params["textDocument"]
            await self.incoming.put(
                LspNotification(
                    "textDocument/publishDiagnostics",
                    {
                        "uri": document["uri"],
                        "version": document["version"],
                        "diagnostics": [{"range": self.span, "message": "sample warning", "severity": 2}],
                    },
                ),
            )
        elif message.method == "exit":
            await self.incoming.put(None)

    async def messages(self) -> AsyncGenerator[LspMessage]:
        """Yield queued server messages until the exit sentinel arrives."""
        while (message := await self.incoming.get()) is not None:
            yield message

    async def close(self):
        """Mark the scripted connection as closed."""
        self.closed = True


@pytest.fixture
async def setup(cpp_acceptance_project) -> AsyncGenerator[SimpleNamespace]:
    """Provide two compilation contexts and retained sessions backed by deterministic LSP peers."""
    workspace = WorkspaceService(cpp_acceptance_project)
    peers = []

    @asynccontextmanager
    async def connect(project, database) -> AsyncGenerator[LspPeer]:
        """Validate the requested project and database and yield a tracked scripted peer."""
        assert project == workspace.root
        assert (database / "compile_commands.json").is_file()
        peer = LspPeer(project)
        peers.append(peer)
        try:
            yield peer
        finally:
            await peer.close()

    toolchains = ToolchainService(SimpleNamespace())
    tool = ToolSpec(
        "clangd",
        ToolKind.LANGUAGE_SERVER,
        workspace.root / "clangd",
        {"connect": connect},
    )
    toolchains.toolsets = (
        Toolset("llvm", "LLVM", (tool,), None, True),
        Toolset("empty", "Without clangd", (), None, True),
    )
    cmake = CMakeService(
        toolchains,
        project_root=workspace.root,
        storage_root=workspace.storage_root,
        protected_paths=workspace.protected_paths,
    )
    cmake.configurations_loaded = True
    for name, toolset in (("debug", "llvm"), ("release", "llvm"), ("unavailable", "empty")):
        directory = workspace.root / f"build/{name}"
        directory.mkdir(parents=True)
        (directory / "compile_commands.json").write_text("[]")
        cmake.configurations[name] = CompilationContext(
            id=name,
            toolset_id=toolset,
            build_directory=WorkspacePath(f"project/build/{name}"),
            compilation_database=WorkspacePath(f"project/build/{name}/compile_commands.json"),
        )
    service = ClangdService(workspace, toolchains, cmake, progress_interval=0)
    workspace.register_provider("clangd", service, tools=service.PROVIDER_TOOLS)
    apps = Apps()
    mcp = MCPServer("clangd-unit", extensions=[apps])
    complete = Complete()
    workspace.register(mcp, apps, complete)
    service.register(mcp, apps, complete)
    for binding in apps.tools():
        mcp.add_tool(binding.fn, meta=binding.meta, **binding.kwargs)
    complete.register(mcp)
    await service.initialize()
    try:
        yield SimpleNamespace(
            workspace=workspace,
            cmake=cmake,
            service=service,
            peers=peers,
            server=mcp,
        )
    finally:
        await service.close()


def test_equal_answers_merge_configuration_ids_but_different_answers_stay_separate():
    """Verify grouping merges only identical answers and leaves input provenance unchanged."""
    empty = HoverResult(
        configurations=["debug"],
        path=WorkspacePath("project/src/math.cpp"),
        position=Position(line=1, character=0),
        hover=None,
    )
    same = empty.model_copy(update={"configurations": ["release"]}, deep=True)
    different = empty.model_copy(
        update={
            "configurations": ["other"],
            "hover": Hover(contents=[HoverText(kind="plaintext", text="different")]),
        },
        deep=True,
    )
    grouped = group_results([empty, same, different])
    assert [answer.configurations for answer in grouped] == [["debug", "release"], ["other"]]
    assert empty.configurations == ["debug"] and same.configurations == ["release"]


@pytest.mark.anyio
async def test_read_only_tools_group_configurations_and_decode_language_values(setup):
    """Verify MCP analysis schemas, progress, grouping, and decoded language results."""
    progress = []

    async def report(value, total, message):
        """Record analysis progress values and messages for monotonicity checks."""
        progress.append((value, message))

    async with Client(setup.server) as client:
        configurations = await client.call_tool("clangd_configurations", {})
        assert [item["id"] for item in configurations.structured_content["result"]] == ["debug", "release"]
        assert "Available clangd configurations: 2." in configurations.content[0].text
        assert "Compilation database:" in configurations.content[0].text
        for tool in (
            "diagnostics",
            "hover",
            "definition",
            "references",
            "document_symbols",
            "workspace_symbols",
        ):
            arguments = {"path": "project/src/math.cpp"}
            if tool in ("hover", "definition", "references"):
                arguments["position"] = {"line": 1, "character": 0}
            if tool == "references":
                arguments["include_declaration"] = False
            if tool == "workspace_symbols":
                arguments = {"query": "add"}
            progress.clear()
            response = await client.call_tool(
                f"clangd_{tool}",
                arguments,
                progress_callback=report,
            )
            assert not response.is_error, response.content
            answers = response.structured_content["result"]
            assert len(answers) == 1 and answers[0]["configurations"] == ["debug", "release"]
            answer = answers[0]
            text = response.content[0].text
            assert "Configurations: debug, release" in text
            assert "```" not in text and not text.startswith("{")
            assert [value for value, _ in progress] == list(range(len(progress)))
            if tool == "diagnostics":
                assert (
                    answer["diagnostics"][0]["severity"] == "warning"
                    and "warning:" in text and "project/src/math.cpp, line 1" in text
                )
            elif tool == "hover":
                assert answer["hover"]["contents"][0]["text"] == "**add**"
                assert text.endswith("\n\nadd") and "**add**" not in text
                assert any("debug: Starting textDocument/hover" in message for _, message in progress)
            elif tool in ("definition", "references"):
                location = answer["locations"][0]
                assert location["path"] == "project/src/math.cpp"
                assert location["range"]["start"] == {"line": 1, "character": 0}
                assert location["preview"]["text"].startswith('#include "fixture/math.hpp"')
                assert '1 | #include "fixture/math.hpp"' in text
            elif tool == "document_symbols":
                assert (
                    answer["symbols"][0]["children"][0]["name"] == "add"
                    and "fixture (namespace)" in text and "add (function)" in text
                )
            else:
                assert answer["symbols"][0]["container_name"] == "fixture"
                assert answer["symbols"][0]["location"]["preview"]
                assert "add (function)" in text and "Scope: fixture" in text
        selected = await client.call_tool(
            "clangd_diagnostics",
            {"path": "project/src/math.cpp", "configurations": ["debug"]},
        )
        assert selected.structured_content["result"][0]["configurations"] == ["debug"]
        invalid = await client.call_tool(
            "clangd_diagnostics",
            {"path": "project/src/math.cpp", "configurations": ["missing"]},
        )
        assert invalid.is_error
    assert len(setup.peers) == 2  # Repeated operations retain the same sessions.
    request = next(
        message
        for message in setup.peers[0].sent
        if getattr(message, "method", "") == "textDocument/references"
    )
    assert request.params["context"]["includeDeclaration"] is False


@pytest.mark.anyio
async def test_workspace_edits_sync_sessions_and_keep_old_analysis_immutable(setup):
    """Verify workspace mutations synchronize sessions while earlier analysis resources remain
    unchanged.
    """
    async with Client(setup.server) as client:
        read = await client.call_tool("workspace_read_file", {"path": "project/src/math.cpp"})
        uri = read.structured_content["resources"]["clangd"]["uri"]
        frozen = (await client.read_resource(uri)).contents[0].text
        analysis = ClangdResource.model_validate_json(frozen)
        assert analysis.files[0].diagnostics[0].configurations == ["debug", "release"]
        assert analysis.files[0].highlighting[0].spans[0].kind == "keyword"
        repeated = await client.call_tool("workspace_read_file", {"path": "project/src/math.cpp"})
        assert not repeated.is_error
        for peer in setup.peers:
            assert sum(message.method == "textDocument/didOpen" for message in peer.sent) == 1
        edited = await client.call_tool(
            "workspace_edit_file",
            {"path": "project/src/math.cpp", "old_text": "left + right", "new_text": "left - right"},
        )
        assert not edited.is_error and edited.structured_content["resources"]["clangd"]["uri"] != uri
        assert (await client.read_resource(uri)).contents[0].text == frozen
        for running in setup.service.sessions.values():
            document = next(iter(running.session.documents.values()))
            assert document.version == 2 and "left - right" in document.text
        moved = await client.call_tool(
            "workspace_move",
            {"source": "project/src/math.cpp", "destination": "project/src/moved.cpp"},
        )
        assert not moved.is_error and moved.structured_content["resources"] == {}
    assert len(setup.peers) == 2
    for peer in setup.peers:
        changes = [message for message in peer.sent if message.method == "workspace/didChangeWatchedFiles"]
        assert len(changes) == 2
        assert [item["type"] for item in changes[-1].params["changes"]] == [3, 1]
    assert all(not running.session.documents for running in setup.service.sessions.values())
    assert not setup.service.lock.locked()


@pytest.mark.anyio
async def test_database_refresh_restarts_only_affected_session_and_removal_closes_it(setup):
    """Verify database changes replace only the affected session and removals close it."""
    original = setup.service.sessions["debug"].session
    retained = setup.service.sessions["release"].session
    setup.workspace.resolve_path("build/debug/compile_commands.json").write_text('[{"file":"changed.cpp"}]')
    await setup.cmake.publish_configurations(refresh=True)
    async with asyncio.timeout(5):
        while (running := setup.service.sessions.get("debug")) is None or running.session is original:
            await asyncio.sleep(0)
    assert setup.peers[0].closed
    assert setup.service.sessions["release"].session is retained
    del setup.cmake.configurations["debug"]
    await setup.cmake.publish_configurations()
    async with asyncio.timeout(5):
        while "debug" in setup.service.contexts:
            await asyncio.sleep(0)
    async with setup.service.lock:
        pass  # Wait for the reconciliation that removed the context to finish.
    assert set(setup.service.sessions) == {"release"}
    await setup.service.close()
    assert all(peer.closed for peer in setup.peers)
    assert not setup.cmake.configuration_subscribers


@pytest.mark.anyio
async def test_request_timeout_sends_cancellation_and_cleans_pending_state(setup):
    """Verify timed-out LSP requests send cancellation and leave no pending request state."""
    peer = setup.peers[0]
    peer.ignore.add("textDocument/hover")
    session = setup.service.sessions["debug"].session
    with pytest.raises(ClangdTimeoutError):
        await session.request("textDocument/hover", {}, timeout=0.01)
    assert not session.pending and not session.methods
    assert peer.sent[-1].method == "$/cancelRequest"
    assert peer.sent[-1].params["id"] == peer.sent[-2].id

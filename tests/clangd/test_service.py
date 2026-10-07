"""Core analysis and retained sessions, using a deterministic LSP peer."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.apps import Apps

from forgemcp.clangd.errors import (
    ClangdError,
    ClangdSessionError,
    ClangdStaleResultError,
    ClangdTimeoutError,
)
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

    span = {
        "start": {"line": 0, "character": 0},
        "end": {"line": 0, "character": 3},
    }

    def __init__(self, root):
        """Initialize a scripted peer with queues, recorded messages, and a shared sample range."""
        self.root = root
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.ignore = set()
        self.diagnostics = [
            {"range": self.span, "message": "sample warning", "severity": 2},
        ]
        self.tokens = [0, 0, 3, 0, 0]

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
                "textDocument/semanticTokens/full": {"data": self.tokens},
                "shutdown": None,
            }
            await self.incoming.put(LspResponse(message.id, results[message.method]))
        elif message.method == "textDocument/didOpen":
            document = message.params["textDocument"]
            await self.publish(document["uri"], document["version"])
        elif message.method == "textDocument/didClose":
            await self.publish(message.params["textDocument"]["uri"], None, [])
        elif message.method == "exit":
            await self.incoming.put(None)

    async def messages(self) -> AsyncGenerator[LspMessage]:
        """Yield queued server messages until the exit sentinel arrives."""
        while (message := await self.incoming.get()) is not None:
            yield message

    async def close(self):
        """Mark the scripted connection as closed."""
        self.closed = True

    async def publish(self, uri, version, diagnostics=None):
        """Queue diagnostics with optional versions, including clangd's close notification."""
        value = {
            "uri": uri,
            "diagnostics": self.diagnostics if diagnostics is None else diagnostics,
        }
        if version is not None:
            value["version"] = version
        await self.incoming.put(LspNotification("textDocument/publishDiagnostics", value))


def opened_documents(peer):
    """Return the complete buffers and versions supplied by didOpen notifications."""
    return [
        message.params["textDocument"]
        for message in peer.sent
        if message.method == "textDocument/didOpen"
    ]


def closed_documents(peer):
    """Return the URIs released by didClose notifications."""
    return [
        message.params["textDocument"]["uri"]
        for message in peer.sent
        if message.method == "textDocument/didClose"
    ]


async def wait_for_method(peer, method):
    """Wait until a scripted peer receives the requested method without a timing assumption."""
    async with asyncio.timeout(5):
        while not any(message.method == method for message in peer.sent):
            await asyncio.sleep(0)


async def read_analysis(client, result):
    """Decode the immutable clangd snapshot linked by a workspace tool result."""
    uri = result.structured_content["resources"]["clangd"]["uri"]
    resource = await client.read_resource(uri)
    return ClangdResource.model_validate_json(resource.contents[0].text)


def lsp_span(start, end, *, start_character=0, end_character=3):
    """Build a zero-based LSP range for filtering and diagnostic-version scenarios."""
    return {
        "start": {"line": start, "character": start_character},
        "end": {"line": end, "character": end_character},
    }


@pytest.fixture(name="setup")
async def clangd_setup(cpp_acceptance_project) -> AsyncGenerator[SimpleNamespace]:
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
async def test_workspace_edits_use_fresh_documents_and_keep_old_analysis_immutable(setup):
    """Verify each file operation closes its document without changing earlier snapshots."""
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
            assert [document["version"] for document in opened_documents(peer)] == [1, 2]
            assert len(closed_documents(peer)) == 2
        edited = await client.call_tool(
            "workspace_edit_file",
            {"path": "project/src/math.cpp", "old_text": "left + right", "new_text": "left - right"},
        )
        assert not edited.is_error and edited.structured_content["resources"]["clangd"]["uri"] != uri
        assert (await client.read_resource(uri)).contents[0].text == frozen
        for peer in setup.peers:
            document = opened_documents(peer)[-1]
            assert document["version"] == 3 and "left - right" in document["text"]
            assert len(closed_documents(peer)) == 3
        assert all(running.session.document is None for running in setup.service.sessions.values())
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
    assert all(running.session.document is None for running in setup.service.sessions.values())
    assert all(len(opened_documents(peer)) == 3 for peer in setup.peers)
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


@pytest.mark.anyio
async def test_header_edit_does_not_reopen_previous_sources_and_next_read_is_fresh(setup):
    """Verify header mutations leave old sources closed and their next analysis uses a new parse."""
    path = "project/src/math.cpp"
    async with Client(setup.server) as client:
        first = await client.call_tool("workspace_read_file", {"path": path})
        uri = first.structured_content["resources"]["clangd"]["uri"]
        frozen = (await client.read_resource(uri)).contents[0].text
        for peer in setup.peers:
            peer.diagnostics = [{"range": peer.span, "message": "after header edit", "severity": 2}]
        edited = await client.call_tool(
            "workspace_edit_file",
            {
                "path": "project/include/fixture/math.hpp",
                "old_text": "int add(int left, int right);",
                "new_text": "int add(int left, int right);\nint subtract(int left, int right);",
            },
        )
        assert not edited.is_error
        for peer in setup.peers:
            cpp_opens = [
                document
                for document in opened_documents(peer)
                if document["uri"].endswith("math.cpp")
            ]
            assert len(cpp_opens) == 1
        second = await client.call_tool("workspace_read_file", {"path": path})
        fresh = await read_analysis(client, second)
        assert fresh.files[0].diagnostics[0].diagnostics[0].message == "after header edit"
        assert (await client.read_resource(uri)).contents[0].text == frozen
    for peer in setup.peers:
        cpp_opens = [
            document
            for document in opened_documents(peer)
            if document["uri"].endswith("math.cpp")
        ]
        assert len(cpp_opens) == 2 and cpp_opens[0]["text"] == cpp_opens[1]["text"]
        assert cpp_opens[1]["version"] > cpp_opens[0]["version"]
        assert len(closed_documents(peer)) == len(opened_documents(peer))


@pytest.mark.anyio
async def test_document_context_rejects_overlap_and_diagnostics_for_inactive_paths(setup):
    """Verify one session cannot hold overlapping documents or reuse a closed diagnostic version."""
    session = setup.service.sessions["debug"].session
    path = WorkspacePath("project/src/math.cpp")
    other = WorkspacePath("project/include/fixture/math.hpp")
    text = setup.service.text(path)
    async with session.open_document(path, text) as version:
        with pytest.raises(ClangdSessionError):
            async with session.open_document(other, setup.service.text(other)):
                pytest.fail("A second active document must be rejected.")
        with pytest.raises(ClangdStaleResultError):
            await session.diagnostics(other, version, timeout=0.1)
        result = await session.diagnostics(path, version, timeout=0.1)
        assert result[0].message == "sample warning"
    assert session.document is None
    with pytest.raises(ClangdStaleResultError):
        await session.diagnostics(path, version, timeout=0.1)
    async with session.open_document(other, setup.service.text(other)) as reopened:
        assert reopened == version + 1
        assert session.document.path == other
    assert session.document is None


@pytest.mark.anyio
async def test_diagnostics_ignore_closed_old_unversioned_and_other_document_notifications(setup):
    """Verify delayed pushes cannot satisfy diagnostics for the next open document version."""
    peer = setup.peers[0]
    peer.ignore.add("textDocument/didOpen")
    session = setup.service.sessions["debug"].session
    path = WorkspacePath("project/src/math.cpp")
    uri = session.uri(path)
    async with session.open_document(path, setup.service.text(path)) as previous:
        pass
    await peer.publish(uri, previous)
    await session.request("textDocument/hover", {})
    assert session.document is None
    async with session.open_document(path, setup.service.text(path)) as version:
        cases = [
            (uri, previous),
            (uri, None),
            (uri, version + 1),
            (session.uri(WorkspacePath("project/include/fixture/math.hpp")), version),
        ]
        for wrong_uri, wrong_version in cases:
            await peer.publish(wrong_uri, wrong_version)
            await session.request("textDocument/hover", {})
            assert session.document.diagnostics_version is None
        accepted = [{"range": peer.span, "message": "current", "severity": 2}]
        await peer.publish(uri, version, accepted)
        result = await session.diagnostics(path, version, timeout=0.1)
        assert [diagnostic.message for diagnostic in result] == ["current"]
    assert session.document is None and len(closed_documents(peer)) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["timeout", "cancel"])
async def test_interrupted_analysis_closes_document_session_and_lock(setup, failure):
    """Verify interrupted analysis closes its active buffer and cleans up the failed context."""
    peer = setup.peers[0]
    peer.ignore.add("textDocument/hover")
    session = setup.service.sessions["debug"].session
    path = WorkspacePath("project/src/math.cpp")

    async def run(active, version):
        """Block a document request until its deadline or the test cancellation arrives."""
        assert active.document.version == version
        await active.request(
            "textDocument/hover",
            {"textDocument": {"uri": active.uri(path)}},
            timeout=0.01 if failure == "timeout" else 5,
        )
        return HoverResult(
            configurations=["debug"],
            path=path,
            position=Position(line=1, character=0),
            hover=None,
        )

    task = asyncio.create_task(
        setup.service.file_operation(
            path,
            ["debug"],
            run,
            on_progress=None,
            timeout=5,
        ),
    )
    if failure == "cancel":
        await wait_for_method(peer, "textDocument/hover")
        task.cancel()
    expected = asyncio.CancelledError if failure == "cancel" else ClangdError
    with pytest.raises(expected):
        await task
    assert session.document is None and peer.closed
    assert set(setup.service.sessions) == {"release"}
    assert not setup.service.lock.locked() and not session.pending and not session.methods
    assert len(opened_documents(peer)) == len(closed_documents(peer)) == 1
    methods = [message.method for message in peer.sent]
    assert methods.index("$/cancelRequest") < methods.index("textDocument/didClose")
    assert methods.index("textDocument/didClose") < methods.index("shutdown")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool, method",
    [
        ("diagnostics", None),
        ("hover", "textDocument/hover"),
        ("definition", "textDocument/definition"),
        ("references", "textDocument/references"),
        ("document_symbols", "textDocument/documentSymbol"),
        ("workspace_symbols", "workspace/symbol"),
    ],
)
async def test_clangd_tools_open_only_explicitly_selected_configurations(setup, tool, method):
    """Verify explicit configuration filters limit analysis to the selected retained session."""
    arguments = {"configurations": ["debug"]}
    if tool != "workspace_symbols":
        arguments["path"] = "project/src/math.cpp"
    if tool in ("hover", "definition", "references"):
        arguments["position"] = {"line": 1, "character": 0}
    if tool == "workspace_symbols":
        arguments["query"] = "add"
    async with Client(setup.server) as client:
        response = await client.call_tool(f"clangd_{tool}", arguments)
        assert not response.is_error, response.content
        result = response.structured_content["result"]
        assert result[0]["configurations"] == ["debug"]
        for invalid in ("missing", "unavailable"):
            response = await client.call_tool(
                f"clangd_{tool}",
                {**arguments, "configurations": [invalid]},
            )
            assert response.is_error
    assert len(setup.peers) == 2
    assert not opened_documents(setup.peers[1])
    if method is not None:
        assert any(message.method == method for message in setup.peers[0].sent)
        assert not any(message.method == method for message in setup.peers[1].sent)
    if tool != "workspace_symbols":
        assert len(opened_documents(setup.peers[0])) == len(closed_documents(setup.peers[0])) == 1
    assert all(running.session.document is None for running in setup.service.sessions.values())


@pytest.mark.anyio
async def test_new_configuration_starts_session_before_its_first_file_request(setup):
    """Verify CMake publication still starts clangd eagerly while keeping its documents closed."""
    assert len(setup.peers) == 2 and set(setup.service.sessions) == {"debug", "release"}
    setup.cmake.configurations["extra"] = setup.cmake.configurations["debug"].model_copy(
        update={"id": "extra"},
        deep=True,
    )
    await setup.cmake.publish_configurations()
    async with asyncio.timeout(5):
        while "extra" not in setup.service.sessions:
            await asyncio.sleep(0)
    assert len(setup.peers) == 3 and not any(peer.closed for peer in setup.peers)
    assert all(not opened_documents(peer) for peer in setup.peers)
    assert all(running.session.document is None for running in setup.service.sessions.values())


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["read_file", "write_file", "edit_file"])
async def test_workspace_analysis_timeout_keeps_successful_file_operation(setup, monkeypatch, tool):
    """Verify timed-out enrichment keeps completed file changes and closes language sessions."""
    monkeypatch.setattr(setup.service, "ANALYSIS_TIMEOUT", 0.03)
    for peer in setup.peers:
        peer.ignore.add("textDocument/semanticTokens/full")
    path = WorkspacePath("project/src/math.cpp")
    arguments = {"path": str(path)}
    if tool == "write_file":
        arguments["text"] = "int replacement;\n"
    if tool == "edit_file":
        arguments.update({"old_text": "left + right", "new_text": "left - right"})
    async with Client(setup.server) as client:
        response = await client.call_tool(f"workspace_{tool}", arguments)
        assert not response.is_error, response.content
        assert "clangd" not in response.structured_content["resources"]
        if tool == "write_file":
            assert setup.service.text(path) == arguments["text"]
        if tool == "edit_file":
            assert "left - right" in setup.service.text(path)
    assert not setup.service.lock.locked() and not setup.service.sessions
    assert all(peer.closed for peer in setup.peers)
    for peer in setup.peers:
        assert len(opened_documents(peer)) == len(closed_documents(peer)) == 1


@pytest.mark.anyio
async def test_partial_reads_parse_full_buffer_and_filter_intersecting_annotations(setup):
    """Verify partial views preserve source coordinates and exclude unselected source lines."""
    path = WorkspacePath("project/src/math.cpp")
    text = "int first;\nint second;\nint third;\nint fourth;\nint fifth;\n"
    setup.workspace.resolve_workspace_path(path).write_text(
        text,
        encoding="utf-8",
        newline="",
    )
    spans = [
        ("ends before selected lines", lsp_span(0, 1, end_character=0)),
        ("crosses selected start", lsp_span(0, 1, end_character=1)),
        ("inside", lsp_span(1, 1)),
        ("crosses selected end", lsp_span(2, 3, end_character=0)),
        ("after selected lines", lsp_span(3, 3)),
    ]
    for peer in setup.peers:
        peer.diagnostics = [
            {"range": span, "message": message, "severity": 2}
            for message, span in spans
        ]
        peer.tokens = [0, 0, 3, 0, 0] + [1, 0, 3, 0, 0] * 4
    async with Client(setup.server) as client:
        response = await client.call_tool(
            "workspace_read_file",
            {"path": str(path), "start_line": 2, "end_line": 3},
        )
        assert response.structured_content["text"] == "int second;\nint third;\n"
        analysis = (await read_analysis(client, response)).files[0]
    diagnostics = analysis.diagnostics[0].diagnostics
    assert [item.message for item in diagnostics] == [
        "crosses selected start",
        "inside",
        "crosses selected end",
    ]
    assert diagnostics[0].range.start.line == 1 and diagnostics[0].range.end.line == 2
    assert diagnostics[-1].range.end.line == 4 and diagnostics[-1].range.end.character == 0
    assert [span.range.start.line for span in analysis.highlighting[0].spans] == [2, 3]
    assert analysis.diagnostics[0].configurations == ["debug", "release"]
    for peer in setup.peers:
        assert opened_documents(peer)[0]["text"] == text and len(closed_documents(peer)) == 1


@pytest.mark.anyio
async def test_empty_read_returns_no_diagnostics_or_highlighting(setup):
    """Verify an excerpt beyond EOF does not include annotations from the complete source buffer."""
    async with Client(setup.server) as client:
        response = await client.call_tool(
            "workspace_read_file",
            {"path": "project/src/math.cpp", "start_line": 100},
        )
        assert not response.is_error and response.structured_content["text"] == ""
        analysis = await read_analysis(client, response)
        for file in analysis.files:
            assert all(not item.diagnostics for item in file.diagnostics)
            assert all(not item.spans for item in file.highlighting)
    assert all(running.session.document is None for running in setup.service.sessions.values())

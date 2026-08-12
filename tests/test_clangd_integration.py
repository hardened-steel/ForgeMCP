"""Protocol-fake tests for clangd lifecycle, synchronization, and tool schemas."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from forgemcp.clangd import (
    ClangdEditConflictError,
    ClangdHandleExpiredError,
    ClangdNotStartedError,
    ClangdService,
    ClangdSessionState,
    ClangdUnsupportedActionError,
    ClangdUnsupportedWorkspaceEditError,
    ClangdRequestCancelledError,
    ClangdContentModifiedError,
)
from forgemcp.core.application import ForgeApplication
from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.models import Position, Range
from forgemcp.processes import ProcessRuntime
from forgemcp.lsp import PositionEncoding
from forgemcp.workspace import WorkspaceService


def _frame(value: object) -> bytes:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload


class _FakeHandle:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = _FakeWriter(self)
        self.returncode: int | None = None
        self._finished = asyncio.Event()

    async def wait(self) -> int:
        await self._finished.wait()
        assert self.returncode is not None
        return self.returncode

    async def terminate(self) -> None:
        self.finish()

    async def aclose(self) -> None:
        self.finish()

    def finish(self) -> None:
        if self.returncode is None:
            self.returncode = 0
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._finished.set()


class _FakeWriter:
    def __init__(self, handle: _FakeHandle) -> None:
        self._handle = handle
        self.closed = False
        self.document_uri: str | None = None
        self.document_version = 0
        self.stale_diagnostics = False
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)
        _, payload = data.split(b"\r\n\r\n", 1)
        value = json.loads(payload)
        method = value.get("method")
        if method == "initialize":
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": {"capabilities": {"positionEncoding": "utf-16"}}})
        elif method == "textDocument/didOpen":
            document = value["params"]["textDocument"]
            self.document_uri = document["uri"]
            self.document_version = document["version"]
            self._publish_diagnostics()
        elif method == "textDocument/didChange":
            document = value["params"]["textDocument"]
            self.document_version = document["version"]
            self._publish_diagnostics()
        elif method == "textDocument/hover":
            self._send(
                {"jsonrpc": "2.0", "id": value["id"], "result": {"contents": {"kind": "markdown", "value": "**hover**"}, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}}}}
            )
        elif method == "textDocument/definition":
            self._send(
                {"jsonrpc": "2.0", "id": value["id"], "result": [
                    {"uri": self.document_uri, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}}},
                    {"uri": "file:///outside-workspace.cpp", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}},
                ]}
            )
        elif method in {"textDocument/declaration", "textDocument/typeDefinition", "textDocument/implementation"}:
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": []})
        elif method == "textDocument/references":
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": []})
        elif method == "textDocument/documentSymbol":
            self._send(
                {"jsonrpc": "2.0", "id": value["id"], "result": [{"name": "main", "kind": 12, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}}, "selectionRange": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}}}]}
            )
        elif method == "workspace/symbol":
            self._send(
                {"jsonrpc": "2.0", "id": value["id"], "result": [
                    {"name": "main", "kind": 12, "location": {"uri": self.document_uri, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}}}},
                    {"name": "external", "kind": 12, "location": {"uri": "file:///outside-workspace.cpp", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}}},
                ]}
            )
        elif method == "textDocument/completion":
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": {"isIncomplete": True, "items": [
                {"label": "snippet", "kind": 15, "insertText": "call(${1:value})", "insertTextFormat": 2},
                {"label": "plain", "kind": 3, "textEdit": {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}, "newText": "plain"}},
            ]}})
        elif method == "textDocument/signatureHelp":
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": {"signatures": [{"label": "f(int value)", "parameters": [{"label": "int value"}]}], "activeSignature": 0, "activeParameter": 0}})
        elif method == "textDocument/prepareRename":
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}}, "placeholder": "name"}})
        elif method == "textDocument/rename":
            result = None if value["params"]["newName"] == "noop" else {"changes": {self.document_uri: [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}}, "newText": value["params"]["newName"]}]}}
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": result})
        elif method == "textDocument/codeAction":
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": [
                {"title": "Apply fake edit", "kind": "quickfix", "edit": {"changes": {self.document_uri: [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}}, "newText": "fixed"}]}}},
                {"title": "Command only", "command": {"title": "unsafe", "command": "workspace.executeCommand"}},
                {"title": "Resolve fake"},
            ]})
        elif method == "codeAction/resolve":
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": {**value["params"], "edit": {"changes": {self.document_uri: [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}, "newText": "resolved "}]}}}})
        elif method == "textDocument/formatting":
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": []})
        elif method == "textDocument/rangeFormatting":
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}}, "newText": "formatted"}]})
        elif method in {"textDocument/prepareCallHierarchy", "textDocument/prepareTypeHierarchy"}:
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": [{"name": "item", "kind": 12, "uri": self.document_uri, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}}, "selectionRange": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}}}]})
        elif method == "callHierarchy/incomingCalls":
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": []})
        elif method == "callHierarchy/outgoingCalls":
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": []})
        elif method in {"typeHierarchy/supertypes", "typeHierarchy/subtypes"}:
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": []})
        elif method == "textDocument/switchSourceHeader":
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": self.document_uri})
        elif method == "fake/cancelled":
            self._send({"jsonrpc": "2.0", "id": value["id"], "error": {"code": -32800, "message": "cancelled"}})
        elif method == "fake/modified":
            self._send({"jsonrpc": "2.0", "id": value["id"], "error": {"code": -32801, "message": "modified"}})
        elif method == "shutdown":
            self._send({"jsonrpc": "2.0", "id": value["id"], "result": None})
        elif method == "exit":
            self._handle.finish()

    def _publish_diagnostics(self) -> None:
        assert self.document_uri is not None
        version = self.document_version - 1 if self.stale_diagnostics else self.document_version
        self._send(
            {"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": self.document_uri, "version": version, "diagnostics": [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}}, "severity": 2, "code": "fake", "source": "fake-clangd", "message": "fake warning"}]}}
        )

    def _send(self, value: object) -> None:
        self._handle.stdout.feed_data(_frame(value))

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FakeProcessRuntime:
    def __init__(self) -> None:
        self.handle: _FakeHandle | None = None
        self.argv: tuple[str, ...] | None = None

    async def start(self, argv, *, cwd: str):
        self.argv = tuple(argv)
        assert cwd == "."
        self.handle = _FakeHandle()
        return self.handle


def _service(root: Path) -> tuple[ClangdService, _FakeProcessRuntime]:
    config = ForgeConfig(workspace_root=root)
    workspace = WorkspaceService(config, create_logger("CRITICAL"))
    runtime = _FakeProcessRuntime()
    return ClangdService(config, workspace, runtime), runtime  # type: ignore[arg-type]


def test_clangd_service_manages_lifecycle_documents_diagnostics_and_external_results(tmp_path: Path):
    async def exercise() -> None:
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "compile_commands.json").write_text("[]", encoding="utf-8")
        source = tmp_path / "main.cpp"
        source.write_text("main😀\n", encoding="utf-8")
        service, runtime = _service(tmp_path)

        with pytest.raises(ClangdNotStartedError):
            await service.hover("main.cpp", Position(line=0, column=0))

        started = await service.start("build")
        assert started.status.state is ClangdSessionState.RUNNING
        assert runtime.argv == ("clangd", "--compile-commands-dir=build")

        diagnostics = await service.diagnostics("main.cpp", timeout_seconds=0.1)
        assert diagnostics.complete is True
        assert diagnostics.document_version == 1
        assert diagnostics.diagnostics[0].severity.value == "warning"

        hover = await service.hover("main.cpp", Position(line=0, column=4))
        assert hover.contents == "**hover**"
        definition = await service.definition("main.cpp", Position(line=0, column=0))
        assert len(definition.locations) == 1
        assert definition.omitted_external_results == 1
        assert definition.locations[0].path == "main.cpp"
        assert (await service.references("main.cpp", Position(line=0, column=0))).locations == ()
        assert (await service.document_symbols("main.cpp")).symbols[0].kind == "function"
        workspace_symbols = await service.workspace_symbols("main", limit=1)
        assert len(workspace_symbols.symbols) == 1
        assert workspace_symbols.omitted_external_results == 1

        source.write_text("changed\n", encoding="utf-8")
        updated = await service.diagnostics("main.cpp", timeout_seconds=0.1)
        assert updated.complete is True
        assert updated.document_version == 2
        await service.aclose()
        await service.aclose()
        assert service.state is ClangdSessionState.STOPPED

    asyncio.run(exercise())


def test_clangd_service_marks_version_mismatched_diagnostics_stale(tmp_path: Path):
    async def exercise() -> None:
        (tmp_path / "db").mkdir()
        (tmp_path / "db" / "compile_commands.json").write_text("[]", encoding="utf-8")
        (tmp_path / "source.cpp").write_text("int x;\n", encoding="utf-8")
        service, runtime = _service(tmp_path)
        await service.start("db")
        assert runtime.handle is not None
        runtime.handle.stdin.stale_diagnostics = True
        result = await service.diagnostics("source.cpp", timeout_seconds=0.1)
        assert result.complete is False
        assert result.stale is True
        assert result.diagnostics == ()
        await service.aclose()

    asyncio.run(exercise())


def test_clangd_process_crash_transitions_the_managed_service_to_failed(tmp_path: Path):
    async def exercise() -> None:
        (tmp_path / "db").mkdir()
        (tmp_path / "db" / "compile_commands.json").write_text("[]", encoding="utf-8")
        service, runtime = _service(tmp_path)
        await service.start("db")
        assert runtime.handle is not None
        runtime.handle.finish()
        for _ in range(10):
            if service.state is ClangdSessionState.FAILED:
                break
            await asyncio.sleep(0)
        assert service.state is ClangdSessionState.FAILED
        await service.aclose()

    asyncio.run(exercise())


def test_clangd_phase_two_fake_tools_apply_only_atomic_workspace_edits_and_invalidate_handles(tmp_path: Path):
    async def exercise() -> None:
        (tmp_path / "db").mkdir()
        (tmp_path / "db" / "compile_commands.json").write_text("[]", encoding="utf-8")
        source = tmp_path / "main.cpp"
        source.write_text("name()\n", encoding="utf-8")
        service, _ = _service(tmp_path)
        await service.start("db")

        completion = await service.completion("main.cpp", Position(line=0, column=0), limit=1)
        assert completion.items[0].insert_text_format.value == "snippet"
        assert completion.truncated is True
        signature = await service.signature_help("main.cpp", Position(line=0, column=0))
        assert signature.signatures[0].label == "f(int value)"
        assert (await service.declaration("main.cpp", Position(line=0, column=0))).locations == ()
        assert (await service.type_definition("main.cpp", Position(line=0, column=0))).locations == ()
        assert (await service.implementation("main.cpp", Position(line=0, column=0))).locations == ()
        assert (await service.prepare_rename("main.cpp", Position(line=0, column=0))).range is not None

        snapshot = service._documents[next(iter(service._documents))].snapshot
        no_op = await service.rename("main.cpp", Position(line=0, column=0), "noop", expected_sha256=snapshot.sha256)
        assert no_op.edit.no_op is True
        with pytest.raises(ClangdEditConflictError):
            await service.rename("main.cpp", Position(line=0, column=0), "renamed", expected_sha256="0" * 64)
        renamed = await service.rename("main.cpp", Position(line=0, column=0), "renamed", expected_sha256=snapshot.sha256)
        assert renamed.edit.applied is True
        assert source.read_text(encoding="utf-8") == "renamed()\n"
        assert (await service.format_document("main.cpp")).edit.no_op is True
        with pytest.raises(ClangdEditConflictError):
            await service.format_document("main.cpp", expected_sha256="0" * 64)
        formatted = await service.format_range("main.cpp", Range(start=Position(line=0, column=0), end=Position(line=0, column=4)))
        assert formatted.edit.applied is True
        assert source.read_text(encoding="utf-8") == "formattedmed()\n"

        # Restore a predictable prefix, then list actions. Their handles are invalidated by any document change.
        source.write_text("name()\n", encoding="utf-8")
        actions = await service.code_actions("main.cpp", Range(start=Position(line=0, column=0), end=Position(line=0, column=4)))
        assert len(actions.actions) == 3
        command = next(action for action in actions.actions if action.command_only)
        with pytest.raises(ClangdUnsupportedActionError):
            await service.apply_code_action(command.action_id)
        edit_action = next(action for action in actions.actions if action.has_workspace_edit)
        applied = await service.apply_code_action(edit_action.action_id)
        assert applied.applied is True
        assert source.read_text(encoding="utf-8") == "fixed()\n"
        with pytest.raises(ClangdHandleExpiredError):
            await service.apply_code_action(edit_action.action_id)

        source.write_text("name()\n", encoding="utf-8")
        resolved_actions = await service.code_actions("main.cpp", Range(start=Position(line=0, column=0), end=Position(line=0, column=4)))
        resolve_action = next(action for action in resolved_actions.actions if action.requires_resolve)
        resolved = await service.apply_code_action(resolve_action.action_id)
        assert resolved.applied is True
        assert source.read_text(encoding="utf-8") == "resolved name()\n"

        expiring_actions = await service.code_actions("main.cpp", Range(start=Position(line=0, column=0), end=Position(line=0, column=4)))
        expired_id = expiring_actions.actions[0].action_id
        service._actions[expired_id] = replace(service._actions[expired_id], expires_at=0.0)
        with pytest.raises(ClangdHandleExpiredError):
            await service.apply_code_action(expired_id)

        call = await service.prepare_call_hierarchy("main.cpp", Position(line=0, column=0))
        assert (await service.incoming_calls(call.items[0].item_id)).calls == ()
        source.write_text("another()\n", encoding="utf-8")
        await service.hover("main.cpp", Position(line=0, column=0))
        with pytest.raises(ClangdHandleExpiredError):
            await service.outgoing_calls(call.items[0].item_id)
        types = await service.prepare_type_hierarchy("main.cpp", Position(line=0, column=0))
        assert (await service.supertypes(types.items[0].item_id)).items == ()
        assert (await service.switch_source_header("main.cpp")).path == "main.cpp"
        await service.aclose()

    asyncio.run(exercise())


def test_workspace_edit_engine_rejects_stale_versions_external_uris_and_resource_operations(tmp_path: Path):
    async def exercise() -> None:
        (tmp_path / "db").mkdir()
        (tmp_path / "db" / "compile_commands.json").write_text("[]", encoding="utf-8")
        (tmp_path / "main.cpp").write_text("name()\n", encoding="utf-8")
        service, _ = _service(tmp_path)
        await service.start("db")
        document, _ = await service._synchronize_document("main.cpp")
        with pytest.raises(ClangdEditConflictError):
            await service._apply_workspace_edit({"documentChanges": [{"textDocument": {"uri": document.uri, "version": 0}, "edits": []}]}, anchor=document)
        with pytest.raises(ClangdUnsupportedWorkspaceEditError):
            await service._apply_workspace_edit({"changes": {"file:///outside.cpp": []}}, anchor=document)
        with pytest.raises(ClangdUnsupportedWorkspaceEditError):
            await service._apply_workspace_edit({"documentChanges": [{"kind": "rename", "oldUri": document.uri, "newUri": document.uri}]}, anchor=document)
        await service.aclose()

    asyncio.run(exercise())


def test_clangd_maps_request_cancelled_and_content_modified_errors_separately(tmp_path: Path):
    async def exercise() -> None:
        (tmp_path / "db").mkdir()
        (tmp_path / "db" / "compile_commands.json").write_text("[]", encoding="utf-8")
        service, _ = _service(tmp_path)
        await service.start("db")
        with pytest.raises(ClangdRequestCancelledError):
            await service._request("fake/cancelled", {})
        with pytest.raises(ClangdContentModifiedError):
            await service._request("fake/modified", {})
        await service.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("encoding", "end_character"),
    [(PositionEncoding.UTF8, 5), (PositionEncoding.UTF16, 3), (PositionEncoding.UTF32, 2)],
)
def test_workspace_edit_engine_converts_non_bmp_lsp_coordinates_and_commits_multi_file_atomically(
    tmp_path: Path, encoding: PositionEncoding, end_character: int
):
    async def exercise() -> None:
        (tmp_path / "db").mkdir()
        (tmp_path / "db" / "compile_commands.json").write_text("[]", encoding="utf-8")
        first = tmp_path / "first.cpp"
        second = tmp_path / "second.cpp"
        first.write_text("A😀BC\n", encoding="utf-8")
        second.write_text("two\n", encoding="utf-8")
        service, _ = _service(tmp_path)
        await service.start("db")
        service._position_encoding = encoding
        document, _ = await service._synchronize_document("first.cpp")
        result = await service._apply_workspace_edit(
            {
                "changes": {
                    document.uri: [{"range": {"start": {"line": 0, "character": 1}, "end": {"line": 0, "character": end_character}}, "newText": "X"}],
                    second.as_uri(): [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}, "newText": "TWO"}],
                }
            },
            anchor=document,
        )
        assert result.applied is True
        assert len(result.changes) == 2
        assert first.read_text(encoding="utf-8") == "AXBC\n"
        assert second.read_text(encoding="utf-8") == "TWO\n"
        await service.aclose()

    asyncio.run(exercise())


def test_builtin_clangd_plugin_registers_every_phase_one_tool_with_flat_schemas(tmp_path: Path):
    async def exercise() -> None:
        application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
        from forgemcp.server import create_server

        server = create_server(lambda: application)
        async with server._mcp_server.lifespan(server._mcp_server):  # type: ignore[attr-defined]
            tools = {tool.name: tool for tool in await server.list_tools()}
            names = {name for name in tools if name.startswith("clangd__")}
            assert names == {
                "clangd__status", "clangd__start", "clangd__stop", "clangd__diagnostics", "clangd__hover",
                "clangd__definition", "clangd__references", "clangd__document_symbols", "clangd__workspace_symbols",
                "clangd__completion", "clangd__signature_help", "clangd__declaration", "clangd__type_definition",
                "clangd__implementation", "clangd__prepare_rename", "clangd__rename", "clangd__code_actions",
                "clangd__apply_code_action", "clangd__format_document", "clangd__format_range",
                "clangd__prepare_call_hierarchy", "clangd__incoming_calls", "clangd__outgoing_calls",
                "clangd__prepare_type_hierarchy", "clangd__supertypes", "clangd__subtypes", "clangd__switch_source_header",
            }
            expected_properties = {
                "clangd__status": set(),
                "clangd__start": {"compile_commands_dir"},
                "clangd__stop": set(),
                "clangd__diagnostics": {"path", "timeout_seconds"},
                "clangd__hover": {"path", "position"},
                "clangd__definition": {"path", "position"},
                "clangd__references": {"path", "position", "include_declaration"},
                "clangd__document_symbols": {"path"},
                "clangd__workspace_symbols": {"query", "limit"},
                "clangd__completion": {"path", "position", "limit"},
                "clangd__signature_help": {"path", "position"},
                "clangd__declaration": {"path", "position"},
                "clangd__type_definition": {"path", "position"},
                "clangd__implementation": {"path", "position"},
                "clangd__prepare_rename": {"path", "position"},
                "clangd__rename": {"path", "position", "new_name", "expected_sha256"},
                "clangd__code_actions": {"path", "range", "diagnostics", "kinds", "limit"},
                "clangd__apply_code_action": {"action_id", "expected_sha256"},
                "clangd__format_document": {"path", "expected_sha256"},
                "clangd__format_range": {"path", "expected_sha256", "range"},
                "clangd__prepare_call_hierarchy": {"path", "position"},
                "clangd__incoming_calls": {"item_id", "limit"},
                "clangd__outgoing_calls": {"item_id", "limit"},
                "clangd__prepare_type_hierarchy": {"path", "position"},
                "clangd__supertypes": {"item_id", "limit"},
                "clangd__subtypes": {"item_id", "limit"},
                "clangd__switch_source_header": {"path"},
            }
            for name, properties in expected_properties.items():
                assert set(tools[name].inputSchema["properties"]) == properties
            assert tools["clangd__start"].inputSchema["required"] == ["compile_commands_dir"]
            assert set(tools["clangd__code_actions"].inputSchema["required"]) == {"path", "range"}

    asyncio.run(exercise())


def test_clangd_file_uri_escaping_remains_workspace_scoped(tmp_path: Path):
    config = ForgeConfig(workspace_root=tmp_path)
    workspace = WorkspaceService(config, create_logger("CRITICAL"))
    runtime = _FakeProcessRuntime()
    service = ClangdService(config, workspace, runtime)  # type: ignore[arg-type]
    source = tmp_path / "space dir"
    source.mkdir()
    file_path = source / "name #.cpp"
    file_path.write_text("int x;\n", encoding="utf-8")

    assert service._path_from_uri(file_path.as_uri()) == "space dir/name #.cpp"
    assert service._path_from_uri("file:///outside-workspace.cpp") is None


def test_explicit_clangd_path_is_allowed_by_the_default_process_policy(tmp_path: Path):
    executable = Path(sys.executable).resolve()
    config = ForgeConfig(workspace_root=tmp_path, clangd_path=executable)
    runtime = ProcessRuntime(config, create_logger("CRITICAL"))

    assert config.clangd_path == executable
    assert executable in runtime.policy.allowed_executable_paths


@pytest.mark.skipif(
    not os.environ.get("FORGEMCP_CLANGD") and shutil.which("clangd") is None,
    reason="optional real-clangd gate requires FORGEMCP_CLANGD or clangd on PATH",
)
def test_real_clangd_phase_one_gate_with_compile_commands(tmp_path: Path):
    """Run the complete phase-1 public flow against a configured real clangd."""
    async def exercise() -> None:
        (tmp_path / "build").mkdir()
        source = tmp_path / "main.cpp"
        source.write_text(
            "int add(int value) { return value; }\n"
            "int main() { return add(1); }\n",
            encoding="utf-8",
        )
        (tmp_path / "build" / "compile_commands.json").write_text(
            json.dumps([{"directory": str(tmp_path), "file": str(source), "command": "clang++ -c main.cpp"}]),
            encoding="utf-8",
        )
        config = ForgeConfig.from_environment(
            {
                "FORGEMCP_WORKSPACE": str(tmp_path),
                **({"FORGEMCP_CLANGD": os.environ["FORGEMCP_CLANGD"]} if os.environ.get("FORGEMCP_CLANGD") else {}),
            }
        )
        logger = create_logger("CRITICAL")
        service = ClangdService(config, WorkspaceService(config, logger), ProcessRuntime(config, logger))
        started = await service.start("build")
        assert started.status.state is ClangdSessionState.RUNNING
        diagnostics = await service.diagnostics("main.cpp", timeout_seconds=10.0)
        assert diagnostics.snapshot.exists is True
        assert diagnostics.stale is False
        hover = await service.hover("main.cpp", Position(line=1, column=20))
        assert hover.contents is not None
        definition = await service.definition("main.cpp", Position(line=1, column=20))
        assert definition.locations
        references = await service.references("main.cpp", Position(line=1, column=20))
        assert references.locations
        await service.aclose()
        assert service.state is ClangdSessionState.STOPPED

    asyncio.run(exercise())


@pytest.mark.skipif(
    not os.environ.get("FORGEMCP_CLANGD") and shutil.which("clangd") is None,
    reason="optional real-clangd phase-2 gate requires FORGEMCP_CLANGD or clangd on PATH",
)
def test_real_clangd_phase_two_rename_gate(tmp_path: Path):
    """Prove a real clangd WorkspaceEdit is applied atomically through WorkspaceService."""
    async def exercise() -> None:
        (tmp_path / "build").mkdir()
        source = tmp_path / "rename.cpp"
        source.write_text("int value = 1;\nint main() { return value; }\n", encoding="utf-8")
        (tmp_path / "build" / "compile_commands.json").write_text(
            json.dumps([{"directory": str(tmp_path), "file": str(source), "command": "clang++ -c rename.cpp"}]),
            encoding="utf-8",
        )
        config = ForgeConfig.from_environment(
            {
                "FORGEMCP_WORKSPACE": str(tmp_path),
                **({"FORGEMCP_CLANGD": os.environ["FORGEMCP_CLANGD"]} if os.environ.get("FORGEMCP_CLANGD") else {}),
            }
        )
        logger = create_logger("CRITICAL")
        service = ClangdService(config, WorkspaceService(config, logger), ProcessRuntime(config, logger))
        await service.start("build")
        document, _ = await service._synchronize_document("rename.cpp")
        result = await service.rename(
            "rename.cpp", Position(line=0, column=4), "renamed_value", expected_sha256=document.snapshot.sha256
        )
        assert result.edit.applied is True
        assert "renamed_value" in source.read_text(encoding="utf-8")
        await service.aclose()

    asyncio.run(exercise())

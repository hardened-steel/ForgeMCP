"""Protocol-fake tests for clangd lifecycle, synchronization, and tool schemas."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

from forgemcp.clangd import ClangdNotStartedError, ClangdService, ClangdSessionState
from forgemcp.core.application import ForgeApplication
from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.models import Position
from forgemcp.processes import ProcessRuntime
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
            }
            for name, properties in expected_properties.items():
                assert set(tools[name].inputSchema["properties"]) == properties
            assert tools["clangd__start"].inputSchema["required"] == ["compile_commands_dir"]

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


@pytest.mark.skipif(shutil.which("clangd") is None, reason="optional real-clangd smoke test requires clangd on PATH")
def test_real_clangd_smoke_path_with_compile_commands(tmp_path: Path):
    """Optional host smoke test: protocol remains managed by ProcessRuntime."""
    async def exercise() -> None:
        (tmp_path / "build").mkdir()
        source = tmp_path / "main.cpp"
        source.write_text("int main() { return missing_name; }\n", encoding="utf-8")
        (tmp_path / "build" / "compile_commands.json").write_text(
            json.dumps([{"directory": str(tmp_path), "file": str(source), "command": "clang++ -c main.cpp"}]),
            encoding="utf-8",
        )
        config = ForgeConfig(workspace_root=tmp_path)
        logger = create_logger("CRITICAL")
        service = ClangdService(config, WorkspaceService(config, logger), ProcessRuntime(config, logger))
        started = await service.start("build")
        assert started.status.state is ClangdSessionState.RUNNING
        result = await service.diagnostics("main.cpp", timeout_seconds=10.0)
        assert result.snapshot.exists is True
        assert result.stale is False
        await service.aclose()

    asyncio.run(exercise())

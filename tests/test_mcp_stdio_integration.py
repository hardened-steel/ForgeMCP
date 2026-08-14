"""End-to-end MCP stdio coverage using the SDK client transport."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


_EXPECTED_TOOLS = {
    "server_status",
    "cmake__status",
    "cmake__list_presets",
    "cmake__configure",
    "cmake__list_targets",
    "cmake__build",
    "cmake__ctest_list_tests",
    "cmake__ctest_run",
    "clangd__status",
    "clangd__rename",
    "clangd__code_actions",
    "clangd__completion",
    "debugger__status",
    "debugger__list_adapters",
    "quality__status",
    "clang_format__check",
    "clang_format__apply",
    "clang_tidy__list_checks",
    "clang_tidy__run",
    "sanitizer__parse_report",
}

_DEBUGGER_TOOLS = {
    "debugger__status", "debugger__list_adapters", "debugger__launch", "debugger__stop",
    "debugger__set_breakpoints", "debugger__continue", "debugger__pause", "debugger__step_over",
    "debugger__step_in", "debugger__step_out", "debugger__threads", "debugger__stack_trace",
    "debugger__scopes", "debugger__variables", "debugger__evaluate", "debugger__events",
}

_DEFAULT_LLDB_DAP = Path(r"C:\Program Files\LLVM\bin\lldb-dap.exe")
_DEFAULT_CLANG = Path(r"C:\Program Files\LLVM\bin\clang.exe")


def _json_tool_content(result: object) -> dict[str, object]:
    """Decode FastMCP's JSON text response without relying on private SDK state."""
    content = getattr(result, "content")
    assert len(content) == 1
    text = getattr(content[0], "text")
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload


def _local_llvm_path(variable: str, default: Path) -> Path | None:
    configured = os.environ.get(variable)
    candidate = Path(configured) if configured else default
    return candidate if candidate.is_file() else None


def test_stdio_mcp_end_to_end_registers_tools_serializes_responses_and_closes_lifespan(
    tmp_path: Path,
):
    """Exercise initialize, tools/list, calls, and shutdown through real stdio."""

    async def exercise() -> str:
        errors_path = tmp_path / "server-stderr.log"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "forgemcp.server"],
            cwd=Path.cwd(),
            env={
                **os.environ,
                "FORGEMCP_WORKSPACE": str(tmp_path),
                "FORGEMCP_LOG_LEVEL": "INFO",
            },
        )
        with errors_path.open("w", encoding="utf-8") as server_errors:
            async with stdio_client(parameters, errlog=server_errors) as streams:
                async with ClientSession(*streams) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "ForgeMCP"

                    tools = await session.list_tools()
                    tool_by_name = {tool.name: tool for tool in tools.tools}
                    assert _EXPECTED_TOOLS.issubset(tool_by_name)
                    assert _DEBUGGER_TOOLS.issubset(tool_by_name)
                    launch_schema = tool_by_name["debugger__launch"].inputSchema
                    assert {"program", "args", "environment", "initial_breakpoints", "stop_on_entry"} <= launch_schema["properties"].keys()
                    assert launch_schema["additionalProperties"] is False
                    assert tool_by_name["debugger__events"].inputSchema["properties"]["limit"]["default"] == 100

                    status = await session.call_tool("cmake__status")
                    assert status.isError is False
                    status_payload = _json_tool_content(status)
                    assert {"available", "cmake", "ctest", "minimum_cmake_version"} <= status_payload.keys()

                    clangd_status = await session.call_tool("clangd__status")
                    assert clangd_status.isError is False
                    assert {"available", "state", "executable"} <= _json_tool_content(clangd_status).keys()

                    quality_status = _json_tool_content(await session.call_tool("quality__status", {}))
                    assert {"clang_format", "clang_tidy", "sanitizer_parsers", "platform_limitations"} <= quality_status.keys()
                    sanitizer = _json_tool_content(await session.call_tool(
                        "sanitizer__parse_report", {"output": "runtime error: signed integer overflow\n"}
                    ))
                    assert sanitizer["findings"][0]["kind"] == "undefined_behavior_sanitizer"
                    invalid_quality = await session.call_tool("clang_format__check", {"paths": ["missing.cpp"], "unexpected": True})
                    assert invalid_quality.isError is True

                    debugger_status = _json_tool_content(await session.call_tool("debugger__status"))
                    assert {"state", "session_generation", "stop_generation", "last_event_sequence"} <= debugger_status.keys()
                    adapters = _json_tool_content(await session.call_tool("debugger__list_adapters"))
                    assert len(adapters["adapters"]) == 1
                    invalid_debugger_arguments = await session.call_tool(
                        "debugger__launch", {"program": "missing.exe", "unexpected": True}
                    )
                    assert invalid_debugger_arguments.isError is True
                    assert "Traceback" not in getattr(invalid_debugger_arguments.content[0], "text")

                    for tool_name, arguments in (
                        ("clangd__completion", {"path": "missing.cpp", "position": {"line": 0, "column": 0}}),
                        ("clangd__rename", {"path": "missing.cpp", "position": {"line": 0, "column": 0}, "new_name": "renamed"}),
                        ("clangd__code_actions", {"path": "missing.cpp", "range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": 0}}}),
                    ):
                        phase_two_prestart = await session.call_tool(tool_name, arguments)
                        assert _json_tool_content(phase_two_prestart)["error"]["code"] == "clangd_not_started"

                    validation_error = await session.call_tool(
                        "clangd__rename",
                        {
                            "path": "missing.cpp",
                            "position": {"line": 0, "column": 0},
                            "new_name": "\x00",
                        },
                    )
                    assert _json_tool_content(validation_error)["error"]["code"] == "clangd_request_error"

                    # This error is raised before CMake is invoked, so it is stable
                    # whether the test host has CMake installed or not.
                    domain_error = await session.call_tool(
                        "cmake__configure", {"source_dir": ".", "binary_dir": "."}
                    )
                    assert domain_error.isError is False
                    assert _json_tool_content(domain_error) == {
                        "ok": False,
                        "error": {
                            "code": "cmake_request_error",
                            "message": "CMake source_dir and binary_dir must be different directories.",
                        },
                    }
        return errors_path.read_text(encoding="utf-8")

    server_errors = asyncio.run(exercise())

    assert '"event": "application_stopped"' in server_errors


@pytest.mark.skipif(
    _local_llvm_path("FORGEMCP_LLDB_DAP_LIVE_TEST", _DEFAULT_LLDB_DAP) is None
    or _local_llvm_path("FORGEMCP_LLVM_CLANG_LIVE_TEST", _DEFAULT_CLANG) is None,
    reason="real MCP debugger gate requires local standalone LLVM lldb-dap and clang",
)
def test_stdio_mcp_real_lldb_dap_vertical_slice(tmp_path: Path):
    """Run Phase 1 tools through real MCP stdio, never a test-only DAP probe."""
    adapter = _local_llvm_path("FORGEMCP_LLDB_DAP_LIVE_TEST", _DEFAULT_LLDB_DAP)
    clang = _local_llvm_path("FORGEMCP_LLVM_CLANG_LIVE_TEST", _DEFAULT_CLANG)
    assert adapter is not None and clang is not None
    source = tmp_path / "main.c"
    build = tmp_path / "build"
    build.mkdir()
    executable = build / "stdio_phase1.exe"
    source.write_text(
        "int main(void) {\n"
        "  volatile int value = 41;\n"
        "  value += 1;\n"
        "  return value == 42 ? 0 : 1;\n"
        "}\n",
        encoding="utf-8",
    )
    compile_result = __import__("subprocess").run(
        [str(clang), "-g", "-gdwarf-4", "-O0", str(source), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if compile_result.returncode != 0:
        pytest.skip("local LLVM cannot link the required PE/COFF + DWARF debuggee")

    async def exercise() -> str:
        errors_path = tmp_path / "server-stderr.log"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "forgemcp.server"],
            cwd=Path.cwd(),
            env={
                **os.environ,
                "FORGEMCP_WORKSPACE": str(tmp_path),
                "FORGEMCP_LLDB_DAP": str(adapter),
                "FORGEMCP_LOG_LEVEL": "INFO",
            },
        )
        with errors_path.open("w", encoding="utf-8") as server_errors:
            async with stdio_client(parameters, errlog=server_errors) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    tool_names = {tool.name for tool in (await session.list_tools()).tools}
                    assert {"debugger__list_adapters", "debugger__launch", "debugger__events", "debugger__stop"} <= tool_names
                    adapters = _json_tool_content(await session.call_tool("debugger__list_adapters", {}))
                    assert adapters["adapters"][0]["available"] is True
                    launch = _json_tool_content(await session.call_tool("debugger__launch", {
                        "program": "build/stdio_phase1.exe",
                        "cwd": "build",
                        "stop_on_entry": False,
                        "initial_breakpoints": {"main.c": [{"line": 2}]},
                    }))
                    assert launch["state"] in {"configuring", "running", "paused"}
                    for _ in range(100):
                        debugger_status = _json_tool_content(await session.call_tool("debugger__status", {}))
                        if debugger_status["state"] == "paused":
                            break
                        await asyncio.sleep(0.05)
                    assert debugger_status["state"] == "paused"
                    threads_payload = _json_tool_content(await session.call_tool("debugger__threads", {}))
                    assert "threads" in threads_payload, threads_payload
                    threads = threads_payload["threads"]
                    assert threads
                    frames = _json_tool_content(await session.call_tool("debugger__stack_trace", {"thread_id": threads[0]["thread_id"]}))["frames"]
                    assert frames and frames[0]["source"]["path"] == "main.c"
                    scopes = _json_tool_content(await session.call_tool("debugger__scopes", {"frame_id": frames[0]["frame_id"]}))["scopes"]
                    variable_scope = next(scope for scope in scopes if scope["variables_id"])
                    variables = _json_tool_content(await session.call_tool("debugger__variables", {"variables_id": variable_scope["variables_id"]}))["variables"]
                    assert any(variable["name"] == "value" for variable in variables)
                    await session.call_tool("debugger__continue", {"thread_id": threads[0]["thread_id"]})
                    events = _json_tool_content(await session.call_tool("debugger__events", {"limit": 256}))
                    assert events["events"]
                    stopped = _json_tool_content(await session.call_tool("debugger__stop", {}))
                    assert stopped["state"] == "terminated"
        return errors_path.read_text(encoding="utf-8")

    server_errors = asyncio.run(exercise())
    assert '"event": "application_stopped"' in server_errors


@pytest.mark.skipif(
    not os.environ.get("FORGEMCP_CLANGD"),
    reason="optional real MCP clangd gate requires an explicit FORGEMCP_CLANGD",
)
def test_stdio_mcp_real_clangd_rename_stop_and_transport_shutdown(tmp_path: Path):
    """Exercise the phase-1/2 surface through a real MCP stdio transport."""

    async def exercise() -> str:
        build = tmp_path / "build"
        build.mkdir()
        header = tmp_path / "shared.hpp"
        main = tmp_path / "main.cpp"
        header.write_text("inline int value = 1;\n", encoding="utf-8")
        main.write_text('#include "shared.hpp"\nint main() { return value; }\n', encoding="utf-8")
        (build / "compile_commands.json").write_text(
            json.dumps(
                [
                    {
                        "directory": str(tmp_path),
                        "file": str(main),
                        "command": "clang++ -std=c++17 -c main.cpp",
                    }
                ]
            ),
            encoding="utf-8",
        )
        errors_path = tmp_path / "server-stderr.log"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "forgemcp.server"],
            cwd=Path.cwd(),
            env={
                **os.environ,
                "FORGEMCP_WORKSPACE": str(tmp_path),
                "FORGEMCP_CLANGD": os.environ["FORGEMCP_CLANGD"],
                "FORGEMCP_LOG_LEVEL": "INFO",
            },
        )
        with errors_path.open("w", encoding="utf-8") as server_errors:
            async with stdio_client(parameters, errlog=server_errors) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    assert "clangd__rename" in {tool.name for tool in (await session.list_tools()).tools}

                    started = _json_tool_content(
                        await session.call_tool("clangd__start", {"compile_commands_dir": "build"})
                    )
                    assert started["status"]["state"] == "running"

                    diagnostics = _json_tool_content(
                        await session.call_tool(
                            "clangd__diagnostics", {"path": "main.cpp", "timeout_seconds": 10.0}
                        )
                    )
                    assert diagnostics["snapshot"]["exists"] is True
                    definition = _json_tool_content(
                        await session.call_tool(
                            "clangd__definition", {"path": "main.cpp", "position": {"line": 1, "column": 20}}
                        )
                    )
                    assert "locations" in definition

                    prepared = _json_tool_content(
                        await session.call_tool(
                            "clangd__prepare_rename",
                            {"path": "main.cpp", "position": {"line": 1, "column": 20}},
                        )
                    )
                    assert prepared["range"] is not None
                    conflict = _json_tool_content(
                        await session.call_tool(
                            "clangd__rename",
                            {
                                "path": "main.cpp",
                                "position": {"line": 1, "column": 20},
                                "new_name": "renamed_value",
                                "expected_sha256": "0" * 64,
                            },
                        )
                    )
                    assert conflict["error"]["code"] == "clangd_edit_conflict"
                    renamed = _json_tool_content(
                        await session.call_tool(
                            "clangd__rename",
                            {
                                "path": "main.cpp",
                                "position": {"line": 1, "column": 20},
                                "new_name": "renamed_value",
                                "expected_sha256": prepared["snapshot"]["sha256"],
                            },
                        )
                    )
                    assert renamed["edit"]["applied"] is True
                    assert "renamed_value" in main.read_text(encoding="utf-8")
                    assert "renamed_value" in header.read_text(encoding="utf-8")

                    refreshed = _json_tool_content(
                        await session.call_tool(
                            "clangd__diagnostics", {"path": "main.cpp", "timeout_seconds": 10.0}
                        )
                    )
                    assert refreshed["snapshot"]["sha256"] != prepared["snapshot"]["sha256"]
                    stopped = _json_tool_content(await session.call_tool("clangd__stop", {}))
                    assert stopped == {"stopped": True}
                    assert _json_tool_content(await session.call_tool("clangd__status", {}))["state"] == "stopped"

                    # Leave a second managed child for the stdio lifespan's
                    # shutdown path, rather than only exercising explicit stop.
                    restarted = _json_tool_content(
                        await session.call_tool("clangd__start", {"compile_commands_dir": "build"})
                    )
                    assert restarted["status"]["state"] == "running"
        return errors_path.read_text(encoding="utf-8")

    server_errors = asyncio.run(exercise())
    assert '"event": "application_stopped"' in server_errors

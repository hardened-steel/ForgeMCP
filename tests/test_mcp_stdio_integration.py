"""End-to-end MCP stdio coverage using the SDK client transport."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from time import monotonic
from pathlib import Path

import pytest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


_EXPECTED_TOOLS = {
    "server_status",
    "project__status",
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
_DEFAULT_CLANG_FORMAT = Path(r"C:\Program Files\LLVM\bin\clang-format.exe")
_DEFAULT_CLANG_TIDY = Path(r"C:\Program Files\LLVM\bin\clang-tidy.exe")


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
                    assert tool_by_name["project__status"].inputSchema["properties"] == {}
                    assert tool_by_name["project__status"].inputSchema["additionalProperties"] is False

                    project_status_result = await session.call_tool("project__status", {})
                    assert project_status_result.isError is False
                    project_status = _json_tool_content(project_status_result)
                    assert project_status["workspace_root"] == "configured"
                    assert str(tmp_path) not in json.dumps(project_status)
                    assert project_status["partial"] is False
                    assert project_status["health"] in {"healthy", "degraded", "failed"}
                    assert project_status["activity"] in {"idle", "busy", "paused"}
                    assert len(project_status["components"]) == 8
                    assert len(json.dumps(project_status)) < 100_000
                    invalid_project_status = await session.call_tool(
                        "project__status", {"refresh": True}
                    )
                    assert invalid_project_status.isError is True

                    status = await session.call_tool("cmake__status")
                    assert status.isError is False
                    status_payload = _json_tool_content(status)
                    assert {"available", "cmake", "ctest", "minimum_cmake_version"} <= status_payload.keys()

                    core_status = _json_tool_content(await session.call_tool("server_status", {}))
                    assert core_status["workspace_root"] == "configured"
                    assert str(tmp_path) not in json.dumps(core_status)

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
    assert str(tmp_path) not in server_errors


@pytest.mark.skipif(
    os.name != "nt" or not os.environ.get("FORGEMCP_REAL_WINDOWS_TOOLCHAIN_GATE"),
    reason="opt-in real Windows MCP toolchain gate requires FORGEMCP_REAL_WINDOWS_TOOLCHAIN_GATE",
)
def test_real_msvc_mcp_stdio_gate_uses_cli_configuration_without_forgemcp_environment(
    tmp_path: Path,
):
    """Run the CMake vertical slice through SDK stdio with CLI-only configuration."""
    (tmp_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.23)\n"
        "project(forgemcp_cli_mcp_gate LANGUAGES CXX)\n"
        "add_executable(app main.cpp)\n"
        "enable_testing()\n"
        "add_test(NAME app_runs COMMAND app)\n",
        encoding="utf-8",
    )
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")

    async def exercise() -> str:
        errors_path = tmp_path / "server-stderr.log"
        # The child gets no ForgeMCP setting and no inherited Developer/PATH
        # state.  VS discovery and VsDevCmd must establish the CMake toolchain.
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("FORGEMCP_")
            and key.upper() not in {"VSCMD_VER", "VCINSTALLDIR", "VSINSTALLDIR"}
        }
        environment["PATH"] = ""
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m", "forgemcp.server", "--workspace", str(tmp_path),
                "--toolchain", "msvc", "--configure-timeout-sec", "300",
                "--build-timeout-sec", "300", "--test-timeout-sec", "300",
            ],
            cwd=Path.cwd(),
            env=environment,
        )
        with errors_path.open("w", encoding="utf-8") as server_errors:
            async with stdio_client(parameters, errlog=server_errors) as streams:
                async with ClientSession(*streams) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "ForgeMCP"
                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    assert {
                        "project__status", "cmake__status", "cmake__configure",
                        "cmake__build", "cmake__ctest_list_tests", "cmake__ctest_run",
                    } <= tools
                    status = _json_tool_content(await session.call_tool("project__status", {}))
                    assert status["workspace_root"] == "configured"
                    cmake_status = _json_tool_content(await session.call_tool("cmake__status", {}))
                    assert cmake_status["available"] is True
                    configured = _json_tool_content(await session.call_tool(
                        "cmake__configure", {"binary_dir": "build"}
                    ))
                    assert configured["process"]["exit_code"] == 0
                    built = _json_tool_content(await session.call_tool(
                        "cmake__build", {"binary_dir": "build", "targets": ["app"]}
                    ))
                    assert built["process"]["exit_code"] == 0
                    tests = _json_tool_content(await session.call_tool(
                        "cmake__ctest_list_tests", {"binary_dir": "build"}
                    ))
                    assert [item["name"] for item in tests["tests"]] == ["app_runs"]
                    executed = _json_tool_content(await session.call_tool(
                        "cmake__ctest_run", {"binary_dir": "build"}
                    ))
                    assert executed["process"]["exit_code"] == 0
        return errors_path.read_text(encoding="utf-8")

    errors = asyncio.run(exercise())
    assert '"event": "application_stopped"' in errors
    assert str(tmp_path) not in errors


@pytest.mark.parametrize(
    ("mode", "expected_health", "expected_activity", "expected_component"),
    [
        ("failure", "degraded", "idle", "fixture_failure"),
        ("timeout", "degraded", "idle", "fixture_timeout"),
        ("active", "healthy", "paused", None),
        ("configured_unavailable", "degraded", "idle", None),
    ],
)
def test_stdio_mcp_project_status_failure_timeout_activity_and_unavailable(
    tmp_path: Path,
    mode: str,
    expected_health: str,
    expected_activity: str,
    expected_component: str | None,
):
    """Exercise Project Phase 1 boundary cases through the real SDK client."""

    async def exercise() -> None:
        fixture = Path(__file__).parent / "fixtures" / "project_status_stdio_server.py"
        environment = {
            **os.environ,
            "FORGEMCP_WORKSPACE": str(tmp_path),
            "FORGEMCP_PROJECT_STATUS_FIXTURE": mode,
        }
        if mode == "configured_unavailable":
            environment["FORGEMCP_CLANGD"] = str(tmp_path / "missing-clangd.exe")
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(fixture)],
            env=environment,
        )
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert tools["project__status"].inputSchema["additionalProperties"] is False
                started = monotonic()
                result = await session.call_tool("project__status", {})
                assert monotonic() - started < 2.0
                payload = _json_tool_content(result)
                assert payload["health"] == expected_health
                assert payload["activity"] == expected_activity
                assert "stdio-secret" not in json.dumps(payload)
                if mode == "failure":
                    assert expected_component in payload["failed_components"]
                if mode == "timeout":
                    assert expected_component in payload["timed_out_components"]
                invalid = await session.call_tool("project__status", {"refresh": True})
                assert invalid.isError is True

    asyncio.run(exercise())


def test_stdio_mcp_concurrent_project_status_calls_are_single_flight(tmp_path: Path):
    async def exercise() -> None:
        fixture = Path(__file__).parent / "fixtures" / "project_status_stdio_server.py"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(fixture)],
            env={
                **os.environ,
                "FORGEMCP_WORKSPACE": str(tmp_path),
                "FORGEMCP_PROJECT_STATUS_FIXTURE": "concurrent",
            },
        )
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                results = await asyncio.gather(
                    *(session.call_tool("project__status", {}) for _ in range(24))
                )
                payloads = [_json_tool_content(result) for result in results]
                for payload in payloads:
                    component = next(
                        item for item in payload["components"] if item["id"] == "fixture_concurrent"
                    )
                    facts = {item["name"]: item["value"] for item in component["facts"]}
                    assert facts["snapshot_calls"] == 1

    asyncio.run(exercise())


def test_stdio_mcp_shutdown_during_project_snapshot_is_bounded(tmp_path: Path):
    async def exercise() -> None:
        fixture = Path(__file__).parent / "fixtures" / "project_status_stdio_server.py"
        errors_path = tmp_path / "project-shutdown-stderr.log"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(fixture)],
            env={
                **os.environ,
                "FORGEMCP_WORKSPACE": str(tmp_path),
                "FORGEMCP_PROJECT_STATUS_FIXTURE": "shutdown",
            },
        )
        started = monotonic()
        with errors_path.open("w", encoding="utf-8") as server_errors:
            async with stdio_client(parameters, errlog=server_errors) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    pending = asyncio.create_task(session.call_tool("project__status", {}))
                    await asyncio.sleep(0.02)
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
        assert monotonic() - started < 3.0
        assert '"event": "application_stopped"' in errors_path.read_text(encoding="utf-8")

    asyncio.run(exercise())


@pytest.mark.skipif(
    _local_llvm_path("FORGEMCP_CLANG_FORMAT_LIVE_TEST", _DEFAULT_CLANG_FORMAT) is None
    or _local_llvm_path("FORGEMCP_CLANG_TIDY_LIVE_TEST", _DEFAULT_CLANG_TIDY) is None,
    reason="real MCP quality gate requires local clang-format and clang-tidy",
)
def test_stdio_mcp_real_quality_security_vertical_slice(tmp_path: Path):
    """Verify schemas, CAS, real tools, safe failures, and stdio shutdown."""
    formatter = _local_llvm_path("FORGEMCP_CLANG_FORMAT_LIVE_TEST", _DEFAULT_CLANG_FORMAT)
    tidy = _local_llvm_path("FORGEMCP_CLANG_TIDY_LIVE_TEST", _DEFAULT_CLANG_TIDY)
    assert formatter is not None and tidy is not None
    clang_driver = tidy.parent / ("clang++.exe" if os.name == "nt" else "clang++")
    if not clang_driver.is_file():
        pytest.skip("real MCP quality gate requires the matching clang++ driver")
    source = tmp_path / "main.cpp"
    source.write_bytes(b"int main(){return 0;}\n")
    tidy_source = tmp_path / "tidy.cpp"
    tidy_source.write_bytes(b"int main() { int unused; return 0; }\n")
    build = tmp_path / "build"
    build.mkdir()
    (build / "compile_commands.json").write_text(json.dumps([{
        "directory": str(tmp_path),
        "file": str(tidy_source),
        "arguments": [str(clang_driver), "-Wall", "-Wextra", "-c", str(tidy_source)],
    }]), encoding="utf-8")
    (tmp_path / ".clang-tidy").write_text("Checks: ''\n", encoding="utf-8")

    async def exercise() -> str:
        errors_path = tmp_path / "quality-server-stderr.log"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "forgemcp.server"],
            cwd=Path.cwd(),
            env={
                **os.environ,
                "FORGEMCP_WORKSPACE": str(tmp_path),
                "FORGEMCP_CLANG_FORMAT": str(formatter),
                "FORGEMCP_CLANG_TIDY": str(tidy),
                "FORGEMCP_LOG_LEVEL": "INFO",
            },
        )
        with errors_path.open("w", encoding="utf-8") as server_errors:
            async with stdio_client(parameters, errlog=server_errors) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    quality_tool_names = {
                        "quality__status",
                        "clang_format__check",
                        "clang_format__apply",
                        "clang_tidy__list_checks",
                        "clang_tidy__run",
                        "sanitizer__parse_report",
                    }
                    assert quality_tool_names <= tools.keys()
                    for tool_name in quality_tool_names:
                        assert tools[tool_name].inputSchema["additionalProperties"] is False
                    assert tools["quality__status"].inputSchema.get("required", []) == []
                    list_checks_schema = tools["clang_tidy__list_checks"].inputSchema
                    assert list_checks_schema.get("required", []) == []
                    assert list_checks_schema["properties"]["checks"]["anyOf"][0][
                        "maxLength"
                    ] == 1024
                    sanitizer_schema = tools["sanitizer__parse_report"].inputSchema
                    assert sanitizer_schema["required"] == ["output"]
                    assert sanitizer_schema["properties"]["output"]["maxLength"] == 65_536
                    check_schema = tools["clang_format__check"].inputSchema
                    apply_schema = tools["clang_format__apply"].inputSchema
                    tidy_schema = tools["clang_tidy__run"].inputSchema
                    assert check_schema["required"] == ["paths"]
                    assert check_schema["properties"]["paths"]["minItems"] == 1
                    assert check_schema["properties"]["paths"]["maxItems"] == 64
                    assert check_schema["additionalProperties"] is False
                    assert apply_schema["required"] == ["files"]
                    item_schema = apply_schema["properties"]["files"]["items"]
                    if "$ref" in item_schema:
                        item_schema = apply_schema["$defs"][item_schema["$ref"].rsplit("/", 1)[-1]]
                    assert set(item_schema["required"]) == {"path", "expected_sha256"}
                    assert item_schema["additionalProperties"] is False
                    assert set(tidy_schema["required"]) == {"paths", "compile_commands_dir"}
                    assert {"checks", "timeout_seconds"} <= tidy_schema["properties"].keys()
                    assert tidy_schema["additionalProperties"] is False

                    checked = _json_tool_content(
                        await session.call_tool("clang_format__check", {"paths": ["main.cpp"]})
                    )
                    assert checked["clean"] is False
                    snapshot = checked["files"][0]["snapshot_sha256"]
                    assert source.read_bytes() == b"int main(){return 0;}\n"
                    assert "source" not in checked["files"][0]
                    assert "replacements" not in checked["files"][0]

                    stale = _json_tool_content(await session.call_tool(
                        "clang_format__apply",
                        {"files": [{"path": "main.cpp", "expected_sha256": "0" * 64}]},
                    ))
                    assert stale["applied"] is False
                    assert stale["conflict"] is True
                    assert source.read_bytes() == b"int main(){return 0;}\n"

                    applied = _json_tool_content(await session.call_tool(
                        "clang_format__apply",
                        {"files": [{"path": "main.cpp", "expected_sha256": snapshot}]},
                    ))
                    assert applied["applied"] is True
                    assert _json_tool_content(await session.call_tool(
                        "clang_format__check", {"paths": ["main.cpp"]}
                    ))["clean"] is True

                    tidy_run = _json_tool_content(await session.call_tool(
                        "clang_tidy__run",
                        {
                            "paths": ["tidy.cpp"],
                            "compile_commands_dir": "build",
                            "checks": "-*,clang-analyzer-core.*,clang-diagnostic-unused-variable",
                            "timeout_seconds": 30,
                        },
                    ))
                    assert tidy_run["execution_state"] == "completed"
                    assert any(
                        item.get("code") == "clang-diagnostic-unused-variable"
                        for item in tidy_run["diagnostics"]
                    )
                    assert not {"stdout", "stderr", "compile_command", "environment"} & tidy_run.keys()

                    tidy_source.write_bytes(b"int main( {\n")
                    failed_tidy = _json_tool_content(await session.call_tool(
                        "clang_tidy__run",
                        {
                            "paths": ["tidy.cpp"],
                            "compile_commands_dir": "build",
                            "checks": "clang-analyzer-core.*",
                        },
                    ))
                    assert failed_tidy["execution_state"] == "tool_failure"
                    assert not {"stdout", "stderr", "compile_command", "environment"} & failed_tidy.keys()

                    malformed = _json_tool_content(await session.call_tool(
                        "sanitizer__parse_report", {"output": "unrecognized TOP_SECRET"}
                    ))
                    assert malformed["findings"][0]["kind"] == "unknown"
                    assert "TOP_SECRET" not in json.dumps(malformed)

                    external_source = str(tmp_path.parent / "external-secret.cpp")
                    external_result = await session.call_tool(
                        "clang_format__check", {"paths": [external_source]}
                    )
                    external_payload = _json_tool_content(external_result)
                    assert external_payload["error"]["code"] == "quality_request_error"
                    assert external_source not in getattr(external_result.content[0], "text")

                    for tool_name, arguments in (
                        ("clang_format__check", {"paths": []}),
                        ("clang_format__check", {"paths": ["main.cpp"], "unknown": True}),
                        ("clang_format__check", {"paths": "main.cpp"}),
                        ("clang_format__check", {"paths": ["main.cpp"] * 65}),
                        ("clang_format__apply", {"files": [{"path": "main.cpp"}]}),
                        ("sanitizer__parse_report", {"output": 1}),
                        ("sanitizer__parse_report", {"output": "x" * 65_537}),
                    ):
                        invalid = await session.call_tool(tool_name, arguments)
                        assert invalid.isError is True
                        assert "Traceback" not in getattr(invalid.content[0], "text")
        return errors_path.read_text(encoding="utf-8")

    server_errors = asyncio.run(exercise())
    assert '"event": "application_stopped"' in server_errors
    assert "int main" not in server_errors
    assert "TOP_SECRET" not in server_errors


def test_stdio_mcp_quality_unavailable_tool_is_structured(tmp_path: Path):
    """An explicit missing executable returns a stable domain response, not a traceback."""
    (tmp_path / "main.cpp").write_bytes(b"int main() {}\n")

    async def exercise() -> None:
        missing = tmp_path / "missing-clang-format.exe"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "forgemcp.server"],
            cwd=Path.cwd(),
            env={
                **os.environ,
                "FORGEMCP_WORKSPACE": str(tmp_path),
                "FORGEMCP_CLANG_FORMAT": str(missing),
            },
        )
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                result = await session.call_tool(
                    "clang_format__check", {"paths": ["main.cpp"]}
                )
                payload = _json_tool_content(result)
                assert payload["error"]["code"] == "quality_tool_unavailable"
                assert "Traceback" not in getattr(result.content[0], "text")

    asyncio.run(exercise())


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

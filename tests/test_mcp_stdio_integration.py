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
}


def _json_tool_content(result: object) -> dict[str, object]:
    """Decode FastMCP's JSON text response without relying on private SDK state."""
    content = getattr(result, "content")
    assert len(content) == 1
    text = getattr(content[0], "text")
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload


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
                    assert _EXPECTED_TOOLS.issubset({tool.name for tool in tools.tools})

                    status = await session.call_tool("cmake__status")
                    assert status.isError is False
                    status_payload = _json_tool_content(status)
                    assert {"available", "cmake", "ctest", "minimum_cmake_version"} <= status_payload.keys()

                    clangd_status = await session.call_tool("clangd__status")
                    assert clangd_status.isError is False
                    assert {"available", "state", "executable"} <= _json_tool_content(clangd_status).keys()

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

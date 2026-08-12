"""End-to-end MCP stdio coverage using the SDK client transport."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

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

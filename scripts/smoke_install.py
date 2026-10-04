"""Exercise the installed package through the in-process MCP Client; run with -I."""

from __future__ import annotations

import asyncio
from importlib.metadata import version
from pathlib import Path
import tempfile

import forgemcp
from mcp import Client
from mcp.client import advertise
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID

from forgemcp.server import create_server


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    package_path = Path(forgemcp.__file__).resolve()
    if package_path.is_relative_to(root / "src"):
        raise RuntimeError("Smoke verification must use the installed wheel, not src/.")
    if forgemcp.__version__ != version("forgemcp"):
        raise RuntimeError("Runtime and distribution versions disagree.")
    with tempfile.TemporaryDirectory(prefix="forgemcp-smoke-") as directory:
        workspace = Path(directory) / "project"
        workspace.mkdir()
        (workspace / "hello.txt").write_text(
            "ForgeMCP release smoke check\n",
            encoding="utf-8",
            newline="\n",
        )
        async with Client(
            create_server(workspace, storage_root=Path(directory) / "storage"),
            extensions=[advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})],
        ) as client:
            tools = (await client.list_tools()).tools
            if not tools:
                raise RuntimeError("Installed server exposes no tools.")
            resources = set()
            for tool in tools:
                if not tool.icons or not tool.output_schema:
                    raise RuntimeError(f"Tool {tool.name} is missing icons or structured output.")
                resources.add(tool.meta["ui"]["resourceUri"])
            for uri in resources:
                response = await client.read_resource(uri)
                if not response.contents or response.contents[0].mime_type != APP_MIME_TYPE:
                    raise RuntimeError(f"App resource {uri} is missing or has the wrong MIME type.")
            result = await client.call_tool(
                "workspace_read_file",
                {"path": "project/hello.txt"},
            )
            if (
                result.is_error
                or not result.content
                or result.structured_content["text"] != "ForgeMCP release smoke check\n"
            ):
                raise RuntimeError("Installed server failed the workspace read smoke check.")
    print(
        f"Verified installed ForgeMCP {forgemcp.__version__}: "
        f"{len(tools)} tools, {len(resources)} Apps resources."
    )


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=120))

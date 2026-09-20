import asyncio
import base64
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest
from mcp import Client
from mcp.client import advertise
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
from mcp.types import ResourceTemplateReference
from mcp.shared.exceptions import MCPError

from forgemcp.server import argument_parser, create_server
from forgemcp.workspace.service import WorkspaceService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate_host_discovery(monkeypatch):
    from forgemcp.toolchain import discovery
    from forgemcp.toolchain.providers import visual_studio

    monkeypatch.setattr(discovery, "load_tools", lambda: ())

    async def discover(*args):
        return ()

    monkeypatch.setattr(visual_studio, "discover", discover)


@pytest.mark.anyio
async def test_workspace_tools_have_apps_schemas_icons_and_progress(
    cpp_acceptance_project,
):
    server = create_server(cpp_acceptance_project, progress_interval=0)
    progress = []

    async def collect(value, total, message):
        progress.append((value, total))

    async with Client(
        server,
        extensions=[advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})],
    ) as client:
        tools = [
            tool
            for tool in (await client.list_tools()).tools
            if tool.name.startswith("workspace_")
        ]
        assert len(tools) == 10
        assert "workspace_overview" not in {tool.name for tool in tools}
        assert (await client.list_prompts()).prompts == []
        for tool in tools:
            assert tool.output_schema and tool.icons and tool.meta["ui"]["resourceUri"]
            assert "ctx" not in tool.input_schema.get("properties", {})
            assert "expected_revision" not in tool.input_schema.get("properties", {})
            app = await client.read_resource(tool.meta["ui"]["resourceUri"])
            assert app.contents[0].mime_type == APP_MIME_TYPE
        result = await client.call_tool("workspace_list", {}, progress_callback=collect)
        assert not result.is_error and result.structured_content["root"] == "project"
        assert result.content
        assert len(progress) > 2
        assert [value for value, _ in progress] == list(range(len(progress)))
        assert all(total is None for _, total in progress)
        assert WorkspaceService.__doc__ in client.instructions


@pytest.mark.anyio
async def test_full_file_workflow_through_client(cpp_acceptance_project):
    async with Client(create_server(cpp_acceptance_project)) as client:

        async def call(name, **arguments):
            result = await client.call_tool("workspace_" + name, arguments)
            assert not result.is_error, result.content
            return result.structured_content

        assert (await call("mkdir", path="new"))["action"] == "created"
        assert (await call("write_file", path="new/a.txt", text="one\ntwo two\n"))[
            "lines_added"
        ] == 2
        assert (
            await call(
                "edit_file",
                path="new/a.txt",
                old_text="two",
                new_text="three",
                replace_all=True,
            )
        )["replacements"] == 2
        assert (await call("read_file", path="new/a.txt", start_line=2))[
            "text"
        ] == "three three\n"
        assert (await call("file_info", path="new/a.txt"))["size_bytes"] == len(
            b"one\nthree three\n"
        )
        assert (await call("find_files", pattern="new/*.txt"))["paths"] == ["new/a.txt"]
        assert (await call("search", query="three", path="new"))["matches"][0][
            "line"
        ] == 2
        assert (await call("move", source="new/a.txt", destination="new/b.txt"))[
            "action"
        ] == "moved"
        assert (await call("delete", path="new/b.txt"))["action"] == "deleted"
        await call("delete", path="new")
        failed = await client.call_tool("workspace_read_file", {"path": "../outside"})
        assert failed.is_error


@pytest.mark.anyio
async def test_mirrors_markdown_templates_regex_and_completions(cpp_acceptance_project):
    workspace = WorkspaceService(cpp_acceptance_project)
    workspace.mkdir("resource data")
    text = "α\r\nfoo(x)/../#&\r\n```\n"
    workspace.write_file("resource data/a#%.cpp", text)
    (cpp_acceptance_project / "resource data/data.bin").write_bytes(b"\x00\xff\x01")
    async with Client(create_server(cpp_acceptance_project)) as client:
        templates = (await client.list_resource_templates()).resource_templates
        workspace_templates = [
            item
            for item in templates
            if str(item.uri_template).startswith("forgemcp://workspace/")
        ]
        assert len(workspace_templates) == 6
        uri = workspace.file_uri("project", "resource data/a#%.cpp")
        result = await client.read_resource(uri)
        assert result.contents[0].text == text
        assert result.contents[0].mime_type == "text/plain"
        raw = await client.read_resource(
            "forgemcp://workspace/project/raw/resource%20data/data.bin"
        )
        assert base64.b64decode(raw.contents[0].blob) == b"\x00\xff\x01"
        assert raw.contents[0].mime_type == "application/octet-stream"
        cases = [
            ("list", {"path": "resource data", "depth": "all"}, "a#%.cpp"),
            (
                "find-files",
                {"pattern": "resource data/*.cpp"},
                "a%23%25.cpp",
            ),
            ("file-info", {"path": "resource data/a#%.cpp"}, "size\\_bytes"),
            (
                "search",
                {
                    "query": r"foo\(x\)/\.\./#&",
                    "regex": "true",
                    "path": "resource data",
                },
                "foo(x)/../#&",
            ),
        ]
        for name, arguments, expected in cases:
            resource = await client.read_resource(
                f"forgemcp://workspace/project/{name}?{urlencode(arguments, quote_via=quote)}"
            )
            assert resource.contents[0].mime_type == "text/markdown"
            assert expected in resource.contents[0].text
        for template, argument, prefix, expected in [
            (WorkspaceService.FILE_URI, "root", "st", "storage"),
            (WorkspaceService.FILE_URI, "path", "src/ma", "src/math.cpp"),
            (WorkspaceService.LIST_URI, "depth", "a", "all"),
            (WorkspaceService.SEARCH_URI, "regex", "t", "true"),
            (WorkspaceService.SEARCH_URI, "extensions", "cp", "cpp"),
        ]:
            completion = await client.complete(
                ResourceTemplateReference(uri=template),
                {"name": argument, "value": prefix},
            )
            assert expected in completion.completion.values
        for uri in (
            "forgemcp://workspace/project/file/%2E%2E/secret",
            "forgemcp://workspace/project/search?query=%5B&regex=true",
            "forgemcp://workspace/project/file-info",
            "forgemcp://workspace/project/search",
        ):
            with pytest.raises(MCPError):
                await client.read_resource(uri)


@pytest.mark.anyio
async def test_default_project_custom_storage_and_resource_updates(
    cpp_acceptance_project,
    monkeypatch,
):
    monkeypatch.chdir(cpp_acceptance_project)
    storage = cpp_acceptance_project / "service-data"
    options = argument_parser().parse_args(["--workspace-storage", str(storage)])
    assert options.workspace_storage == storage
    async with Client(create_server(storage_root=storage)) as client:
        await client.call_tool("workspace_mkdir", {"path": "index", "root": "storage"})
        for text in ("first", "second"):
            await client.call_tool(
                "workspace_write_file",
                {"path": "index/state", "text": text, "root": "storage"},
            )
            result = await client.read_resource(
                "forgemcp://workspace/storage/file/index/state"
            )
            assert result.contents[0].text == text
        assert (
            await client.call_tool("workspace_read_file", {"path": "README.md"})
        ).structured_content["path"] == "README.md"


@pytest.mark.anyio
async def test_hidden_directories_and_find_schema(cpp_acceptance_project):
    (cpp_acceptance_project / ".hidden").mkdir()
    (cpp_acceptance_project / ".visible-file").write_text("visible")
    async with Client(create_server(cpp_acceptance_project)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert (
            "extensions" not in tools["workspace_find_files"].input_schema["properties"]
        )
        assert (
            tools["workspace_list"].input_schema["properties"]["include_hidden"][
                "default"
            ]
            is False
        )
        for include_hidden in (False, True):
            result = await client.call_tool(
                "workspace_list",
                {"include_hidden": include_hidden},
            )
            paths = {entry["path"] for entry in result.structured_content["entries"]}
            assert (".hidden" in paths) is include_hidden
            assert ".visible-file" in paths
            resource = await client.read_resource(
                "forgemcp://workspace/project/list?include_hidden="
                + str(include_hidden).lower()
            )
            assert (".hidden/" in resource.contents[0].text) is include_hidden
            assert ".visible-file" in resource.contents[0].text
        completion = await client.complete(
            ResourceTemplateReference(uri=WorkspaceService.LIST_URI),
            {"name": "include_hidden", "value": "t"},
        )
        assert completion.completion.values == ["true"]


@pytest.mark.anyio
async def test_invalid_resource_roots_fail_promptly_with_useful_errors(
    cpp_acceptance_project,
):
    async with Client(create_server(cpp_acceptance_project)) as client:
        for root in ("wrong", quote("цуацуа")):
            for suffix in (
                "file",
                "file/README.md",
                "raw",
                "list",
                "find-files",
                "file-info",
                "search",
            ):
                with pytest.raises(
                    MCPError,
                    match="Root must be 'project' or 'storage'",
                ):
                    await asyncio.wait_for(
                        client.read_resource(f"forgemcp://workspace/{root}/{suffix}"),
                        timeout=2,
                    )
        with pytest.raises(MCPError, match="nonempty relative path"):
            await client.read_resource("forgemcp://workspace/project/file")
        failed = await client.call_tool("workspace_delete", {"path": "src"})
        assert failed.is_error
        assert "src:" in failed.content[0].text


@pytest.mark.anyio
async def test_scan_and_read_progress_and_exact_regex_spans(cpp_acceptance_project):
    (cpp_acceptance_project / "unicode.txt").write_text(
        "😀 Straße STRASSE\n",
        encoding="utf-8",
    )
    (cpp_acceptance_project / "large.txt").write_text("x" * 150000, encoding="utf-8")
    progress = []

    async def collect(value, total, message):
        progress.append((value, total, message))

    async with Client(create_server(cpp_acceptance_project, progress_interval=0)) as client:
        for tool, arguments in (
            ("workspace_list", {"depth": None}),
            ("workspace_find_files", {}),
            ("workspace_search", {"query": "STRASSE", "case_sensitive": False}),
            ("workspace_read_file", {"path": "large.txt"}),
        ):
            progress.clear()
            result = await client.call_tool(tool, arguments, progress_callback=collect)
            assert not result.is_error
            assert len(progress) > 2
            values = [value for value, _, _ in progress]
            assert values == sorted(set(values))
            assert values[0] == 0
            assert all(total is None for _, total, _ in progress)
            if tool == "workspace_read_file":
                assert values[-1] == 150000
            if tool == "workspace_search":
                match = next(
                    item
                    for item in result.structured_content["matches"]
                    if item["path"] == "unicode.txt"
                )
                assert match["spans"] == [[2, 8], [9, 16]]
                assert [match["text"][a:b] for a, b in match["spans"]] == [
                    "Straße",
                    "STRASSE",
                ]


def test_progress_interval_cli(cpp_acceptance_project):
    assert argument_parser().parse_args([]).progress_interval == 1.0
    options = argument_parser().parse_args(["--progress-interval", "2.5"])
    assert options.progress_interval == 2.5
    assert argument_parser().parse_args(["--progress-interval", "0"]).progress_interval == 0


@pytest.mark.parametrize("interval", [-1, float("nan"), float("inf")])
def test_invalid_progress_interval_is_rejected_before_service_creation(
    cpp_acceptance_project,
    monkeypatch,
    interval,
):
    def unexpected_service(*args, **kwargs):
        pytest.fail("Configuration must be validated before constructing services")

    monkeypatch.setattr("forgemcp.server.WorkspaceService", unexpected_service)
    with pytest.raises(ValueError, match="Progress interval"):
        create_server(cpp_acceptance_project, progress_interval=interval)

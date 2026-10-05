"""In-process MCP registration, workspace lifecycle, resource, and completion tests."""

import asyncio
import base64
import inspect
import json
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest
from mcp import Client
from mcp.client import advertise
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
from mcp.types import ElicitResult, ResourceTemplateReference
from mcp.shared.exceptions import MCPError

from forgemcp.server import argument_parser, create_server
from forgemcp.cmake.service import CMakeService
from forgemcp.clangd.service import ClangdService
from forgemcp.process.service import ProcessService
from forgemcp.toolchain.service import ToolchainService
from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.service import WorkspaceService
from forgemcp.workspace.diff import FileDiff


@pytest.mark.anyio
async def test_workspace_plain_text_workflow_preserves_structured_data(cpp_acceptance_project):
    """Exercise every workspace fallback through the SDK, including unchanged Markdown resources."""
    async with Client(create_server(cpp_acceptance_project)) as client:
        responses = {}
        operations = [
            ("workspace_mkdir", {"path": "project/text-output"}),
            ("workspace_mkdir", {"path": "project/text-output"}),
            (
                "workspace_write_file",
                {"path": "project/text-output/sample.cpp", "text": "// α\nint needle = 1;\n"},
            ),
            ("workspace_list", {"path": "project/text-output", "depth": None}),
            (
                "workspace_find_files",
                {"pattern": "*.cpp", "path": "project/text-output"},
            ),
            ("workspace_file_info", {"path": "project/text-output/sample.cpp"}),
            (
                "workspace_read_file",
                {"path": "project/text-output/sample.cpp", "start_line": 2},
            ),
            ("workspace_text_search", {"query": "needle", "path": "project/text-output"}),
            (
                "workspace_edit_file",
                {"path": "project/text-output/sample.cpp", "old_text": "1", "new_text": "2"},
            ),
            (
                "workspace_move",
                {
                    "source": "project/text-output/sample.cpp",
                    "destination": "project/text-output/moved.cpp",
                },
            ),
            ("workspace_delete", {"path": "project/text-output/moved.cpp"}),
        ]
        for name, arguments in operations:
            response = await client.call_tool(name, arguments)
            assert not response.is_error, response.content
            assert response.structured_content is not None
            text = response.content[0].text
            assert text and not text.lstrip().startswith("{")
            assert "```" not in text and "forgemcp://workspace/results/" not in text
            responses[name] = response
        assert responses["workspace_mkdir"].content[0].text.endswith("already exists.")
        assert responses["workspace_read_file"].content[0].text == "2 | int needle = 1;"
        search = responses["workspace_text_search"]
        assert "2 | int needle = 1;" in search.content[0].text
        assert "spans" not in search.content[0].text
        assert 'Query: "needle" (literal text; case-sensitive)' in search.content[0].text
        assert search.structured_content["matches"][0]["spans"] == [[4, 10]]
        assert "Size:" in responses["workspace_find_files"].content[0].text
        assert "Modified:" in responses["workspace_file_info"].content[0].text
        assert "├──" in responses["workspace_list"].content[0].text or (
            "└──" in responses["workspace_list"].content[0].text
        )
        assert responses["workspace_move"].content[0].text == (
            "Moved project/text-output/sample.cpp to project/text-output/moved.cpp."
        )
        assert responses["workspace_delete"].content[0].text == (
            "Deleted project/text-output/moved.cpp."
        )
        for uri in (
            "forgemcp://workspace/list?path=project/text-output",
            "forgemcp://workspace/find-files?pattern=*.cpp&path=project/text-output",
            "forgemcp://workspace/file-info?path=project/README.md",
            "forgemcp://workspace/search?query=fixture&path=project/README.md",
        ):
            resource = await client.read_resource(uri)
            assert resource.contents[0].mime_type == "text/markdown"
            assert resource.contents[0].text.startswith("# ")


@pytest.fixture
def anyio_backend():
    """Run async tests on the asyncio backend used by the process and LSP services."""
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate_host_discovery(monkeypatch):
    """Replace host tool discovery with an empty collection for deterministic protocol tests."""
    from forgemcp.toolchain import discovery
    from forgemcp.toolchain.providers import visual_studio

    monkeypatch.setattr(discovery, "load_tools", lambda: ())

    async def discover(*args):
        """Return no host toolsets without launching discovery commands."""
        return ()

    monkeypatch.setattr(visual_studio, "discover", discover)


@pytest.mark.anyio
async def test_workspace_tools_have_apps_schemas_icons_and_progress(
    cpp_acceptance_project,
):
    """Verify workspace tools expose Apps metadata, schemas, icons, and throttled progress."""
    server = create_server(cpp_acceptance_project, progress_interval=0)
    progress = []

    async def collect(value, total, message):
        """Record workspace tool progress values for throttle assertions."""
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
        assert "workspace_text_search" in {tool.name for tool in tools}
        assert "workspace_search" not in {tool.name for tool in tools}
        assert "workspace_overview" not in {tool.name for tool in tools}
        assert (await client.list_prompts()).prompts == []
        for tool in tools:
            assert tool.output_schema and tool.icons and tool.meta["ui"]["resourceUri"]
            assert "JsonValue" not in tool.output_schema.get("$defs", {})
            assert "ctx" not in tool.input_schema.get("properties", {})
            assert "expected_revision" not in tool.input_schema.get("properties", {})
            app = await client.read_resource(tool.meta["ui"]["resourceUri"])
            assert app.contents[0].mime_type == APP_MIME_TYPE
        result = await client.call_tool("workspace_list", {}, progress_callback=collect)
        assert not result.is_error and result.structured_content["path"] == "project/"
        assert result.content
        assert result.content[0].text.startswith("project/\n")
        assert "```" not in result.content[0].text
        assert len(progress) > 2
        assert [value for value, _ in progress] == list(range(len(progress)))
        assert all(total is None for _, total in progress)
        for service in (
            WorkspaceService,
            ProcessService,
            ToolchainService,
            CMakeService,
            ClangdService,
        ):
            assert inspect.getdoc(service) in client.instructions


@pytest.mark.anyio
async def test_clangd_tools_are_registered_and_unavailable_configurations_fail_cleanly(
    cpp_acceptance_project,
):
    """Verify analysis tools are mounted and unavailable contexts produce recoverable tool
    errors.
    """
    async with Client(create_server(cpp_acceptance_project)) as client:
        tools = {
            tool.name: tool
            for tool in (await client.list_tools()).tools
            if tool.name.startswith("clangd_")
        }
        assert set(tools) == {
            "clangd_configurations",
            "clangd_diagnostics",
            "clangd_hover",
            "clangd_definition",
            "clangd_references",
            "clangd_document_symbols",
            "clangd_workspace_symbols",
        }
        for tool in tools.values():
            assert tool.output_schema and tool.icons and tool.meta["ui"]["resourceUri"]
        assert tools["clangd_diagnostics"].input_schema["properties"]["path"]["type"] == "string"
        contexts = await client.call_tool("clangd_configurations", {})
        assert not contexts.is_error and contexts.structured_content["result"] == []
        assert contexts.content[0].text == "Available clangd configurations: 0."
        diagnostics = await client.call_tool("clangd_diagnostics", {"path": "project/src/math.cpp"})
        assert diagnostics.is_error and "compilation database" in diagnostics.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["accept", "decline"])
async def test_external_read_requires_confirmation_and_never_creates_mirrors(
    cpp_acceptance_project,
    action,
):
    """Verify external reads require elicitation and never expose managed mirror resources."""
    external = cpp_acceptance_project / "README.md"
    path = "root/" + external.as_posix()
    questions = []

    async def confirm(context, params):
        """Record the external-read elicitation and return the parametrized operator decision."""
        questions.append(params.message)
        return ElicitResult(
            action=action,
            content={"allow": True} if action == "accept" else None,
        )

    async with Client(
        create_server(cpp_acceptance_project),
        elicitation_callback=confirm,
    ) as client:
        managed = await client.call_tool("workspace_read_file", {"path": "project/README.md"})
        assert not managed.is_error and questions == []
        read = await client.call_tool("workspace_read_file", {"path": path})
        assert len(questions) == 1 and str(external) in questions[0]
        if action == "accept":
            assert not read.is_error and read.structured_content["text"] == external.read_bytes().decode()
        else:
            assert read.is_error
        write = await client.call_tool("workspace_write_file", {"path": path, "text": "forbidden"})
        assert write.is_error
        assert external.read_bytes().decode() == managed.structured_content["text"]
        for kind in ("file", "raw"):
            with pytest.raises(MCPError):
                await client.read_resource(f"forgemcp://workspace/{kind}/{quote(path, safe='/')}")


@pytest.mark.anyio
async def test_full_file_workflow_through_client(cpp_acceptance_project):
    """Verify create, read, edit, move, search, metadata, and delete through the MCP client."""
    async with Client(create_server(cpp_acceptance_project)) as client:

        async def call(name, **arguments):
            """Call a workspace tool and require a successful protocol result."""
            result = await client.call_tool("workspace_" + name, arguments)
            assert not result.is_error, result.content
            return result.structured_content

        assert (await call("mkdir", path="project/new"))["action"] == "created"
        assert (await call("write_file", path="project/new/a.txt", text="one\ntwo two\n"))[
            "lines_added"
        ] == 2
        assert (
            await call(
                "edit_file",
                path="project/new/a.txt",
                old_text="two",
                new_text="three",
                replace_all=True,
            )
        )["replacements"] == 2
        assert (await call("read_file", path="project/new/a.txt", start_line=2))[
            "text"
        ] == "three three\n"
        assert (await call("file_info", path="project/new/a.txt"))["size_bytes"] == len(
            b"one\nthree three\n"
        )
        assert (await call("find_files", pattern="new/*.txt"))["paths"] == ["project/new/a.txt"]
        assert (await call("text_search", query="three", path="project/new"))["matches"][0][
            "line"
        ] == 2
        assert (await call("move", source="project/new/a.txt", destination="project/new/b.txt"))[
            "action"
        ] == "moved"
        assert (await call("delete", path="project/new/b.txt"))["action"] == "deleted"
        await call("delete", path="project/new")
        failed = await client.call_tool("workspace_read_file", {"path": "../outside"})
        assert failed.is_error


@pytest.mark.anyio
async def test_diff_resources_are_linked_typed_and_immutable(cpp_acceptance_project):
    """Verify mutations link typed diffs and earlier result resources remain unchanged."""
    async with Client(create_server(cpp_acceptance_project)) as client:
        written = await client.call_tool(
            "workspace_write_file",
            {"path": "project/diff.txt", "text": "old\nunchanged\n"},
        )
        result = written.structured_content
        assert written.content[0].text.startswith("Created ")
        assert "forgemcp://workspace/results/" not in written.content[0].text
        assert "diff" not in result and "changed_lines" not in result
        assert "extensions_uri" not in result
        resource = result["resources"]["diff"]
        assert resource["uri"].endswith("/diff.json")
        assert resource["mime_type"] == "application/json"
        frozen = (await client.read_resource(resource["uri"])).contents[0].text
        diff = FileDiff.model_validate_json(frozen)
        assert diff.path == WorkspacePath("project/diff.txt")
        assert [line.text for line in diff.hunks[0].lines] == ["old\n", "unchanged\n"]
        edited = await client.call_tool(
            "workspace_edit_file",
            {"path": "project/diff.txt", "old_text": "old", "new_text": "new"},
        )
        assert edited.structured_content["replacements"] == 1
        assert edited.content[0].text.endswith("Replaced 1 occurrence.")
        assert "diff" not in edited.structured_content
        changed = FileDiff.model_validate_json(
            (await client.read_resource(edited.structured_content["resources"]["diff"]["uri"])).contents[0].text
        )
        assert [line.kind for line in changed.hunks[0].lines] == ["removed", "added", "context"]
        (cpp_acceptance_project / "diff.txt").write_text("externally changed")
        assert (await client.read_resource(resource["uri"])).contents[0].text == frozen
        read = await client.call_tool("workspace_read_file", {"path": "project/diff.txt"})
        assert read.structured_content["resources"] == {}


@pytest.mark.anyio
async def test_mirrors_markdown_templates_regex_and_completions(cpp_acceptance_project):
    """Verify file mirrors, Markdown templates, regex search, and resource completions."""
    workspace = WorkspaceService(cpp_acceptance_project)
    (cpp_acceptance_project / "resource data").mkdir()
    text = "α\r\nfoo(x)/../#&\r\n```\n"
    (cpp_acceptance_project / "resource data/a#%.cpp").write_bytes(text.encode("utf-8"))
    (cpp_acceptance_project / "resource data/data.bin").write_bytes(b"\x00\xff\x01")
    async with Client(create_server(cpp_acceptance_project)) as client:
        templates = (await client.list_resource_templates()).resource_templates
        workspace_templates = [
            item
            for item in templates
            if str(item.uri_template).startswith("forgemcp://workspace/")
        ]
        assert len(workspace_templates) == 8
        uri = workspace.file_uri(WorkspacePath("project/resource data/a#%.cpp"))
        result = await client.read_resource(uri)
        assert result.contents[0].text == text
        assert result.contents[0].mime_type == "text/plain"
        raw = await client.read_resource(
            "forgemcp://workspace/raw/project/resource%20data/data.bin"
        )
        assert base64.b64decode(raw.contents[0].blob) == b"\x00\xff\x01"
        assert raw.contents[0].mime_type == "application/octet-stream"
        cases = [
            ("list", {"path": "project/resource data", "depth": "all"}, "a#%.cpp"),
            (
                "find-files",
                {"pattern": "resource data/*.cpp"},
                "a%23%25.cpp",
            ),
            ("file-info", {"path": "project/resource data/a#%.cpp"}, "size\\_bytes"),
            (
                "search",
                {
                    "query": r"foo\(x\)/\.\./#&",
                    "regex": "true",
                    "path": "project/resource data",
                },
                "foo(x)/../#&",
            ),
        ]
        for name, arguments, expected in cases:
            resource = await client.read_resource(
                f"forgemcp://workspace/{name}?{urlencode(arguments, quote_via=quote)}"
            )
            assert resource.contents[0].mime_type == "text/markdown"
            assert expected in resource.contents[0].text
        for template, argument, prefix, expected in [
            (WorkspaceService.FILE_URI, "path", "st", "storage/"),
            (WorkspaceService.FILE_URI, "path", "project/src/ma", "project/src/math.cpp"),
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
            "forgemcp://workspace/file/project/%2E%2E/secret",
            "forgemcp://workspace/search?query=%5B&regex=true",
            "forgemcp://workspace/file-info",
            "forgemcp://workspace/search",
        ):
            with pytest.raises(MCPError):
                await client.read_resource(uri)


@pytest.mark.anyio
async def test_default_project_custom_storage_and_resource_updates(
    cpp_acceptance_project,
    monkeypatch,
):
    """Verify default project selection, custom storage, and resources reflecting managed
    mutations.
    """
    monkeypatch.chdir(cpp_acceptance_project)
    storage = cpp_acceptance_project / "service-data"
    options = argument_parser().parse_args(["--workspace-storage", str(storage)])
    assert options.workspace_storage == storage
    async with Client(create_server(storage_root=storage)) as client:
        await client.call_tool("workspace_mkdir", {"path": "storage/index"})
        for text in ("first", "second"):
            await client.call_tool(
                "workspace_write_file",
                {"path": "storage/index/state", "text": text},
            )
            result = await client.read_resource(
                "forgemcp://workspace/file/storage/index/state"
            )
            assert result.contents[0].text == text
        assert (
            await client.call_tool("workspace_read_file", {"path": "project/README.md"})
        ).structured_content["path"] == "project/README.md"


@pytest.mark.anyio
async def test_hidden_directories_and_find_schema(cpp_acceptance_project):
    """Verify hidden-directory behavior and the public file-finder schema."""
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
            assert ("project/.hidden" in paths) is include_hidden
            assert "project/.visible-file" in paths
            resource = await client.read_resource(
                "forgemcp://workspace/list?include_hidden="
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
    """Verify unsupported resource roots fail promptly with actionable protocol errors."""
    async with Client(create_server(cpp_acceptance_project)) as client:
        for root in ("wrong", quote("цуацуа")):
            for suffix in (
                "file",
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
                        client.read_resource(
                            f"forgemcp://workspace/{suffix}/{root}/README.md"
                            if suffix in ("file", "raw")
                            else f"forgemcp://workspace/{suffix}?path={root}/README.md"
                        ),
                        timeout=2,
                    )
        with pytest.raises(MCPError, match="Path must start"):
            await client.read_resource("forgemcp://workspace/file")
        failed = await client.call_tool("workspace_delete", {"path": "project/src"})
        assert failed.is_error
        assert "src:" in failed.content[0].text


@pytest.mark.anyio
async def test_search_limits_and_immutable_skipped_resource(cpp_acceptance_project):
    """Verify compact tool and Markdown results link an immutable skipped-file snapshot."""
    source = cpp_acceptance_project / "search-limit.txt"
    source.write_text("needle\n" * 3, encoding="utf-8")
    binary = cpp_acceptance_project / "search-limit.bin"
    binary.write_bytes(b"\0")
    async with Client(create_server(cpp_acceptance_project)) as client:
        response = await client.call_tool(
            "workspace_text_search",
            {"query": "needle", "extensions": ["txt", "bin"], "max_matches": 1},
        )
        assert not response.is_error
        data = response.structured_content
        assert len(data["matches"]) == 1
        assert data["matches_count"] == 3
        assert data["matches_truncated"]
        assert data["skipped_files_count"] == 1
        assert "skipped_files" not in data
        assert "Returned 1 of 3 matching lines" in response.content[0].text
        assert "Matches truncated: True" in response.content[0].text
        assert "Skipped files: 1." in response.content[0].text
        link = data["resources"]["skipped_files"]
        saved = (await client.read_resource(link["uri"])).contents[0].text
        assert json.loads(saved) == {"skipped_files": ["project/search-limit.bin"]}
        binary.write_text("needle", encoding="utf-8")
        assert (await client.read_resource(link["uri"])).contents[0].text == saved
        markdown = (await client.read_resource(
            "forgemcp://workspace/search?query=needle&path=project/search-limit.txt&max_matches=2"
        )).contents[0].text
        assert "Returned 2 of 3" in markdown
        assert "matches\\_truncated=True" in markdown
        invalid = await client.call_tool(
            "workspace_text_search",
            {"query": "needle", "max_matches": 0},
        )
        assert invalid.is_error


@pytest.mark.anyio
async def test_scan_and_read_progress_and_exact_regex_spans(cpp_acceptance_project):
    """Verify scan and read progress and exact Unicode match spans in search results."""
    (cpp_acceptance_project / "unicode.txt").write_text(
        "😀 Straße STRASSE\n",
        encoding="utf-8",
    )
    (cpp_acceptance_project / "large.txt").write_text("x" * 150000, encoding="utf-8")
    progress = []

    async def collect(value, total, message):
        """Record progress values emitted during filesystem scanning and reading."""
        progress.append((value, total, message))

    async with Client(create_server(cpp_acceptance_project, progress_interval=0)) as client:
        for tool, arguments in (
            ("workspace_list", {"depth": None}),
            ("workspace_find_files", {}),
            ("workspace_text_search", {"query": "STRASSE", "case_sensitive": False}),
            ("workspace_read_file", {"path": "project/large.txt"}),
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
            if tool == "workspace_text_search":
                match = next(
                    item
                    for item in result.structured_content["matches"]
                    if item["path"] == "project/unicode.txt"
                )
                assert match["spans"] == [[2, 8], [9, 16]]
                assert [match["text"][a:b] for a, b in match["spans"]] == [
                    "Straße",
                    "STRASSE",
                ]


def test_progress_interval_cli(cpp_acceptance_project):
    """Verify the CLI parses the configured progress interval."""
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
    """Verify invalid progress intervals fail before any service is constructed."""
    def unexpected_service(*args, **kwargs):
        """Fail if validation incorrectly reaches service construction."""
        pytest.fail("Configuration must be validated before constructing services")

    monkeypatch.setattr("forgemcp.server.WorkspaceService", unexpected_service)
    with pytest.raises(ValueError, match="Progress interval"):
        create_server(cpp_acceptance_project, progress_interval=interval)

@pytest.mark.anyio
async def test_completions_follow_qualified_path_and_storage_context(cpp_acceptance_project):
    """Verify completions follow the selected managed root and surrounding resource arguments."""
    storage = cpp_acceptance_project / "service-data"
    (storage / "index").mkdir(parents=True)
    (storage / "index/data.xyz").write_text("data")
    async with Client(create_server(cpp_acceptance_project, storage_root=storage)) as client:
        async def complete(template, value, name="path", context=None):
            """Request resource argument completions through the in-process client."""
            response = await client.complete(
                ResourceTemplateReference(uri=template),
                {"name": name, "value": value},
                context_arguments=context,
            )
            return response.completion.values

        assert await complete(WorkspaceService.FILE_URI, "") == ["project/", "storage/"]
        assert await complete(WorkspaceService.FILE_URI, "storage/in") == ["storage/index/"]
        assert await complete(WorkspaceService.FILE_URI, "storage/index/d") == ["storage/index/data.xyz"]
        assert await complete(WorkspaceService.LIST_URI, "storage/index/") == []
        assert await complete(WorkspaceService.FILE_URI, "project/../") == []
        assert await complete(
            WorkspaceService.SEARCH_URI,
            "x",
            name="extensions",
            context={"path": "storage/index/"},
        ) == ["xyz"]
        assert await complete(
            WorkspaceService.FILE_URI,
            "project/service-data/in",
        ) == ["project/service-data/index/"]
        listing = await client.read_resource("forgemcp://workspace/list?path=storage/index/")
        assert "data.xyz" in listing.contents[0].text

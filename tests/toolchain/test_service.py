"""Toolset discovery, operator configuration, and read-only MCP inspection tests."""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import Client
from mcp.types import ResourceTemplateReference

from forgemcp.process.service import ProcessService
from forgemcp.toolchain import discovery
from forgemcp.toolchain.errors import (
    DuplicateToolsetError,
    InvalidToolPathError,
    ToolCommandError,
    ToolsetNotFoundError,
    UnknownToolError,
)
from forgemcp.toolchain.loader import load_tools
from forgemcp.toolchain.providers import system, user, visual_studio
from forgemcp.toolchain.service import ToolchainService
from forgemcp.toolchain.spec import ToolInfo, ToolKind, ToolSpec


def python_info():
    """Build discovery metadata for a Python executable acting as a test tool."""
    def create_spec(path, processes, environment, inherit_environment):
        """Bind a Python executable and its environment to a scripted version operation."""
        async def version():
            """Run the Python version script, drain output, and require a successful exit."""
            async with await processes.launch(
                path,
                ("-c", "import sys; print('12.3',file=sys.stderr)"),
                env=environment,
                inherit_environment=inherit_environment,
            ) as session:
                await session.close_stdin()
                output = "".join([chunk.text async for chunk in session.output()])
                assert await session.wait() == 0
                return output.strip()

        return ToolSpec("python", ToolKind.OTHER, path, {"version": version})

    return ToolInfo("python", ToolKind.OTHER, create_spec)


@pytest.mark.anyio
async def test_system_controlled_path_and_empty_toolset(
    cpp_acceptance_project,
    monkeypatch,
):
    """Verify PATH discovery binds exact executables and permits an empty system toolset."""
    processes = ProcessService(cpp_acceptance_project)
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    spec = python_info()
    try:
        toolset = system.discover((spec,), processes)
        assert toolset.id == "system" and toolset.name == "System"
        assert toolset.tools[0].path == Path(sys.executable).resolve()
        assert await toolset.tools[0].methods["version"]() == "12.3"
        monkeypatch.setenv("PATH", str(cpp_acceptance_project))
        assert system.discover((spec,), processes).tools == ()
    finally:
        await processes.close()


def test_user_configuration_multiple_exact_paths_and_stable_ids(
    cpp_acceptance_project,
    monkeypatch,
):
    # Executable content isn't probed while validating an explicit path.
    """Verify explicit toolsets retain exact paths and deterministic IDs without discovery
    commands.
    """
    executable = cpp_acceptance_project / (
        "compiler.exe" if os.name == "nt" else "compiler"
    )
    executable.write_text("fixture")
    executable.chmod(0o755)
    monkeypatch.chdir(cpp_acceptance_project)
    groups = [
        ["LLVM 20", f"clang={executable.name}"],
        ["LLVM 22", f"clang++={executable}"],
    ]
    specs = load_tools()
    config = user.parse_toolsets(groups, specs)
    processes = ProcessService(cpp_acceptance_project)
    result = user.discover(config, specs, processes)
    assert len(result) == 2
    assert result[0].id != result[1].id
    assert result[0].tools[0].path == executable.resolve()
    assert all(item.inherit_environment and item.environment is None for item in result)
    repeated = user.discover(user.parse_toolsets(groups, specs), specs, processes)
    assert [(item.id, item.tools[0].path) for item in result] == [
        (item.id, item.tools[0].path) for item in repeated
    ]
    assert not processes.records


@pytest.mark.parametrize(
    "groups,error",
    [
        ([["same", "git={path}"], ["same", "git={path}"]], DuplicateToolsetError),
        ([["one", "git={path}", "git={path}"]], ToolCommandError),
        ([["one", "unknown={path}"]], UnknownToolError),
        ([["one", "git=missing.exe"]], InvalidToolPathError),
        ([["one", "git="]], InvalidToolPathError),
        ([["one"]], ToolCommandError),
        ([["git={path}"]], ToolCommandError),
    ],
)
def test_bad_user_configuration_has_no_fallback(groups, error):
    """Verify invalid explicit tool names, paths, and assignments are rejected without fallback."""
    groups = [
        [value.format(path=sys.executable) for value in group] for group in groups
    ]
    with pytest.raises(error):
        user.parse_toolsets(groups, load_tools())


@pytest.mark.anyio
async def test_discovery_does_not_query_versions_and_returns_containers(
    cpp_acceptance_project,
    monkeypatch,
):
    """Verify discovery runs once and returns retained toolset containers without version probes."""
    processes = ProcessService(cpp_acceptance_project)
    monkeypatch.setattr(discovery, "load_tools", lambda: (python_info(),))
    monkeypatch.setattr(system, "locate", lambda spec: Path(sys.executable))

    async def no_vs(*args):
        """Disable Visual Studio discovery for this deterministic toolset test."""
        return ()

    monkeypatch.setattr(visual_studio, "discover", no_vs)
    service = ToolchainService(processes)
    await asyncio.gather(service.initialize(), service.initialize())
    before = service.toolsets
    monkeypatch.setattr(
        discovery,
        "load_tools",
        lambda: pytest.fail("discovery repeated"),
    )
    await service.initialize()
    assert service.toolsets is before
    assert (
        service.get_tool("system", "python") is service.get_toolset("system").tools[0]
    )
    assert service.get_tool("system", "missing") is None
    assert service.list_toolsets() is before
    with pytest.raises(ToolsetNotFoundError):
        service.get_toolset("unknown")
    assert not processes.records


@pytest.mark.anyio
async def test_visual_studio_not_run_on_non_windows(
    cpp_acceptance_project,
    monkeypatch,
):
    """Verify Visual Studio discovery returns immediately on non-Windows hosts."""
    monkeypatch.setattr(visual_studio, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(
        visual_studio,
        "vswhere_path",
        lambda: pytest.fail("not Windows"),
    )
    assert (
        await visual_studio.discover(
            load_tools(),
            ProcessService(cpp_acceptance_project),
        )
        == ()
    )


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows batch environment")
async def test_vsdevcmd_environment_preserves_values_and_transcript(
    cpp_acceptance_project,
    monkeypatch,
):
    """Verify developer-environment parsing preserves Unicode, equals signs, long values, and the
    transcript.
    """
    monkeypatch.setenv("FORGEMCP_UNICODE", "Лаборатория 日本語")
    root = cpp_acceptance_project / "Visual Studio & test"
    batch = root / "Common7/Tools/VsDevCmd.bat"
    batch.parent.mkdir(parents=True)
    batch.write_text(
        '@echo off\nset "FORGEMCP_INSTANCE=instance-one"\nset "FORGEMCP_LONG='
        + "x" * 5000
        + '"\nset "FORGEMCP_EQUALS=a=b=c"\n',
        encoding="utf-8",
    )
    processes = ProcessService(cpp_acceptance_project)
    try:
        environment = await visual_studio.instance_environment(root, processes)
        assert environment["FORGEMCP_INSTANCE"] == "instance-one"
        assert environment["FORGEMCP_EQUALS"] == "a=b=c"
        assert environment["FORGEMCP_UNICODE"] == "Лаборатория 日本語"
        assert len(environment["FORGEMCP_LONG"]) == 5000
        record = next(iter(processes.records.values()))
        assert "FORGEMCP_INSTANCE=instance-one" in "".join(
            entry.text for entry in record.transcript
        )
    finally:
        await processes.close()


@pytest.mark.anyio
async def test_multiple_vs_instances_partial_tools_env_and_failure_isolation(
    cpp_acceptance_project,
    monkeypatch,
):
    """Verify separate instance environments, partial toolsets, and isolation of failed Visual
    Studio setup.
    """
    roots = [cpp_acceptance_project / f"VS {index}" for index in range(3)]
    for root in roots:
        (root / "VC/Tools/Llvm/x64/bin").mkdir(parents=True)
        (root / "VC/Tools/Llvm/x64/bin/clang.exe").write_bytes(
            b"not a PE; no architecture parsing"
        )
    instances = [
        dict(
            instanceId=str(index),
            displayName=f"VS {index}",
            installationPath=str(root),
        )
        for index, root in enumerate(roots)
    ]

    class ListingProcesses(ProcessService):
        """A process service that substitutes a scripted Visual Studio instance listing."""

        async def launch(self, executable, arguments=(), **kwargs):
            """Replace vswhere with a Python JSON emitter and delegate other launches unchanged."""
            if str(executable) == "vswhere.exe":
                return await super().launch(
                    sys.executable,
                    ("-c", f"print({json.dumps(instances)!r})"),
                    **kwargs,
                )
            return await super().launch(executable, arguments, **kwargs)

    processes = ListingProcesses(cpp_acceptance_project)
    monkeypatch.setattr(
        visual_studio,
        "os",
        SimpleNamespace(name="nt", environ=os.environ),
    )
    monkeypatch.setattr(visual_studio, "vswhere_path", lambda: Path("vswhere.exe"))

    async def environment(root, service):
        """Return an instance-specific environment or fail the selected broken installation."""
        if root == roots[1]:
            raise ToolCommandError("broken instance")
        return {"INSTANCE": str(root)}

    monkeypatch.setattr(visual_studio, "instance_environment", environment)
    try:
        result = await visual_studio.discover(load_tools(), processes)
        assert [item.id for item in result] == [
            "visual-studio-0",
            "visual-studio-1",
            "visual-studio-2",
        ]
        assert result[1].tools == ()
        assert result[0].tools[0].name == "clang"
        assert len(result[0].tools) == 1
        for index in (0, 2):
            assert result[index].environment == {"INSTANCE": str(roots[index])}
            assert result[index].inherit_environment is False
            assert callable(result[index].tools[0].methods["version"])
        assert len(processes.records) == 1
        record = next(iter(processes.records.values()))
        assert record.status.current_status == 0
        assert "installationPath" in "".join(
            entry.text for entry in record.transcript
        )
    finally:
        await processes.close()


@pytest.mark.anyio
@pytest.mark.parametrize("interval", [0, 1])
async def test_mcp_tools_resources_progress_completions_and_transcripts(
    cpp_acceptance_project,
    monkeypatch,
    interval,
):
    """Verify toolset tools, resources, progress, completions, and retained version process logs."""
    monkeypatch.setattr(discovery, "load_tools", lambda: (python_info(),))
    monkeypatch.setattr(system, "locate", lambda spec: Path(sys.executable))

    async def no_vs(*args):
        """Disable Visual Studio discovery to isolate the test toolset."""
        return ()

    monkeypatch.setattr(visual_studio, "discover", no_vs)
    from forgemcp.server import create_server

    monkeypatch.setattr("forgemcp.progress.monotonic", lambda: 0)
    progress = []

    async def collect(value, total, message):
        """Record toolset progress values for invocation-specific throttle checks."""
        progress.append(value)

    async with Client(
        create_server(cpp_acceptance_project, progress_interval=interval),
        raise_exceptions=True,
    ) as client:
        tools = (await client.list_tools()).tools
        for name in ("toolsets_list", "toolset_get"):
            tool = next(tool for tool in tools if tool.name == name)
            assert tool.icons and tool.output_schema
            assert tool.annotations.read_only_hint
            assert tool.annotations.title is None
            assert tool.meta["ui"]["resourceUri"] == ToolchainService.WIDGET.uri
        result = await client.call_tool("toolsets_list", {}, progress_callback=collect)
        assert result.structured_content == {
            "result": [{"id": "system", "name": "System", "tools": ["python"]}]
        }
        assert result.content and progress == ([0.0, 1.0] if interval == 0 else [0.0])
        progress.clear()
        result = await client.call_tool(
            "toolset_get",
            {"toolset_id": "system"},
            progress_callback=collect,
        )
        assert progress == ([0.0, 1.0] if interval == 0 else [0.0])
        tool_info = result.structured_content["tools"][0]
        assert Path(tool_info["path"]).resolve() == Path(sys.executable).resolve()
        assert tool_info == dict(
            name="python",
            kind="other",
            path=tool_info["path"],
            version="12.3",
        )
        overview = await client.call_tool("processes_overview", {})
        assert overview.structured_content["completed"] == 1
        pid = overview.structured_content["processes"][0]["summary"]["process_id"]
        transcript = await client.read_resource(f"forgemcp://processes/{pid}")
        assert "12.3" in transcript.contents[0].text
        listing = await client.read_resource(ToolchainService.LIST_URI)
        details = await client.read_resource("forgemcp://toolsets/system")
        assert (
            "System" in listing.contents[0].text and "12.3" in details.contents[0].text
        )
        completion = await client.complete(
            ResourceTemplateReference(uri=ToolchainService.DETAILS_URI),
            {"name": "toolset_id", "value": "sys"},
        )
        assert completion.completion.values == ["system"]
        app = await client.read_resource(ToolchainService.WIDGET.uri)
        assert "Available toolsets" in app.contents[0].text
        # Details query again, while list calls do not launch version probes.
        await client.call_tool("toolsets_list", {})
        assert (await client.call_tool("processes_overview", {})).structured_content[
            "completed"
        ] == 2


@pytest.mark.anyio
async def test_unknown_ids_at_mcp_boundary(cpp_acceptance_project, monkeypatch):
    """Verify unknown toolset IDs become actionable MCP tool and resource errors."""
    from mcp.shared.exceptions import MCPError
    from forgemcp.server import create_server

    monkeypatch.setattr(discovery, "load_tools", lambda: ())

    async def no_vs(*args):
        """Disable Visual Studio discovery while testing invalid toolset identifiers."""
        return ()

    monkeypatch.setattr(visual_studio, "discover", no_vs)
    async with Client(
        create_server(cpp_acceptance_project),
        raise_exceptions=True,
    ) as client:
        result = await client.call_tool("toolset_get", {"toolset_id": "missing"})
        assert result.is_error and "Unknown toolset" in result.content[0].text
        with pytest.raises(MCPError, match="Unknown toolset"):
            await client.read_resource("forgemcp://toolsets/missing")


@pytest.mark.anyio
async def test_invalid_user_path_launches_nothing(cpp_acceptance_project, monkeypatch):
    """Verify invalid operator paths fail before discovery launches or publishes toolsets."""
    processes = ProcessService(cpp_acceptance_project)
    service = ToolchainService(processes, [["bad", "git=does-not-exist"]])
    with pytest.raises(InvalidToolPathError):
        await service.initialize()
    assert processes.records == {}
    assert service.toolsets == () and service.initialized is False


def test_cli_repeated_option():
    """Verify repeated toolset CLI options retain separate groups and exact assignments."""
    from forgemcp.server import argument_parser

    parsed = argument_parser().parse_args(
        [
            "--toolset",
            "LLVM 20",
            "clang=a",
            "clang++=b",
            "--toolset",
            "LLVM 22",
            "clang=c",
        ]
    )
    assert parsed.toolset == [
        ["LLVM 20", "clang=a", "clang++=b"],
        ["LLVM 22", "clang=c"],
    ]


@pytest.mark.anyio
async def test_mcp_version_failures_are_isolated_and_not_cached(
    cpp_acceptance_project,
    monkeypatch,
):
    """Verify version failures stay isolated, omit transcripts, and are retried on later
    requests.
    """
    from forgemcp.server import create_server
    from forgemcp.toolchain.errors import ToolParserError

    calls = []

    def info(name, failure=None, available=True):
        """Build test tool metadata with an optional failing or unavailable version operation."""
        def create_spec(path, processes, environment, inherit_environment):
            """Bind the instrumented version operation only when it is available."""
            async def version():
                """Record each probe and return a changing value or raise the configured failure."""
                calls.append(name)
                if failure:
                    raise failure("PRIVATE_TRANSCRIPT")
                return str(calls.count(name))

            return ToolSpec(
                name,
                ToolKind.OTHER,
                path,
                {"version": version} if available else {},
            )

        return ToolInfo(name, ToolKind.OTHER, create_spec)

    infos = (
        info("good"),
        info("failed", ToolCommandError),
        info("unparsed", ToolParserError),
        info("unavailable", available=False),
    )
    monkeypatch.setattr(discovery, "load_tools", lambda: infos)
    monkeypatch.setattr(system, "locate", lambda spec: Path(sys.executable))

    async def no_vs(*args):
        """Disable Visual Studio discovery while exercising scripted version failures."""
        return ()

    monkeypatch.setattr(visual_studio, "discover", no_vs)
    async with Client(
        create_server(cpp_acceptance_project),
        raise_exceptions=True,
    ) as client:
        await client.call_tool("toolsets_list", {})
        await client.read_resource(ToolchainService.LIST_URI)
        assert calls == []
        for expected in ("1", "2"):
            result = await client.call_tool("toolset_get", {"toolset_id": "system"})
            assert not result.is_error
            versions = {
                item["name"]: item["version"]
                for item in result.structured_content["tools"]
            }
            assert versions == {
                "good": expected,
                "failed": None,
                "unparsed": None,
                "unavailable": None,
            }
            assert "environment" not in result.structured_content
            assert "PRIVATE_TRANSCRIPT" not in str(result.content)
        resource = await client.read_resource("forgemcp://toolsets/system")
        text = resource.contents[0].text
        assert "| good |" in text and "| 3 |" in text and "Unknown" in text
        assert "PRIVATE_TRANSCRIPT" not in text
        assert calls.count("good") == 3
        assert calls.count("failed") == calls.count("unparsed") == 3
        assert "unavailable" not in calls

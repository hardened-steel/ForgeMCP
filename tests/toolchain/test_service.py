import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import Client
from mcp.types import ResourceTemplateReference

from forgemcp.process.service import ProcessService
from forgemcp.toolchain import discovery
from forgemcp.toolchain.errors import (
    DuplicateToolsetError, InvalidToolPathError, ToolCommandError, ToolsetNotFoundError, UnknownToolError,
)
from forgemcp.toolchain.loader import load_tools
from forgemcp.toolchain.providers import system, user, visual_studio
from forgemcp.toolchain.service import ToolchainService
from forgemcp.toolchain.spec import ToolCommand, ToolKind, ToolSpec, version_parser


def python_spec():
    return ToolSpec("python", ToolKind.OTHER, None, None,
        {"version": ToolCommand(lambda: ("-c", "import sys; print('version 12.3',file=sys.stderr)"), parser="version")},
        {"version": version_parser(r"version ([0-9.]+)")})


@pytest.mark.anyio
async def test_system_controlled_path_and_empty_toolset(cpp_acceptance_project, monkeypatch):
    processes = ProcessService(cpp_acceptance_project)
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    spec = python_spec()
    try:
        toolset = system.discover((spec,), processes)
        assert toolset.id == "system" and toolset.name == "System"
        assert toolset.tools[0].path == Path(sys.executable).resolve()
        assert await toolset.tools[0].commands["version"]() == "12.3"
        monkeypatch.setenv("PATH", str(cpp_acceptance_project))
        assert system.discover((spec,), processes).tools == ()
    finally:
        await processes.close()


def test_user_configuration_multiple_exact_paths_and_stable_ids(cpp_acceptance_project, monkeypatch):
    # Executable content isn't probed while validating an explicit path.
    executable = cpp_acceptance_project / ("compiler.exe" if os.name == "nt" else "compiler")
    executable.write_text("fixture")
    executable.chmod(0o755)
    monkeypatch.chdir(cpp_acceptance_project)
    groups = [["LLVM 20", f"clang={executable.name}"], ["LLVM 22", f"clang++={executable}"]]
    specs = load_tools()
    config = user.parse_toolsets(groups, specs)
    processes = ProcessService(cpp_acceptance_project)
    result = user.discover(config, specs, processes)
    assert len(result) == 2
    assert result[0].id != result[1].id
    assert result[0].tools[0].path == executable.resolve()
    assert all(item.inherit_environment and item.environment is None for item in result)
    assert result == user.discover(user.parse_toolsets(groups, specs), specs, processes)


@pytest.mark.parametrize("groups,error", [
    ([["same", "git={path}"], ["same", "git={path}"]], DuplicateToolsetError),
    ([["one", "git={path}", "git={path}"]], ToolCommandError),
    ([["one", "unknown={path}"]], UnknownToolError),
    ([["one", "git=missing.exe"]], InvalidToolPathError),
    ([["one", "git="]], InvalidToolPathError),
    ([["one"]], ToolCommandError),
    ([["git={path}"]], ToolCommandError),
])
def test_bad_user_configuration_has_no_fallback(groups, error):
    groups = [[value.format(path=sys.executable) for value in group] for group in groups]
    with pytest.raises(error):
        user.parse_toolsets(groups, load_tools())


@pytest.mark.anyio
async def test_cache_versions_absent_commands_failure_isolation_and_api(cpp_acceptance_project, monkeypatch):
    processes = ProcessService(cpp_acceptance_project)
    good = python_spec()
    failed = replace(good, name="failed", commands={"version": ToolCommand(lambda: ("-c", "raise SystemExit(8)"), parser="version")})
    no_version = replace(good, name="no-version", commands={}, parsers={})
    unparsed = replace(good, name="unparsed", commands={"version": ToolCommand(lambda: ("-V",))}, parsers={})
    specs = (good, failed, no_version, unparsed)
    monkeypatch.setattr(discovery, "load_tools", lambda: specs)
    monkeypatch.setattr(system, "locate", lambda spec: Path(sys.executable))
    async def no_vs(*args):
        return ()
    monkeypatch.setattr(visual_studio, "discover", no_vs)
    service = ToolchainService(processes)
    try:
        await asyncio.gather(service.initialize(), service.initialize())
        assert len(processes.records) == 3
        before = service.cache
        monkeypatch.setattr(discovery, "load_tools", lambda: pytest.fail("cache reloaded"))
        await service.initialize()
        assert service.cache is before
        assert service.get_tool("system", "python").version == "12.3"
        assert service.get_tool("system", "failed").version is None
        assert service.get_tool("system", "missing") is None
        assert service.resolve_toolset("system").tools[0].name == "failed"
        details = service.get_toolset("system")
        details.tools.clear()
        assert len(service.get_toolset("system").tools) == 4
        assert "environment" not in service.get_toolset("system").model_dump()
        with pytest.raises(ToolsetNotFoundError):
            service.get_toolset("unknown")
        assert len(processes.records) == 3
    finally:
        await processes.close()


@pytest.mark.anyio
async def test_visual_studio_not_run_on_non_windows(cpp_acceptance_project, monkeypatch):
    monkeypatch.setattr(visual_studio, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(visual_studio, "vswhere_path", lambda: pytest.fail("not Windows"))
    assert await visual_studio.discover(load_tools(), ProcessService(cpp_acceptance_project)) == ()


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows batch environment")
async def test_vsdevcmd_environment_preserves_values_and_transcript(cpp_acceptance_project, monkeypatch):
    monkeypatch.setenv("FORGEMCP_UNICODE", "Лаборатория 日本語")
    root = cpp_acceptance_project / "Visual Studio & test"
    batch = root / "Common7/Tools/VsDevCmd.bat"
    batch.parent.mkdir(parents=True)
    batch.write_text('@echo off\nset "FORGEMCP_INSTANCE=instance-one"\nset "FORGEMCP_LONG=' + "x" * 5000 + '"\nset "FORGEMCP_EQUALS=a=b=c"\n', encoding="utf-8")
    processes = ProcessService(cpp_acceptance_project)
    try:
        environment = await visual_studio.instance_environment(root, processes)
        assert environment["FORGEMCP_INSTANCE"] == "instance-one"
        assert environment["FORGEMCP_EQUALS"] == "a=b=c"
        assert environment["FORGEMCP_UNICODE"] == "Лаборатория 日本語"
        assert len(environment["FORGEMCP_LONG"]) == 5000
        record = next(iter(processes.records.values()))
        assert any("FORGEMCP_INSTANCE=instance-one" in entry.text for entry in record.transcript)
    finally:
        await processes.close()


@pytest.mark.anyio
async def test_multiple_vs_instances_partial_tools_env_and_failure_isolation(cpp_acceptance_project, monkeypatch):
    roots = [cpp_acceptance_project / f"VS {index}" for index in range(3)]
    for root in roots:
        (root / "VC/Tools/Llvm/x64/bin").mkdir(parents=True)
        (root / "VC/Tools/Llvm/x64/bin/clang.exe").write_bytes(b"not a PE; no architecture parsing")
    instances = [dict(instanceId=str(index), displayName=f"VS {index}", installationPath=str(root))
                 for index, root in enumerate(roots)]
    class ListingProcesses(ProcessService):
        async def start(self, executable, arguments=(), **kwargs):
            if str(executable) == "vswhere.exe":
                return await super().start(sys.executable, ("-c", f"print({json.dumps(instances)!r})"), **kwargs)
            return await super().start(executable, arguments, **kwargs)
    processes = ListingProcesses(cpp_acceptance_project)
    monkeypatch.setattr(visual_studio, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(visual_studio, "vswhere_path", lambda: Path("vswhere.exe"))
    async def environment(root, service):
        if root == roots[1]:
            raise ToolCommandError("broken instance")
        return {"INSTANCE": str(root)}
    monkeypatch.setattr(visual_studio, "instance_environment", environment)
    try:
        result = await visual_studio.discover(load_tools(), processes)
        assert [item.id for item in result] == ["visual-studio-0", "visual-studio-1", "visual-studio-2"]
        assert result[1].tools == ()
        assert result[0].tools[0].name == "clang"
        assert len(result[0].tools) == 1
        for index in (0, 2):
            command = result[index].tools[0].commands["version"]
            assert command.environment == {"INSTANCE": str(roots[index])}
            assert command.inherit_environment is False
        assert processes.overview().completed == 1
        assert any("installationPath" in entry.text for entry in next(iter(processes.records.values())).transcript)
    finally:
        await processes.close()


@pytest.mark.anyio
async def test_mcp_tools_resources_progress_completions_and_transcripts(cpp_acceptance_project, monkeypatch):
    monkeypatch.setattr(discovery, "load_tools", lambda: (python_spec(),))
    monkeypatch.setattr(system, "locate", lambda spec: Path(sys.executable))
    async def no_vs(*args):
        return ()
    monkeypatch.setattr(visual_studio, "discover", no_vs)
    from forgemcp.server import create_server
    progress = []
    async def collect(value, total, message):
        progress.append(value)
    async with Client(create_server(cpp_acceptance_project), raise_exceptions=True) as client:
        tools = (await client.list_tools()).tools
        for name in ("toolsets_list", "toolset_get"):
            tool = next(tool for tool in tools if tool.name == name)
            assert tool.icons and tool.output_schema
            assert tool.annotations.read_only_hint
            assert tool.annotations.title is None
            assert tool.meta["ui"]["resourceUri"] == ToolchainService.WIDGET.uri
        result = await client.call_tool("toolsets_list", {}, progress_callback=collect)
        assert result.structured_content == {"result": [{"id": "system", "name": "System", "tools": ["python"]}]}
        assert result.content and progress == [1., 2.]
        progress.clear()
        result = await client.call_tool("toolset_get", {"toolset_id": "system"}, progress_callback=collect)
        assert progress == [1., 2.]
        assert result.structured_content["tools"][0] == dict(name="python", kind="other", path=str(Path(sys.executable).resolve()), version="12.3")
        overview = await client.call_tool("processes_overview", {})
        assert overview.structured_content["completed"] == 1
        pid = overview.structured_content["processes"][0]["process_id"]
        transcript = await client.read_resource(f"forgemcp://processes/{pid}")
        assert "version 12.3" in transcript.contents[0].text
        listing = await client.read_resource(ToolchainService.LIST_URI)
        details = await client.read_resource("forgemcp://toolsets/system")
        assert "System" in listing.contents[0].text and "12.3" in details.contents[0].text
        completion = await client.complete(ResourceTemplateReference(uri=ToolchainService.DETAILS_URI), {"name": "toolset_id", "value": "sys"})
        assert completion.completion.values == ["system"]
        app = await client.read_resource(ToolchainService.WIDGET.uri)
        assert "Available toolsets" in app.contents[0].text
        # Reads and ordinary list/get calls never probe again.
        assert (await client.call_tool("processes_overview", {})).structured_content["completed"] == 1


@pytest.mark.anyio
async def test_unknown_ids_at_mcp_boundary(cpp_acceptance_project, monkeypatch):
    from mcp.shared.exceptions import MCPError
    from forgemcp.server import create_server
    monkeypatch.setattr(discovery, "load_tools", lambda: ())
    async def no_vs(*args):
        return ()
    monkeypatch.setattr(visual_studio, "discover", no_vs)
    async with Client(create_server(cpp_acceptance_project), raise_exceptions=True) as client:
        result = await client.call_tool("toolset_get", {"toolset_id": "missing"})
        assert result.is_error and "Unknown toolset" in result.content[0].text
        with pytest.raises(MCPError, match="Unknown toolset"):
            await client.read_resource("forgemcp://toolsets/missing")


@pytest.mark.anyio
async def test_invalid_user_path_launches_nothing(cpp_acceptance_project, monkeypatch):
    processes = ProcessService(cpp_acceptance_project)
    service = ToolchainService(processes, [["bad", "git=does-not-exist"]])
    with pytest.raises(InvalidToolPathError):
        await service.initialize()
    assert processes.records == {}
    assert service.cache == () and service.initialized is False


def test_cli_repeated_option():
    from forgemcp.server import argument_parser
    parsed = argument_parser().parse_args(["--toolset", "LLVM 20", "clang=a", "clang++=b", "--toolset", "LLVM 22", "clang=c"])
    assert parsed.toolset == [["LLVM 20", "clang=a", "clang++=b"], ["LLVM 22", "clang=c"]]

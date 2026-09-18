"""Windows Visual Studio instances, installation layouts, and VsDevCmd environments."""

import json
import os
from itertools import islice
from pathlib import Path
from urllib.parse import quote

from forgemcp.process.errors import ProcessError
from forgemcp.process.models import ProcessEncoding, ProcessTimeout
from forgemcp.process.service import ProcessService

from ..errors import ToolCommandError
from ..spec import ToolInfo, Toolset


MAX_INSTANCES = 128
MAX_LAYOUT_ENTRIES = 512


def vswhere_path() -> Path | None:
    folder = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    path = Path(folder) / "Microsoft Visual Studio/Installer/vswhere.exe"
    return path.resolve() if path.is_file() else None


async def instance_environment(root: Path, processes: ProcessService) -> dict[str, str]:
    batch = root / "Common7/Tools/VsDevCmd.bat"
    if not batch.is_file():
        raise ToolCommandError("Visual Studio developer environment is unavailable.")
    # cmd is the explicit interpreter for the batch script. Shell operators here
    # are provider-owned, not executable arguments supplied by project data.
    comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    session = await processes.launch(
        comspec, ("/u", "/d", "/s", "/c", "call", str(batch), "-no_logo", "-arch=x64", "&&", "set"),
        encoding=ProcessEncoding("utf-16-le"), timeout=ProcessTimeout(total=60),
    )
    environment: dict[str, str] = {}
    pending = ""
    async with session:
        await session.close_stdin()
        async for output in session.output():
            if output.stream != "stdout":
                continue
            pending += output.text
            lines = pending.split("\n")
            pending = lines.pop()
            for line in lines:
                name, separator, value = line.rstrip("\r").partition("=")
                if separator and name:
                    environment[name] = value
        if pending:
            name, separator, value = pending.rstrip("\r").partition("=")
            if separator and name:
                environment[name] = value
        result = await session.wait()
    if result != 0 or not environment:
        raise ToolCommandError("Visual Studio developer environment failed.")
    return environment


def directories(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    # Only enumerate known shallow layout directories, never a recursive tree scan.
    return sorted((path for path in islice(root.iterdir(), MAX_LAYOUT_ENTRIES)
                   if path.is_dir()), reverse=True)


def locate(root: Path, name: str) -> Path | None:
    candidates: list[Path] = []
    if name in {"cl", "link"}:
        for version in directories(root / "VC/Tools/MSVC"):
            for host in ("Hostx64/x64", "Hostarm64/arm64", "Hostx86/x86"):
                candidates.append(version / "bin" / host / f"{name}.exe")
    if name == "msbuild":
        candidates += [root / "MSBuild/Current/Bin/amd64/MSBuild.exe",
                       root / "MSBuild/Current/Bin/MSBuild.exe"]
    if name in {"cmake", "ctest", "ninja"}:
        relative = "Ninja/ninja.exe" if name == "ninja" else f"CMake/bin/{name}.exe"
        candidates.append(root / "Common7/IDE/CommonExtensions/Microsoft/CMake" / relative)
    if name in {"clang", "clang++", "clang-cl", "lld", "lld-link", "clangd", "lldb-dap"}:
        for folder in ("VC/Tools/Llvm/x64/bin", "VC/Tools/Llvm/ARM64/bin", "VC/Tools/Llvm/bin"):
            candidates.append(root / folder / f"{name}.exe")
    if name == "git":
        candidates.append(root / "Common7/IDE/CommonExtensions/Microsoft/TeamFoundation/Team Explorer/Git/cmd/git.exe")
    if name == "cppvsdbg":
        for relative in ("Common7/IDE/Extensions/Microsoft/DebugAdapterHost/OpenDebugAD7.exe",
                         "Common7/IDE/CommonExtensions/Microsoft/VC/Debugger/OpenDebugAD7.exe"):
            candidates.append(root / relative)
    return next((path.resolve() for path in candidates if path.is_file()), None)


async def discover(specs: tuple[ToolInfo, ...], processes: ProcessService) -> tuple[Toolset, ...]:
    if os.name != "nt":
        return ()
    executable = vswhere_path()
    if executable is None:
        return ()
    try:
        session = await processes.launch(
            executable, ("-all", "-prerelease", "-products", "*", "-format", "json", "-utf8"),
            encoding=ProcessEncoding("utf-8"), timeout=ProcessTimeout(total=30),
        )
        async with session:
            await session.close_stdin()
            # JSON must be decoded as a document; cap only this discovery listing.
            document = ""
            async for output in session.output():
                if output.stream == "stdout":
                    if len(document) + len(output.text) > 4 * 1024 * 1024:
                        raise ToolCommandError("Visual Studio instance listing is too large.")
                    document += output.text
            terminal = await session.wait()
        if terminal != 0:
            return ()
        instances = json.loads(document)
        if not isinstance(instances, list):
            return ()
    except (ProcessError, ToolCommandError, ValueError, OSError):
        return ()
    found = []
    seen = set()
    for instance in instances[:MAX_INSTANCES]:
        if not isinstance(instance, dict):
            continue
        if not all(isinstance(instance.get(key), str) and instance[key]
                   for key in ("instanceId", "installationPath", "displayName")):
            continue
        identifier = "visual-studio-" + quote(instance["instanceId"], safe="")
        if identifier in seen:
            continue
        seen.add(identifier)
        root = Path(instance["installationPath"])
        try:
            environment = await instance_environment(root, processes)
        except (ProcessError, ToolCommandError, OSError):
            # Keep a failed instance visible, but don't bind commands to a wrong env.
            found.append(Toolset(identifier, instance["displayName"], (), None, False))
            continue
        bound = []
        for spec in specs:
            try:
                path = locate(root, spec.name)
                if path is not None:
                    bound.append(spec.create_spec(path, processes, environment, False))
            except OSError:
                continue
        found.append(Toolset(identifier, instance["displayName"], tuple(bound), environment, False))
    return tuple(found)

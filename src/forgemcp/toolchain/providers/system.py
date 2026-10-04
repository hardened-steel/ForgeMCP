"""PATH and operating-system executable discovery for the System toolset."""

import os
import shutil
from itertools import islice
from pathlib import Path

from forgemcp.process.service import ProcessService

from ..spec import ToolInfo, Toolset


def locate(spec: ToolInfo) -> Path | None:
    """Find a named executable through PATH and supported platform-specific adapter locations."""
    name = spec.name
    # POSIX link is a filesystem utility, not Microsoft's linker.
    if os.name != "nt" and name in {"cl", "link", "msbuild", "cppvsdbg"}:
        return None
    executable = shutil.which(name)
    if executable:
        return Path(executable).resolve()
    if name == "cppvsdbg" and os.name == "nt":
        executable = shutil.which("OpenDebugAD7.exe")
        if executable:
            return Path(executable).resolve()
        for folder in (".vscode", ".vscode-insiders"):
            extensions = Path.home() / folder / "extensions"
            if extensions.is_dir():
                for extension in sorted(
                    islice(extensions.iterdir(), 512),
                    reverse=True,
                ):
                    if extension.name.startswith("ms-vscode.cpptools-"):
                        for relative in (
                            "debugAdapters/vsdbg/bin/vsdbg.exe",
                            "debugAdapters/bin/OpenDebugAD7.exe",
                        ):
                            path = extension / relative
                            if path.is_file():
                                return path.resolve()
    return None


def discover(specs: tuple[ToolInfo, ...], processes: ProcessService) -> Toolset:
    """Bind all available system executables into a toolset inheriting the server environment."""
    found = []
    for spec in specs:
        try:
            path = locate(spec)
        except OSError:
            continue
        if path is not None:
            found.append(spec.create_spec(path, processes, None, True))
    return Toolset("system", "System", tuple(found), None, True)

"""Build the MCP App frontend before Hatch packages the Python wheel."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Produce frontend assets as part of the wheel build."""

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        """Build missing frontend assets and include them in the wheel artifacts."""
        frontend = Path(self.root) / "frontend"
        outputs = [
            *(
                Path(self.root)
                / "src"
                / "forgemcp"
                / "assets"
                / f"workspace-{kind}.html"
                for kind in ("tree", "file", "search", "result")
            ),
            Path(self.root) / "src" / "forgemcp" / "assets" / "process-overview.html",
            Path(self.root) / "src" / "forgemcp" / "assets" / "process-details.html",
            Path(self.root) / "src" / "forgemcp" / "assets" / "toolsets.html",
            Path(self.root) / "src" / "forgemcp" / "assets" / "clangd-result.html",
            *(
                Path(self.root) / "src" / "forgemcp" / "assets" / f"cmake-{kind}.html"
                for kind in ("profiles", "configure", "build", "test")
            ),
        ]
        artifacts = build_data.setdefault("artifacts", [])
        if not isinstance(artifacts, list):
            raise TypeError("Hatch build data 'artifacts' must be a list.")
        artifacts.append("src/forgemcp/assets/*.html")

        if version == "editable" and all(output.is_file() for output in outputs):
            return

        command = "npm.cmd" if os.name == "nt" else "npm"
        npm = shutil.which(command)
        if npm is None:
            raise RuntimeError(
                "Node.js and npm are required to build ForgeMCP widgets."
            )

        subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=frontend, check=True)
        subprocess.run([npm, "run", "build"], cwd=frontend, check=True)
        missing = [str(output) for output in outputs if not output.is_file()]
        if missing:
            raise RuntimeError(f"Frontend build did not produce: {', '.join(missing)}.")

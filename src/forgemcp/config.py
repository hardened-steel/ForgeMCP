"""Configuration shared by ForgeMCP services and plugins."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ForgeConfig:
    """Runtime limits and the project root a server instance may access."""

    workspace_root: Path
    max_read_chars: int = 100_000
    max_listed_files: int = 10_000
    process_timeout_seconds: float = 300.0
    max_process_output_chars: int = 1_000_000

    @classmethod
    def from_environment(cls) -> "ForgeConfig":
        """Build configuration from environment variables with safe defaults."""
        root = Path(os.environ.get("FORGEMCP_WORKSPACE", Path.cwd())).resolve()
        return cls(
            workspace_root=root,
            process_timeout_seconds=float(
                os.environ.get("FORGEMCP_PROCESS_TIMEOUT_SECONDS", "300")
            ),
        )

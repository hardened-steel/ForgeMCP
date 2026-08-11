"""Safe, workspace-scoped filesystem operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from forgemcp.config import ForgeConfig


class WorkspaceError(ValueError):
    """Raised when a filesystem request violates the workspace contract."""


@dataclass(frozen=True, slots=True)
class FileReadResult:
    content: str
    truncated: bool

    def as_dict(self) -> dict[str, str | bool]:
        return {"content": self.content, "truncated": self.truncated}


class WorkspaceService:
    """The only filesystem boundary exposed to ForgeMCP providers."""

    def __init__(self, config: ForgeConfig) -> None:
        self._config = config
        if not self.root.is_dir():
            raise WorkspaceError(f"Workspace directory does not exist: {self.root}")

    @property
    def root(self) -> Path:
        return self._config.workspace_root

    def resolve_path(self, path: str) -> Path:
        """Resolve a workspace-relative path without allowing path traversal."""
        candidate = (self.root / path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise WorkspaceError("Path must stay inside the configured workspace.") from error
        return candidate

    def list_files(self, path: str = ".", *, recursive: bool = False) -> list[str]:
        """List visible regular files under a workspace directory."""
        directory = self.resolve_path(path)
        if not directory.is_dir():
            raise WorkspaceError(f"Not a directory: {path}")

        iterator = directory.rglob("*") if recursive else directory.iterdir()
        result: list[str] = []
        for item in iterator:
            relative_path = item.relative_to(self.root)
            if item.is_file() and not any(part.startswith(".") for part in relative_path.parts):
                result.append(relative_path.as_posix())
                if len(result) > self._config.max_listed_files:
                    raise WorkspaceError(
                        f"File listing exceeds the limit of {self._config.max_listed_files}."
                    )
        return sorted(result)

    def read_text(self, path: str, *, max_chars: int | None = None) -> FileReadResult:
        """Read a UTF-8 file and cap the returned content."""
        limit = max_chars if max_chars is not None else self._config.max_read_chars
        if not 1 <= limit <= self._config.max_read_chars:
            raise WorkspaceError(
                f"max_chars must be between 1 and {self._config.max_read_chars}."
            )

        file_path = self.resolve_path(path)
        if not file_path.is_file():
            raise WorkspaceError(f"Not a file: {path}")

        content = file_path.read_text(encoding="utf-8")
        return FileReadResult(content=content[:limit], truncated=len(content) > limit)

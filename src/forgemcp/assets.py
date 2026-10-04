"""Small helpers for files shipped inside the ForgeMCP package."""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath

from mcp.types import Icon

PACKAGE = "forgemcp"


def package_file(relative_path: str) -> Traversable:
    """Resolve a safe path relative to the ForgeMCP package."""
    path = PurePosixPath(relative_path.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(
            f"Package path must be relative and cannot contain '..': {relative_path}"
        )

    resource = files(PACKAGE)
    for part in path.parts:
        resource = resource.joinpath(part)
    return resource


@dataclass(frozen=True)
class Widget:
    """A generated single-file MCP App stored relative to the package root."""

    path: str

    @property
    def uri(self) -> str:
        """Return the stable UI resource URI derived from the packaged filename."""
        return f"ui://forgemcp/{PurePosixPath(self.path).name}"

    @property
    def content(self) -> str:
        """Read the packaged widget as UTF-8 HTML."""
        return package_file(self.path).read_text(encoding="utf-8")


@dataclass(frozen=True)
class IconFile:
    """An icon source file converted to portable MCP icon metadata."""

    path: str
    sizes: tuple[str, ...] = ("any",)

    @property
    def icon(self) -> Icon:
        """Encode the packaged icon as portable MCP metadata with its MIME type and sizes."""
        resource = package_file(self.path)
        mime_type = mimetypes.guess_type(self.path)[0] or "application/octet-stream"
        encoded = base64.b64encode(resource.read_bytes()).decode("ascii")
        return Icon(
            src=f"data:{mime_type};base64,{encoded}",
            mime_type=mime_type,
            sizes=list(self.sizes),
        )

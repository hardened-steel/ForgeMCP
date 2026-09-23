"""Portable project/storage paths shared by feature modules."""

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Literal

from pydantic import ConfigDict, RootModel, model_validator


class WorkspacePath(RootModel[str]):
    """A JSON string such as project/src/main.cpp or storage/build/debug."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def validate_path(self) -> WorkspacePath:
        area, separator, relative = self.root.partition("/")
        if area not in ("project", "storage") or not separator:
            raise ValueError("Workspace path must start with project/ or storage/.")
        if "\\" in relative or "\0" in relative:
            raise ValueError("Workspace paths use '/' and cannot contain NUL.")
        windows = PureWindowsPath(relative)
        if windows.drive or windows.root or any(
            part in ("", ".", "..") for part in relative.split("/") if relative
        ):
            raise ValueError("Workspace path must contain a canonical relative path.")
        return self

    @property
    def area(self) -> Literal["project", "storage"]:
        return "project" if self.root.startswith("project/") else "storage"

    @property
    def relative(self) -> str:
        return self.root.partition("/")[2] or "."

    def __str__(self) -> str:
        return self.root

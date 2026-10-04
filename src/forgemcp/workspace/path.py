"""Qualified managed paths and explicit external locations shared by modules."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, cast

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema


class WorkspacePath(str):
    """A project/storage path, or root/ followed by an absolute native path."""

    def __new__(cls, value: str) -> WorkspacePath:
        instance = super().__new__(cls, value)
        instance.validate_path()
        return instance

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: object,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(),
            serialization=core_schema.to_string_ser_schema(),
        )

    def validate_path(self) -> WorkspacePath:
        area, separator, relative = self.partition("/")
        if area not in ("project", "storage", "root") or not separator:
            raise ValueError("Workspace path must start with project/, storage/, or root/.")
        if "\\" in relative or "\0" in relative:
            raise ValueError("Workspace paths use '/' and cannot contain NUL.")
        windows = PureWindowsPath(relative)
        if area == "root":
            native = windows if windows.drive else PurePosixPath(relative)
            if (
                not native.is_absolute()
                or native.as_posix() != relative
                or any(part in (".", "..") for part in relative.split("/"))
                or relative.startswith(("//?/", "//./"))
            ):
                raise ValueError("root/ must be followed by a canonical absolute path.")
            return self
        if windows.drive or windows.root or any(
            part in ("", ".", "..") for part in relative.split("/") if relative
        ):
            raise ValueError("Workspace path must contain a canonical relative path.")
        return self

    @property
    def area(self) -> Literal["project", "storage", "root"]:
        return cast(Literal["project", "storage", "root"], self.partition("/")[0])

    @property
    def relative(self) -> str:
        # The root/ suffix stays absolute and is rejected by managed-root APIs.
        return self.partition("/")[2] or "."

    @property
    def absolute(self) -> Path:
        if self.area != "root":
            raise ValueError("Only root/ paths contain a native absolute path.")
        path = Path(self.relative)
        if not path.is_absolute():
            raise ValueError("The external path is not absolute on this host.")
        return path

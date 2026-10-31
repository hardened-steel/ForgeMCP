"""Qualified managed paths and explicit external locations shared by modules."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, cast

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from forgemcp.diagnostics import display_value


class WorkspacePath(str):
    """A canonical managed path accepting project-relative input, or an explicit root/ location."""

    def __new__(cls, value: str) -> WorkspacePath:
        """Normalize project-relative input and one managed trailing slash, then validate syntax."""
        if value.startswith("./"):
            value = "project/" + value[2:]
        elif value and not value.startswith(("project/", "storage/", "root/")):
            value = "project/" + value
        if (
            value.startswith(("project/", "storage/"))
            and value not in ("project/", "storage/")
            and value.endswith("/")
            and not value.endswith("//")
        ):
            value = value[:-1]
        instance = super().__new__(cls, value)
        instance.validate_path()
        return instance

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: object,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Validate qualified paths as strings and serialize them without schema indirection."""
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(),
            serialization=core_schema.to_string_ser_schema(),
        )

    def validate_path(self) -> WorkspacePath:
        """Reject unknown areas, ambiguous separators, and paths escaping their declared root."""
        area, separator, relative = self.partition("/")
        if area not in ("project", "storage", "root") or not separator:
            raise ValueError(
                f"Invalid workspace path={display_value(self)}: use a relative path such as "
                "'src/main.cpp', or a project/, storage/, or root/ prefix."
            )
        if "\\" in relative:
            raise ValueError(
                f"Invalid workspace path={display_value(self)}: use '/' separators, "
                "for example 'project/src/main.cpp'."
            )
        if "\0" in relative:
            raise ValueError(
                f"Invalid workspace path={display_value(self)}: NUL characters are forbidden; "
                "remove them from the path."
            )
        windows = PureWindowsPath(relative)
        if area == "root":
            native = windows if windows.drive else PurePosixPath(relative)
            if (
                not native.is_absolute()
                or native.as_posix() != relative
                or any(part in (".", "..") for part in relative.split("/"))
                or relative.startswith(("//?/", "//./"))
            ):
                raise ValueError(
                    f"Invalid workspace path={display_value(self)}: root/ must be followed "
                    "by a canonical absolute path without '.' or '..' components, repeated "
                    "separators, or a trailing slash; for example 'root/C:/SDK/header.h' "
                    "on Windows or 'root//usr/include/header.h' on Unix."
                )
            return self
        if windows.drive or windows.root or any(
            part in ("", ".", "..") for part in relative.split("/") if relative
        ):
            raise ValueError(
                f"Invalid workspace path={display_value(self)}: managed paths must be relative "
                "and cannot contain '..', remaining '.' components, repeated separators, "
                "or a drive letter. Use a path such as 'project/src/main.cpp'."
            )
        return self

    @property
    def area(self) -> Literal["project", "storage", "root"]:
        """Return the project, storage, or external root area prefix."""
        return cast(Literal["project", "storage", "root"], self.partition("/")[0])

    @property
    def relative(self) -> str:
        # The root/ suffix stays absolute and is rejected by managed-root APIs.
        """Return the path suffix, using a dot for an empty root-relative path."""
        return self.partition("/")[2] or "."

    @property
    def absolute(self) -> Path:
        """Return a native absolute external path and reject other areas or host-relative values."""
        if self.area != "root":
            raise ValueError(
                f"Invalid path={display_value(self)}: only root/ paths contain a native "
                "absolute path; use managed-path resolution for project/ and storage/."
            )
        path = Path(self.relative)
        if not path.is_absolute():
            raise ValueError(
                f"Invalid path={display_value(self)}: the external path is not absolute "
                "on this host; use root/ followed by an absolute path for the server's OS."
            )
        return path

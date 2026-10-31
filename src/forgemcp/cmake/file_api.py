"""Read bounded CMake-owned replies and fingerprint managed configuration inputs."""

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from forgemcp.diagnostics import display_value, os_error_reason
from forgemcp.workspace.path import WorkspacePath

from .errors import CMakeError

MAX_REPLY_BYTES = 16 * 1024 * 1024
MAX_INPUTS = 100_000


@dataclass
class WorkspaceChange:
    """Known input hashes and path kinds needed to verify one controlled mutation."""

    inputs: dict[WorkspacePath, str | None]
    kinds: dict[WorkspacePath, str]


@dataclass
class ConfigurationInputs:
    """One generation's input hashes and conservative structural invalidation policy."""

    checksums: dict[WorkspacePath, str | None] = field(default_factory=dict)
    structural: bool = False
    current: bool = False

    def verify(self, project: Path, storage: Path) -> None:
        """Withdraw a confirmed baseline when an input changes or cannot safely be read."""
        if not self.current:
            return
        try:
            self.current = self.checksums == input_checksums(
                self.checksums,
                project,
                storage,
            )
        except (CMakeError, OSError):
            self.current = False


def managed_path(path: Path, project: Path, storage: Path) -> WorkspacePath | None:
    """Qualify a path only when both its lexical and resolved targets stay managed."""
    for area, base in (("storage", storage), ("project", project)):
        if path.is_relative_to(base) and path.resolve().is_relative_to(base):
            relative = path.relative_to(base).as_posix()
            return WorkspacePath(f"{area}/{'' if relative == '.' else relative}")
    return None


def checksum(path: Path) -> str | None:
    """Hash a file incrementally, representing a missing input without retaining contents."""
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except FileNotFoundError:
        return None


def read_reply(path: Path, project: Path, storage: Path) -> dict:
    """Read a size-bounded managed JSON reply without following references outside the roots."""
    reference = managed_path(path, project, storage)
    if reference is None:
        raise CMakeError("CMake File API reply is outside managed roots.")
    with path.open("rb") as stream:
        data = stream.read(MAX_REPLY_BYTES + 1)
    if len(data) > MAX_REPLY_BYTES:
        raise CMakeError(
            f"CMake File API reply {display_value(str(reference))} exceeds "
            f"the {MAX_REPLY_BYTES}-byte size limit."
        )
    try:
        value = json.loads(data)
    except json.JSONDecodeError as error:
        raise CMakeError(
            f"CMake File API reply {display_value(str(reference))} has invalid JSON "
            f"at line {error.lineno}, column {error.colno}. Run cmake_configure "
            "to regenerate metadata."
        ) from error
    if not isinstance(value, dict):
        raise CMakeError(
            f"CMake File API reply {display_value(str(reference))} must contain a JSON object."
        )
    return value


def generation(directory: Path) -> str | None:
    """Identify the latest successful index, rejecting a newer CMake 4.1 error index."""
    try:
        reply = directory / ".cmake/api/v1/reply"
        current = reply
        while current != directory:
            if current.is_symlink() or current.is_junction():
                raise CMakeError(
                    "CMake File API metadata path .cmake/api/v1/reply traverses a link "
                    "or junction. Choose a build tree without metadata links."
                )
            current = current.parent
        if not reply.exists():
            return None
        indexes = []
        errors = []
        for count, path in enumerate(reply.iterdir()):
            if count >= MAX_INPUTS:
                raise CMakeError(
                    "CMake File API directory .cmake/api/v1/reply exceeds the "
                    f"{MAX_INPUTS}-entry scan limit. Inspect old build metadata "
                    "before configuring again."
                )
            if path.name.startswith("index-") and path.suffix == ".json":
                indexes.append(path.name)
            elif path.name.startswith("error-") and path.suffix == ".json":
                errors.append(path.name)
        latest = max(indexes, default=None)
        if latest is None or (errors and max(errors)[6:] >= latest[6:]):
            return None
        return latest
    except OSError as error:
        raise CMakeError(
            f"Cannot inspect CMake File API generation in .cmake/api/v1/reply: "
            f"{os_error_reason(error)}."
        ) from error


def read_inputs(
    directory: Path,
    index: str,
    project: Path,
    storage: Path,
) -> ConfigurationInputs:
    """Follow cmakeFiles-v1 from this generation and hash only project/storage inputs."""
    try:
        reply = directory / ".cmake/api/v1/reply"
        reference = read_reply(reply / index, project, storage)["reply"][
            "client-forgemcp"
        ]["cmakeFiles-v1"]
        filename = reference["jsonFile"]
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise CMakeError(
                "CMake File API cmakeFiles-v1.jsonFile must be one relative filename "
                "inside .cmake/api/v1/reply. Run cmake_configure to regenerate metadata."
            )
        files = read_reply(reply / filename, project, storage)
        if files["kind"] != "cmakeFiles" or files["version"]["major"] != 1:
            raise CMakeError(
                "CMake configuration inputs reply requires kind='cmakeFiles' and "
                "version.major=1. Regenerate supported File API metadata with cmake_configure."
            )
        if (
            Path(files["paths"]["source"]).resolve() != project
            or Path(files["paths"]["build"]).resolve() != directory.resolve()
        ):
            raise CMakeError(
                "CMake File API reply belongs to a different source/build tree."
            )
        inputs = files["inputs"]
        if not isinstance(inputs, list) or not inputs or len(inputs) > MAX_INPUTS:
            raise CMakeError(
                f"CMake File API inputs must be a nonempty array with at most {MAX_INPUTS} "
                "entries. The compilation context cannot be confirmed from this reply."
            )
        checksums = {}
        for item in inputs:
            native = Path(item["path"])
            if not native.is_absolute():
                native = project / native
            native = Path(os.path.abspath(native))
            qualified = managed_path(native, project, storage)
            if qualified is not None:
                checksums[qualified] = checksum(native)
        for filename in ("CMakePresets.json", "CMakeUserPresets.json"):
            native = project / filename
            qualified = managed_path(native, project, storage)
            if qualified is not None:
                checksums[qualified] = checksum(native)
        # Older replies cannot tell us whether CONFIGURE_DEPENDS globs exist.
        return ConfigurationInputs(
            checksums,
            structural=files["version"].get("minor", 0) < 1
            or bool(files.get("globsDependent")),
            current=True,
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        reason = (
            os_error_reason(error)
            if isinstance(error, OSError)
            else f"missing metadata field {display_value(error.args[0])}"
            if isinstance(error, KeyError)
            else "metadata has an invalid value or shape"
        )
        raise CMakeError(
            f"Cannot confirm CMake configuration inputs from index "
            f"{display_value(index)}: {reason}. Resolve the metadata problem and run "
            "cmake_configure; clangd contexts remain unavailable until confirmation."
        ) from error


def input_path(reference: WorkspacePath, project: Path, storage: Path) -> Path:
    """Resolve an input within its selected managed root without accessing external targets."""
    if reference.area == "root":
        raise CMakeError("Configuration inputs must be managed paths.")
    base = project if reference.area == "project" else storage
    path = base / reference.relative
    if not path.resolve().is_relative_to(base):
        raise CMakeError(f"Configuration input escaped its root: {reference}.")
    return path


def input_checksums(
    paths: Iterable[WorkspacePath],
    project: Path,
    storage: Path,
) -> dict[WorkspacePath, str | None]:
    """Capture only hashes for the supplied managed input names."""
    return {path: checksum(input_path(path, project, storage)) for path in paths}


def path_kind(path: Path) -> str:
    """Capture a mutation target's existence and kind without traversing its subtree."""
    if path.is_dir():
        return "directory"
    return "file" if path.exists() else "missing"


def prepare_queries(query: Path) -> None:
    """Create owned File API queries in a validated build-tree directory."""
    try:
        query.mkdir(parents=True, exist_ok=True)
        for name in ("codemodel-v2", "cache-v2", "toolchains-v1", "cmakeFiles-v1"):
            target = query / name
            if target.is_symlink() or target.is_junction():
                raise CMakeError("CMake File API queries cannot be links.")
            target.write_text("", encoding="utf-8")
    except OSError as error:
        raise CMakeError(
            "Cannot prepare CMake File API queries in .cmake/api/v1/query/client-forgemcp: "
            f"{os_error_reason(error)}."
        ) from error


def compilation_database(
    directory: WorkspacePath,
    native: Path,
) -> WorkspacePath | None:
    """Return the qualified compilation database name when that build tree contains one."""
    path = native / "compile_commands.json"
    return (
        WorkspacePath(f"{directory}/compile_commands.json") if path.is_file() else None
    )


def read_generation(
    directory: Path,
    previous: str | None,
    project: Path,
    storage: Path,
) -> ConfigurationInputs | None:
    """Read inputs only when a command produced a new successful File API index."""
    try:
        latest = generation(directory)
        if latest is None or latest == previous:
            return None
        return read_inputs(directory, latest, project, storage)
    except OSError as error:
        raise CMakeError(
            f"Cannot inspect CMake generation: {os_error_reason(error)}. "
            "Resolve build-metadata access and run cmake_configure."
        ) from error

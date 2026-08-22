"""Expected, safe errors for the transport-neutral CMake integration."""

from __future__ import annotations

from forgemcp.core.errors import ForgeMCPError


class CMakeError(ForgeMCPError):
    """Base class for CMake and CTest operations that a client may understand."""

    code = "cmake_error"


class CMakeRequestError(CMakeError):
    """A tool request does not meet the published CMake input contract."""

    code = "cmake_request_error"


class CMakeToolUnavailableError(CMakeError):
    """The required CMake or CTest executable cannot be started."""

    code = "cmake_tool_unavailable"


class CMakeVersionError(CMakeError):
    """A discovered CMake installation is below the supported version floor."""

    code = "cmake_version_unsupported"


class CMakePresetError(CMakeError):
    """A CMake preset document is absent when required or structurally invalid."""

    code = "cmake_preset_error"


class CMakeFileApiError(CMakeError):
    """CMake File API metadata is absent, stale, invalid, or outside the workspace."""

    code = "cmake_file_api_error"


class CompilationDatabaseRequirementError(CMakeError):
    """The configured required compilation-database policy was not met."""

    code = "compile_commands_required"


class CTestJsonError(CMakeError):
    """CTest's documented JSON listing response is invalid or unsupported."""

    code = "ctest_json_error"


class CMakeKitError(CMakeError):
    """A requested cached ForgeMCP kit cannot be used safely."""

    code = "cmake_kit_error"


class CMakeKitSelectionConflictError(CMakeKitError):
    """A selection compare-and-swap generation is stale."""

    code = "kit_selection_conflict"


class CMakePresetKitConflictError(CMakeKitError):
    """A CMake Preset workflow cannot be silently mixed with a ForgeMCP kit."""

    code = "preset_kit_conflict"


class CMakeBuildTreeIncompatibleError(CMakeError):
    """An existing CMake cache cannot be safely reconfigured with this selection."""

    code = "build_tree_incompatible"

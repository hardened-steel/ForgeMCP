"""Expected, output-free errors raised by the process runtime."""

from __future__ import annotations

from forgemcp.core.errors import ForgeMCPError


class ProcessError(ForgeMCPError):
    """Base class for safe process-runtime operation errors."""

    code = "process_error"


class ProcessArgumentError(ProcessError):
    """The requested command is not a valid argv sequence."""

    code = "process_argument_error"


class ProcessExecutableError(ProcessError):
    """The requested executable is unavailable or denied by policy."""

    code = "process_executable_error"


class ProcessWorkingDirectoryError(ProcessError):
    """The requested working directory is not an allowed workspace directory."""

    code = "process_working_directory_error"


class ProcessEnvironmentError(ProcessError):
    """The requested environment inheritance or override is invalid or denied."""

    code = "process_environment_error"


class ProcessPolicyError(ProcessError):
    """A requested runtime limit is outside the configured policy."""

    code = "process_policy_error"


class ProcessRuntimeClosedError(ProcessError):
    """A caller tried to launch a process after runtime shutdown began."""

    code = "process_runtime_closed"

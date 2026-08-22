"""ForgeMCP: an extensible MCP server for C++ development workflows."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("forgemcp")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.1.0"

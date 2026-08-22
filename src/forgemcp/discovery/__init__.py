"""ForgeMCP's bounded discovery surface and server-wide guidance."""

from forgemcp.discovery.instructions import (
    MAX_SERVER_INSTRUCTIONS_BYTES,
    SERVER_INSTRUCTIONS,
    validate_server_instructions,
)
from forgemcp.discovery.plugin import (
    ABOUT_URI,
    LOGS_TEMPLATE_URI,
    LOGS_URI,
    PROMPT_NAMES,
    DiscoveryPlugin,
)

__all__ = [
    "ABOUT_URI",
    "DiscoveryPlugin",
    "LOGS_TEMPLATE_URI",
    "LOGS_URI",
    "MAX_SERVER_INSTRUCTIONS_BYTES",
    "PROMPT_NAMES",
    "SERVER_INSTRUCTIONS",
    "validate_server_instructions",
]

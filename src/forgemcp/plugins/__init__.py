"""Public feature-plugin API for ForgeMCP extensions."""

from forgemcp.plugins.contract import (
    PLUGIN_API_VERSION,
    ForgePlugin,
    PluginContext,
    PluginLogger,
    PluginMetadata,
    PluginServiceAccess,
)
from forgemcp.plugins.errors import (
    DuplicateCapabilityError,
    DuplicatePluginIdError,
    DuplicateToolNameError,
    MissingPluginDependencyError,
    MissingRequiredServiceError,
    PluginApiVersionError,
    PluginDependencyCycleError,
    PluginDiscoveryError,
    PluginError,
    PluginManagerClosedError,
    PluginRegistrationError,
    PluginStartError,
    ToolNamespaceError,
)
from forgemcp.plugins.manager import PluginManager, PluginState, PluginStatus
from forgemcp.plugins.tools import (
    PluginToolRegistry,
    RegisteredToolContribution,
    ToolHandler,
    ToolContribution,
    ToolRegistry,
)

__all__ = [
    "PLUGIN_API_VERSION",
    "DuplicateCapabilityError",
    "DuplicatePluginIdError",
    "DuplicateToolNameError",
    "ForgePlugin",
    "MissingPluginDependencyError",
    "MissingRequiredServiceError",
    "PluginApiVersionError",
    "PluginContext",
    "PluginDependencyCycleError",
    "PluginDiscoveryError",
    "PluginError",
    "PluginManager",
    "PluginManagerClosedError",
    "PluginMetadata",
    "PluginLogger",
    "PluginRegistrationError",
    "PluginServiceAccess",
    "PluginStartError",
    "PluginState",
    "PluginStatus",
    "PluginToolRegistry",
    "RegisteredToolContribution",
    "ToolContribution",
    "ToolHandler",
    "ToolNamespaceError",
    "ToolRegistry",
]

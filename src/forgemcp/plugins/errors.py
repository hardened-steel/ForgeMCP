"""Errors raised while composing and running ForgeMCP feature plugins."""

from __future__ import annotations


class PluginError(Exception):
    """Base class for expected plugin-system failures."""


class PluginRegistrationError(PluginError):
    """A plugin cannot be added to the current manager."""


class DuplicatePluginIdError(PluginRegistrationError):
    """Two plugins use the same stable identifier."""


class DuplicateCapabilityError(PluginRegistrationError):
    """Two plugins claim the same capability."""


class PluginApiVersionError(PluginRegistrationError):
    """A plugin targets a different ForgeMCP plugin API version."""


class MissingPluginDependencyError(PluginError):
    """A declared plugin dependency was not registered."""


class MissingRequiredServiceError(PluginError):
    """A declared Core service is not available to a plugin."""


class PluginDependencyCycleError(PluginError):
    """Plugin dependencies contain a directed cycle."""


class PluginStartError(PluginError):
    """A plugin raised while it was starting."""


class PluginDiscoveryError(PluginError):
    """An explicitly allowed external entry point is invalid or cannot load."""


class PluginManagerClosedError(PluginError):
    """A terminal plugin manager was asked to start or register work."""


class DuplicateToolNameError(PluginRegistrationError):
    """Two registered tool contributions use the same qualified tool name."""



class ToolNamespaceError(PluginRegistrationError):
    """A tool contribution has an invalid local tool namespace."""


class DuplicateResourceUriError(PluginRegistrationError):
    """Two registered static resources use the same public URI."""


class DuplicateResourceTemplateError(PluginRegistrationError):
    """Two registered resource templates use the same URI template."""


class DuplicatePromptNameError(PluginRegistrationError):
    """Two registered prompts use the same public name."""


class DuplicateCompletionProviderError(PluginRegistrationError):
    """Two completion providers claim the same reference argument."""


class ContributionLimitError(PluginRegistrationError):
    """An application exceeded a bounded contribution registry capacity."""


class DiscoverySurfaceError(PluginError):
    """A discovery-surface request failed with a fixed safe category."""


class ResourceReadError(DiscoverySurfaceError):
    """A resource contribution could not produce a bounded response."""


class PromptRequestError(DiscoverySurfaceError):
    """Prompt arguments did not satisfy the published contribution contract."""


class CompletionRequestError(DiscoverySurfaceError):
    """A completion request did not match a registered bounded provider."""

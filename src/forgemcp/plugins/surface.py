"""Transport-neutral resources, prompts, and completion contributions.

The MCP SDK adapter lives in :mod:`forgemcp.server`.  This module deliberately
contains no MCP, FastMCP, request-context, session, or transport types.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

from forgemcp.plugins.errors import (
    CompletionRequestError,
    ContributionLimitError,
    DuplicateCompletionProviderError,
    DuplicatePromptNameError,
    DuplicateResourceTemplateError,
    DuplicateResourceUriError,
    PromptRequestError,
    ResourceReadError,
)


MAX_RESOURCE_CONTRIBUTIONS = 128
MAX_RESOURCE_TEMPLATE_CONTRIBUTIONS = 128
MAX_PROMPT_CONTRIBUTIONS = 128
MAX_COMPLETION_PROVIDERS = 256
MAX_RESOURCE_CONTENT_BYTES = 256 * 1024
MAX_CONCURRENT_RESOURCE_READS = 8
MAX_PROMPT_MESSAGES = 8
MAX_PROMPT_MESSAGE_BYTES = 8 * 1024
MAX_PROMPT_TOTAL_BYTES = 24 * 1024
MAX_COMPLETION_VALUES = 100
MAX_COMPLETION_CANDIDATES = 1_000
MAX_COMPLETION_CONTEXT_ARGUMENTS = 16
MAX_COMPLETION_VALUE_CHARACTERS = 512
RESOURCE_READ_TIMEOUT_SECONDS = 2.0
COMPLETION_TIMEOUT_SECONDS = 0.75

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TEMPLATE_ARGUMENT = re.compile(r"{([a-z][a-z0-9_]{0,63})}")
_MIME_TYPE = re.compile(r"^[A-Za-z0-9]+/[A-Za-z0-9.+-]+$")


def _bounded_text(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded non-empty string.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} must not contain control characters.")
    return value


def _normalise_arguments(values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValueError("Contribution arguments must be a collection of names.")
    try:
        arguments = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError("Contribution arguments must be a collection of names.") from error
    if len(arguments) != len(set(arguments)) or any(
        not isinstance(argument, str) or not _NAME.fullmatch(argument)
        for argument in arguments
    ):
        raise ValueError("Contribution arguments must be unique lower-case identifiers.")
    return arguments


ResourceHandler = Callable[[], object | Awaitable[object]]
ResourceTemplateHandler = Callable[[Mapping[str, str]], object | Awaitable[object]]
PromptHandler = Callable[[Mapping[str, str]], object | Awaitable[object]]
CompletionProvider = Callable[["CompletionRequest"], Iterable[str] | Awaitable[Iterable[str]]]


@dataclass(frozen=True, slots=True)
class ResourceContribution:
    """One static model-facing resource independent of an MCP implementation."""

    uri: str
    name: str
    description: str
    handler: ResourceHandler = field(repr=False, compare=False)
    mime_type: str = "application/json"

    def __post_init__(self) -> None:
        _bounded_text(self.uri, label="Resource URI", maximum=512)
        if "{" in self.uri or "}" in self.uri or "://" not in self.uri:
            raise ValueError("Static resource URIs must be absolute and contain no templates.")
        if not _NAME.fullmatch(self.name):
            raise ValueError("Resource names must be lower-case identifiers.")
        _bounded_text(self.description, label="Resource description", maximum=1024)
        if not _MIME_TYPE.fullmatch(self.mime_type):
            raise ValueError("Resource MIME type is invalid.")
        if not callable(self.handler):
            raise TypeError("Resource handler must be callable.")


@dataclass(frozen=True, slots=True)
class ResourceTemplateContribution:
    """One URI-template resource with a strict mapping-based handler."""

    uri_template: str
    name: str
    description: str
    arguments: tuple[str, ...]
    handler: ResourceTemplateHandler = field(repr=False, compare=False)
    mime_type: str = "application/json"

    def __post_init__(self) -> None:
        template = _bounded_text(self.uri_template, label="Resource URI template", maximum=512)
        arguments = _normalise_arguments(self.arguments)
        placeholders = tuple(_TEMPLATE_ARGUMENT.findall(template))
        remainder = _TEMPLATE_ARGUMENT.sub("", template)
        if "://" not in template or "{" in remainder or "}" in remainder:
            raise ValueError("Resource URI template syntax is invalid.")
        if placeholders != arguments:
            raise ValueError("Resource template arguments must exactly match URI placeholders in order.")
        if not _NAME.fullmatch(self.name):
            raise ValueError("Resource template names must be lower-case identifiers.")
        _bounded_text(self.description, label="Resource template description", maximum=1024)
        if not _MIME_TYPE.fullmatch(self.mime_type):
            raise ValueError("Resource MIME type is invalid.")
        if not callable(self.handler):
            raise TypeError("Resource template handler must be callable.")
        object.__setattr__(self, "arguments", arguments)


@dataclass(frozen=True, slots=True)
class PromptArgument:
    """One bounded untrusted identifier accepted by a reusable prompt."""

    name: str
    description: str
    required: bool = False
    max_length: int = 256

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError("Prompt argument names must be lower-case identifiers.")
        _bounded_text(self.description, label="Prompt argument description", maximum=512)
        if not isinstance(self.required, bool):
            raise TypeError("Prompt argument required flag must be boolean.")
        if not isinstance(self.max_length, int) or isinstance(self.max_length, bool) or not 1 <= self.max_length <= 4096:
            raise ValueError("Prompt argument max_length must be from 1 through 4096.")


@dataclass(frozen=True, slots=True)
class PromptMessage:
    """A transport-neutral prompt message containing fixed authored text or JSON data."""

    role: Literal["user", "assistant"]
    text: str

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant"}:
            raise ValueError("Prompt message role is invalid.")
        _bounded_text(self.text, label="Prompt message text", maximum=MAX_PROMPT_MESSAGE_BYTES)


@dataclass(frozen=True, slots=True)
class PromptContribution:
    """One reusable prompt whose handler only returns messages."""

    name: str
    description: str
    arguments: tuple[PromptArgument, ...]
    handler: PromptHandler = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError("Prompt names must be lower-case identifiers.")
        _bounded_text(self.description, label="Prompt description", maximum=1024)
        arguments = tuple(self.arguments)
        if len(arguments) > 16 or len({argument.name for argument in arguments}) != len(arguments):
            raise ValueError("Prompt arguments must be unique and bounded.")
        if any(not isinstance(argument, PromptArgument) for argument in arguments):
            raise TypeError("Prompt arguments must be PromptArgument values.")
        if not callable(self.handler):
            raise TypeError("Prompt handler must be callable.")
        object.__setattr__(self, "arguments", arguments)


class CompletionReferenceKind(StrEnum):
    PROMPT = "prompt"
    RESOURCE_TEMPLATE = "resource_template"


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """One bounded completion lookup with previously supplied string arguments."""

    reference_kind: CompletionReferenceKind
    reference: str
    argument: str
    value: str
    context: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        context = MappingProxyType(dict(self.context))
        object.__setattr__(self, "context", context)


@dataclass(frozen=True, slots=True)
class CompletionContribution:
    """A provider for one prompt or resource-template argument."""

    reference_kind: CompletionReferenceKind
    reference: str
    argument: str
    provider: CompletionProvider = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.reference_kind, CompletionReferenceKind):
            raise TypeError("Completion reference kind is invalid.")
        _bounded_text(self.reference, label="Completion reference", maximum=512)
        if not _NAME.fullmatch(self.argument):
            raise ValueError("Completion argument name is invalid.")
        if not callable(self.provider):
            raise TypeError("Completion provider must be callable.")


@dataclass(frozen=True, slots=True)
class CompletionResult:
    values: tuple[str, ...]
    total: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class _OwnedResource:
    plugin_id: str
    contribution: ResourceContribution


@dataclass(frozen=True, slots=True)
class _OwnedTemplate:
    plugin_id: str
    contribution: ResourceTemplateContribution


@dataclass(frozen=True, slots=True)
class _OwnedPrompt:
    plugin_id: str
    contribution: PromptContribution


@dataclass(frozen=True, slots=True)
class _OwnedCompletion:
    plugin_id: str
    contribution: CompletionContribution


class DiscoverySurfaceRegistry:
    """Application-owned bounded registry for every non-tool MCP contribution."""

    def __init__(self) -> None:
        self._resources: dict[str, _OwnedResource] = {}
        self._templates: dict[str, _OwnedTemplate] = {}
        self._prompts: dict[str, _OwnedPrompt] = {}
        self._completions: dict[tuple[CompletionReferenceKind, str, str], _OwnedCompletion] = {}
        self._resource_slots = asyncio.Semaphore(MAX_CONCURRENT_RESOURCE_READS)
        self._retired: set[asyncio.Task[object]] = set()
        self._closed = False

    def register_resource(self, plugin_id: str, contribution: ResourceContribution) -> None:
        self._ensure_open()
        if len(self._resources) >= MAX_RESOURCE_CONTRIBUTIONS:
            raise ContributionLimitError("Static resource contribution limit exceeded.")
        if contribution.uri in self._resources:
            raise DuplicateResourceUriError(f"Resource URI already registered: {contribution.uri}")
        self._resources[contribution.uri] = _OwnedResource(plugin_id, contribution)

    def register_template(self, plugin_id: str, contribution: ResourceTemplateContribution) -> None:
        self._ensure_open()
        if len(self._templates) >= MAX_RESOURCE_TEMPLATE_CONTRIBUTIONS:
            raise ContributionLimitError("Resource template contribution limit exceeded.")
        if contribution.uri_template in self._templates:
            raise DuplicateResourceTemplateError(
                f"Resource template already registered: {contribution.uri_template}"
            )
        self._templates[contribution.uri_template] = _OwnedTemplate(plugin_id, contribution)

    def register_prompt(self, plugin_id: str, contribution: PromptContribution) -> None:
        self._ensure_open()
        if len(self._prompts) >= MAX_PROMPT_CONTRIBUTIONS:
            raise ContributionLimitError("Prompt contribution limit exceeded.")
        if contribution.name in self._prompts:
            raise DuplicatePromptNameError(f"Prompt already registered: {contribution.name}")
        self._prompts[contribution.name] = _OwnedPrompt(plugin_id, contribution)

    def register_completion(self, plugin_id: str, contribution: CompletionContribution) -> None:
        self._ensure_open()
        if len(self._completions) >= MAX_COMPLETION_PROVIDERS:
            raise ContributionLimitError("Completion provider limit exceeded.")
        key = (contribution.reference_kind, contribution.reference, contribution.argument)
        if key in self._completions:
            raise DuplicateCompletionProviderError(
                f"Completion provider already registered: {contribution.reference}:{contribution.argument}"
            )
        self._completions[key] = _OwnedCompletion(plugin_id, contribution)

    def unregister_plugin(self, plugin_id: str) -> None:
        for registry in (self._resources, self._templates, self._prompts, self._completions):
            for key in tuple(registry):
                if registry[key].plugin_id == plugin_id:
                    del registry[key]

    def resources(self) -> tuple[ResourceContribution, ...]:
        return tuple(self._resources[key].contribution for key in sorted(self._resources))

    def templates(self) -> tuple[ResourceTemplateContribution, ...]:
        return tuple(self._templates[key].contribution for key in sorted(self._templates))

    def prompts(self) -> tuple[PromptContribution, ...]:
        return tuple(self._prompts[key].contribution for key in sorted(self._prompts))

    async def read_resource(self, uri: str) -> str:
        try:
            owned = self._resources[uri]
        except KeyError as error:
            raise ResourceReadError("Unknown resource contribution.") from error
        return await self._read(owned.contribution.handler)

    async def read_template(self, uri_template: str, arguments: Mapping[str, str]) -> str:
        try:
            contribution = self._templates[uri_template].contribution
        except KeyError as error:
            raise ResourceReadError("Unknown resource template contribution.") from error
        if tuple(arguments) != contribution.arguments:
            raise ResourceReadError("Resource template arguments are invalid.")
        validated = {
            name: _bounded_text(arguments[name], label="Resource template argument", maximum=512)
            for name in contribution.arguments
        }
        return await self._read(lambda: contribution.handler(MappingProxyType(validated)))

    async def _read(self, handler: ResourceHandler) -> str:
        async with self._resource_slots:
            value = await self._invoke_bounded(
                handler, timeout=RESOURCE_READ_TIMEOUT_SECONDS, error_type=ResourceReadError
            )
        try:
            if isinstance(value, str):
                encoded = value.encode("utf-8")
                content = value
            else:
                content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                encoded = content.encode("utf-8")
        except (TypeError, UnicodeEncodeError, ValueError) as error:
            raise ResourceReadError("Resource response is not valid bounded UTF-8 JSON.") from error
        if len(encoded) > MAX_RESOURCE_CONTENT_BYTES:
            raise ResourceReadError("Resource response exceeds the configured byte limit.")
        return content

    async def get_prompt(self, name: str, arguments: Mapping[str, object]) -> tuple[PromptMessage, ...]:
        try:
            contribution = self._prompts[name].contribution
        except KeyError as error:
            raise PromptRequestError("Unknown prompt contribution.") from error
        specifications = {argument.name: argument for argument in contribution.arguments}
        unknown = set(arguments).difference(specifications)
        missing = {argument.name for argument in contribution.arguments if argument.required}.difference(arguments)
        if unknown or missing:
            raise PromptRequestError("Prompt arguments do not match the published contract.")
        validated: dict[str, str] = {}
        for argument_name, value in arguments.items():
            specification = specifications[argument_name]
            if not isinstance(value, str):
                raise PromptRequestError("Prompt arguments must be strings.")
            try:
                validated[argument_name] = _bounded_text(
                    value, label="Prompt argument", maximum=specification.max_length
                )
            except ValueError as error:
                raise PromptRequestError("Prompt argument is invalid.") from error
        result = contribution.handler(MappingProxyType(validated))
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, (tuple, list)) or not 1 <= len(result) <= MAX_PROMPT_MESSAGES:
            raise PromptRequestError("Prompt handler returned an invalid message collection.")
        messages = tuple(result)
        if any(not isinstance(message, PromptMessage) for message in messages):
            raise PromptRequestError("Prompt handler returned an invalid message.")
        if sum(len(message.text.encode("utf-8")) for message in messages) > MAX_PROMPT_TOTAL_BYTES:
            raise PromptRequestError("Prompt response exceeds the configured byte limit.")
        return messages

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self._validate_completion_request(request)
        key = (request.reference_kind, request.reference, request.argument)
        try:
            provider = self._completions[key].contribution.provider
        except KeyError as error:
            raise CompletionRequestError("No completion provider is registered for this argument.") from error
        raw = await self._invoke_bounded(
            lambda: provider(request),
            timeout=COMPLETION_TIMEOUT_SECONDS,
            error_type=CompletionRequestError,
        )
        if isinstance(raw, str):
            raise CompletionRequestError("Completion provider returned an invalid collection.")
        try:
            candidates = tuple(itertools.islice(iter(raw), MAX_COMPLETION_CANDIDATES + 1))
        except TypeError as error:
            raise CompletionRequestError("Completion provider returned an invalid collection.") from error
        provider_has_more = len(candidates) > MAX_COMPLETION_CANDIDATES
        safe: set[str] = set()
        for candidate in candidates[:MAX_COMPLETION_CANDIDATES]:
            if (
                isinstance(candidate, str)
                and 0 < len(candidate) <= MAX_COMPLETION_VALUE_CHARACTERS
                and not any(ord(character) < 32 or ord(character) == 127 for character in candidate)
                and candidate.startswith(request.value)
            ):
                safe.add(candidate)
        ordered = tuple(sorted(safe, key=lambda value: (value.casefold(), value)))
        total = len(ordered)
        return CompletionResult(
            values=ordered[:MAX_COMPLETION_VALUES],
            total=total,
            has_more=provider_has_more or total > MAX_COMPLETION_VALUES,
        )

    def _validate_completion_request(self, request: CompletionRequest) -> None:
        if not isinstance(request, CompletionRequest):
            raise CompletionRequestError("Completion request is invalid.")
        try:
            _bounded_text(request.reference, label="Completion reference", maximum=512)
        except ValueError as error:
            raise CompletionRequestError("Completion reference is invalid.") from error
        if not _NAME.fullmatch(request.argument) or len(request.value) > MAX_COMPLETION_VALUE_CHARACTERS:
            raise CompletionRequestError("Completion argument is invalid.")
        if any(ord(character) < 32 or ord(character) == 127 for character in request.value):
            raise CompletionRequestError("Completion prefix is invalid.")
        if len(request.context) > MAX_COMPLETION_CONTEXT_ARGUMENTS:
            raise CompletionRequestError("Completion context is too large.")
        for key, value in request.context.items():
            if (
                not isinstance(key, str)
                or not _NAME.fullmatch(key)
                or not isinstance(value, str)
                or len(value) > MAX_COMPLETION_VALUE_CHARACTERS
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise CompletionRequestError("Completion context is invalid.")

    async def _invoke_bounded(
        self,
        handler: Callable[[], object | Awaitable[object]],
        *,
        timeout: float,
        error_type: type[ResourceReadError] | type[CompletionRequestError],
    ) -> object:
        if self._closed:
            raise error_type("Discovery surface is closed.")
        try:
            result = handler()
        except Exception as error:
            raise error_type("Discovery contribution failed.") from error
        if not inspect.isawaitable(result):
            return result
        if len(self._retired) >= MAX_CONCURRENT_RESOURCE_READS:
            raise error_type("Discovery contribution capacity is unavailable.")
        task = asyncio.create_task(result)
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            self._retire(task)
            raise
        if task not in done:
            self._retire(task)
            raise error_type("Discovery contribution deadline exceeded.")
        try:
            return task.result()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise error_type("Discovery contribution failed.") from error

    def _retire(self, task: asyncio.Task[object]) -> None:
        if not task.done():
            task.cancel()
        self._retired.add(task)

        def consume(completed: asyncio.Task[object]) -> None:
            self._retired.discard(completed)
            try:
                completed.result()
            except (asyncio.CancelledError, Exception):
                return

        task.add_done_callback(consume)

    async def aclose(self) -> None:
        self._closed = True
        for task in tuple(self._retired):
            if not task.done():
                task.cancel()
        self._resources.clear()
        self._templates.clear()
        self._prompts.clear()
        self._completions.clear()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ContributionLimitError("Discovery surface registry is closed.")


class _PluginSurfaceFacade:
    __slots__ = ("_plugin_id", "_registry")

    def __init__(self, plugin_id: str, registry: DiscoverySurfaceRegistry) -> None:
        self._plugin_id = plugin_id
        self._registry = registry


class PluginResourceRegistry(_PluginSurfaceFacade):
    def register(self, contribution: ResourceContribution) -> None:
        self._registry.register_resource(self._plugin_id, contribution)


class PluginResourceTemplateRegistry(_PluginSurfaceFacade):
    def register(self, contribution: ResourceTemplateContribution) -> None:
        self._registry.register_template(self._plugin_id, contribution)


class PluginPromptRegistry(_PluginSurfaceFacade):
    def register(self, contribution: PromptContribution) -> None:
        self._registry.register_prompt(self._plugin_id, contribution)


class PluginCompletionRegistry(_PluginSurfaceFacade):
    def register(self, contribution: CompletionContribution) -> None:
        self._registry.register_completion(self._plugin_id, contribution)

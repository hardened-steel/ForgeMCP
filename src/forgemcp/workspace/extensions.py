"""Provider contracts for immutable workspace result resources."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, JsonValue

from .path import WorkspacePath


@dataclass(frozen=True)
class TextChange:
    path: WorkspacePath
    before: str
    after: str


@dataclass(frozen=True)
class ExtensionContext:
    tool_name: str
    result: BaseModel
    paths: tuple[WorkspacePath, ...]
    texts: Mapping[WorkspacePath, str] = field(default_factory=dict)
    change: TextChange | None = None


@dataclass(frozen=True)
class ExtensionResource:
    name: str
    mime_type: str
    text: str


@dataclass(frozen=True)
class ExtensionOutput:
    data: JsonValue
    resources: tuple[ExtensionResource, ...] = ()


type ExtensionProvider = Callable[
    [ExtensionContext],
    Awaitable[ExtensionOutput | None],
]


class ResultResource(BaseModel):
    uri: str
    mime_type: str


class ResultResources(BaseModel):
    extensions_uri: str | None = None
    resources: list[ResultResource] = Field(default_factory=list)


class ResultExtension(BaseModel):
    name: str
    kind: str
    version: int
    data: JsonValue = None
    error: str | None = None


class ResultExtensions(BaseModel):
    extensions: list[ResultExtension]
    resources: list[ResultResource]

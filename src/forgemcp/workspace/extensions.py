"""Provider contracts for immutable workspace result resources."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, JsonValue

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
    resource: ExtensionResource
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


type ExtensionProvider = Callable[
    [ExtensionContext],
    Awaitable[ExtensionOutput | None],
]


class ResultResource(BaseModel):
    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)
    uri: str
    mime_type: str


class ResultResources(BaseModel):
    resources: dict[str, ResultResource] = Field(default_factory=dict)

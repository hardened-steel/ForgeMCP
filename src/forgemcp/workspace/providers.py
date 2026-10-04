"""Invocation contexts and links to immutable Workspace provider resources."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResultResource(BaseModel):
    """The URI and supported MIME type of an immutable linked result resource."""

    model_config = ConfigDict(extra="forbid", strict=True)

    uri: str
    mime_type: Literal["application/json", "text/markdown"]


class ResultResources(BaseModel):
    """Named provider resources attached to one workspace tool result."""

    resources: dict[str, ResultResource] = Field(default_factory=dict)


@dataclass(frozen=True)
class ProviderCall:
    """One invocation's identifier, tool name, and participating provider contexts."""

    id: str
    tool_name: str
    contexts: Mapping[str, object]

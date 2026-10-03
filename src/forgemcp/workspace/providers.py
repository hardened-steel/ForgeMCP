"""Invocation contexts and links to immutable Workspace provider resources."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResultResource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    uri: str
    mime_type: Literal["application/json", "text/markdown"]


class ResultResources(BaseModel):
    resources: dict[str, ResultResource] = Field(default_factory=dict)


@dataclass(frozen=True)
class ProviderCall:
    id: str
    tool_name: str
    contexts: Mapping[str, object]

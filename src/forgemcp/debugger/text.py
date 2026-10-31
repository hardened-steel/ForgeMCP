"""Readable debugger observations independent of the MCP App."""

from collections.abc import Sequence
from enum import Enum

from pydantic import BaseModel


def label(name: str) -> str:
    """Turn public result field names into concise English labels."""
    return name.replace("_", " ").capitalize()


def fields(value: object, indent: int = 0) -> list[str]:
    """Render every supplied observation field without interpreting untrusted strings."""
    prefix = " " * indent
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, dict):
        lines = []
        for name, item in value.items():
            if isinstance(item, (dict, list, BaseModel)):
                lines.append(f"{prefix}{label(str(name))}:")
                lines.extend(fields(item, indent + 2))
            else:
                lines.append(f"{prefix}{label(str(name))}: {scalar(item)}")
        return lines or [prefix + "(empty object)"]
    if isinstance(value, (list, tuple)):
        lines = []
        for index, item in enumerate(value, 1):
            lines.append(f"{prefix}[{index}]")
            lines.extend(fields(item, indent + 2))
        return lines or [prefix + "(empty list)"]
    return [prefix + scalar(value)]


def scalar(value: object) -> str:
    """Preserve rendered debugger text and distinguish absent and empty values."""
    if value is None:
        return "Unavailable"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value == "":
        return '""'
    return str(value)


def render(result: BaseModel | Sequence[BaseModel]) -> str:
    """Produce readable text while retaining the complete structured result separately."""
    return "\n".join(fields(result))

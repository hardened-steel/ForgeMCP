"""Bounded argument values and actionable discovery hints shared by feature errors."""

from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError


def display_value(value: object) -> str:
    """Quote a short argument value without emitting control characters or large payloads."""
    if isinstance(value, str):
        shortened = value[:120]
        return repr(shortened) + ("..." if len(value) > len(shortened) else "")
    return repr(value)


def available_values(values: Iterable[str], discovery: str) -> str:
    """List up to eight known choices and name an existing way to inspect the catalog."""
    known = sorted(set(values))
    if not known:
        description = "No values are currently available."
    elif len(known) <= 8:
        description = f"Available values: {', '.join(display_value(value) for value in known)}."
    else:
        description = f"There are {len(known)} available values."
    return f"{description} Call {discovery} to inspect available values."


def os_error_reason(error: OSError) -> str:
    """Keep the operating system's explanation without its native filename fields."""
    return error.strerror or type(error).__name__


def argument_errors(error: ValidationError, schema: dict[str, Any]) -> str:
    """Render SDK validation failures without input payloads or internal union branch names."""
    messages = []
    described = set()
    for detail in error.errors(include_url=False, include_input=False):
        location = detail["loc"]
        parameter = str(location[0]) if location else "arguments"
        field = schema.get("properties", {}).get(parameter, {})
        variants = [item for item in field.get("anyOf", []) if "$ref" in item]
        if len(variants) > 1 and field.get("description"):
            if parameter not in described:
                messages.append(f"Invalid {parameter}: {field['description']}")
                described.add(parameter)
            continue
        path = ""
        for component in location:
            if isinstance(component, int):
                path += f"[{component}]"
            else:
                path += ("." if path else "") + str(component)
        reason = detail["msg"].removeprefix("Value error, ").rstrip(".")
        message = f"Invalid {path or 'arguments'}: {reason}."
        if message not in messages:
            messages.append(message)
    return "\n".join(messages)

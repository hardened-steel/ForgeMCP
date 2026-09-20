"""Load built-in tool metadata without a central tool registry."""

import importlib
import pkgutil
import re
from types import ModuleType

from . import tools
from .errors import DuplicateToolSpecError, ToolModuleError
from .spec import ToolInfo, ToolKind


def load_tools(package: ModuleType = tools) -> tuple[ToolInfo, ...]:
    specs = []
    names: set[str] = set()
    for info in sorted(
        pkgutil.iter_modules(package.__path__),
        key=lambda item: item.name,
    ):
        if info.name.startswith("_"):
            continue
        module_name = f"{package.__name__}.{info.name}"
        try:
            if info.ispkg:
                raise ValueError("Tool entries must be modules.")
            module = importlib.import_module(module_name)
            spec = module.INFO
            if (
                not isinstance(spec, ToolInfo)
                or not isinstance(spec.name, str)
                or not re.fullmatch(r"[a-z][a-z0-9_+.-]*", spec.name)
                or not isinstance(spec.kind, ToolKind)
                or not callable(spec.create_spec)
                or sum(isinstance(value, ToolInfo) for value in vars(module).values())
                != 1
            ):
                raise ValueError(
                    "Expected exactly one INFO with a create_spec factory."
                )
        except Exception:
            raise ToolModuleError(
                f"Invalid built-in tool module {module_name}."
            ) from None
        if spec.name in names:
            raise DuplicateToolSpecError(f"Duplicate tool spec {spec.name!r}.")
        names.add(spec.name)
        specs.append(spec)
    return tuple(specs)

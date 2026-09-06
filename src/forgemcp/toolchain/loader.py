"""Load built-in specs by package enumeration, without a central tool registry."""

import importlib
import pkgutil
import re
from types import ModuleType

from . import tools
from .errors import DuplicateToolSpecError, ToolModuleError
from .spec import ToolCommand, ToolKind, ToolSpec


def load_tools(package: ModuleType = tools) -> tuple[ToolSpec, ...]:
    specs = []
    names: set[str] = set()
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda item: item.name):
        if info.name.startswith("_"):
            continue
        module_name = f"{package.__name__}.{info.name}"
        try:
            if info.ispkg:
                raise ValueError("Tool entries must be modules.")
            module = importlib.import_module(module_name)
            spec = module.SPEC
            if (not isinstance(spec, ToolSpec)
                    or not isinstance(spec.name, str)
                    or not re.fullmatch(r"[a-z][a-z0-9_+.-]*", spec.name)
                    or not isinstance(spec.kind, ToolKind)
                    or spec.path is not None or spec.version is not None
                    or sum(isinstance(value, ToolSpec) for value in vars(module).values()) != 1):
                raise ValueError("Expected exactly one unbound SPEC.")
            for name, command in spec.commands.items():
                if (not isinstance(name, str) or not name or not isinstance(command, ToolCommand)
                        or not callable(command.arguments) or command.path is not None
                        or command.processes is not None
                        or (command.parser is not None and command.parser not in spec.parsers)):
                    raise ValueError("Invalid command.")
            if any(not isinstance(key, str) or not key or not callable(parser)
                   for key, parser in spec.parsers.items()):
                raise ValueError("Invalid parser.")
        except Exception:
            raise ToolModuleError(f"Invalid built-in tool module {module_name}.") from None
        if spec.name in names:
            raise DuplicateToolSpecError(f"Duplicate tool spec {spec.name!r}.")
        names.add(spec.name)
        specs.append(spec)
    return tuple(specs)

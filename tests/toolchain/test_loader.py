"""Dynamic built-in tool discovery and metadata validation tests."""

import importlib
from pathlib import Path

import pytest

from forgemcp.toolchain.errors import DuplicateToolSpecError, ToolModuleError
from forgemcp.toolchain.loader import load_tools


def test_load_all_builtin_modules():
    """Verify all built-in tools have unique names and callable spec factories."""
    specs = load_tools()
    assert len(specs) == 19
    assert len({spec.name for spec in specs}) == 19
    assert {"clang++", "g++", "cl", "cppvsdbg", "lldb-dap"} <= {
        spec.name for spec in specs
    }
    assert all(callable(spec.create_spec) for spec in specs)


def test_enumeration_is_dynamic_sorted_and_skips_private(tmp_path, monkeypatch):
    """Verify enumeration sees added public modules in deterministic order and skips private
    names.
    """
    package = tmp_path / "fixture_tools"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "_ignored.py").write_text("raise RuntimeError('ignored')")
    source = "from forgemcp.toolchain.spec import ToolInfo, ToolKind\nINFO=ToolInfo({!r}, ToolKind.OTHER, lambda *args: None)\n"
    (package / "zebra.py").write_text(source.format("first"))
    (package / "alpha.py").write_text(source.format("second"))
    monkeypatch.syspath_prepend(str(tmp_path))
    loaded = importlib.import_module("fixture_tools")
    assert [spec.name for spec in load_tools(loaded)] == ["second", "first"]
    (package / "new_tool.py").write_text(source.format("added"))
    importlib.invalidate_caches()
    assert [spec.name for spec in load_tools(loaded)] == ["second", "added", "first"]
    (package / "duplicate.py").write_text(source.format("added"))
    importlib.invalidate_caches()
    with pytest.raises(DuplicateToolSpecError):
        load_tools(loaded)


@pytest.mark.parametrize(
    "source",
    [
        "",
        "INFO=42",
        "raise RuntimeError('sensitive')",
        "from forgemcp.toolchain.spec import ToolInfo,ToolKind\nINFO=ToolInfo('x',ToolKind.OTHER,None)",
        "from forgemcp.toolchain.spec import ToolInfo,ToolKind\nINFO=ToolInfo('x',ToolKind.OTHER,lambda *args: None)\nOTHER=INFO",
    ],
)
def test_invalid_modules_are_domain_errors(tmp_path, monkeypatch, source):
    """Verify invalid tool metadata raises sanitized domain errors."""
    import types

    package = types.ModuleType("invalid_tools")
    package.__path__ = [str(tmp_path)]
    (tmp_path / "invalid.py").write_text(source)
    monkeypatch.setitem(__import__("sys").modules, "invalid_tools", package)
    __import__("sys").modules.pop("invalid_tools.invalid", None)
    with pytest.raises(ToolModuleError) as error:
        load_tools(package)
    assert "sensitive" not in str(error.value)

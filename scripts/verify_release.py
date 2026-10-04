"""Verify distribution metadata, packaged assets, and frontend build sources."""

from __future__ import annotations

import argparse
import ast
from email.parser import BytesParser
import hashlib
from pathlib import Path
import tarfile
import tomllib
import zipfile


def verify_distributions(directory: Path, tag: str | None = None) -> list[Path]:
    """Reject incomplete distributions or mismatched package, source, and tag versions."""
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"Expected tag v{version}, got {tag!r}.")

    wheels = list(directory.glob("*.whl"))
    sources = list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("Expected exactly one wheel and one source distribution.")

    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        metadata_paths = [
            name
            for name in names
            if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_paths) != 1:
            raise ValueError("Expected one wheel metadata file.")
        metadata = BytesParser().parsebytes(wheel.read(metadata_paths[0]))
        if metadata["Name"] != project["name"] or metadata["Version"] != version:
            raise ValueError("Wheel metadata does not match pyproject.toml.")
        module = ast.parse(wheel.read("forgemcp/__init__.py"))
        versions = [
            node.value.value
            for node in module.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
        ]
        if versions != [version]:
            raise ValueError("Runtime version does not match package metadata.")
        for name in sorted(names):
            if not name.startswith("forgemcp/") or not name.endswith(".py"):
                continue
            for node in ast.walk(ast.parse(wheel.read(name))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in {"Widget", "IconFile"}
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    asset = "forgemcp/" + node.args[0].value
                    if asset not in names or not wheel.read(asset):
                        raise ValueError(f"Missing or empty packaged asset: {asset}.")
        if not any(name.endswith("/licenses/LICENSE") for name in names):
            raise ValueError("Wheel is missing its license file.")

    with tarfile.open(sources[0]) as source:
        members = source.getnames()
        prefixes = {name.split("/", 1)[0] for name in members}
        if len(prefixes) != 1:
            raise ValueError("Source distribution must have one root directory.")
        prefix = prefixes.pop() + "/"
        names = {name.removeprefix(prefix) for name in members}
        required = {
            "hatch_build.py",
            "pyproject.toml",
            "README.md",
            "LICENSE",
            "PKG-INFO",
            "src/forgemcp/__init__.py",
            "frontend/package.json",
            "frontend/package-lock.json",
            "frontend/vite.config.js",
        }
        required.update(
            path.relative_to(root).as_posix()
            for path in (root / "frontend" / "src").rglob("*")
            if path.is_file()
        )
        required.update(
            path.relative_to(root).as_posix()
            for path in (root / "frontend").glob("*.html")
        )
        if missing := required - names:
            raise ValueError(f"Source distribution is missing: {sorted(missing)}.")
        if any(
            "node_modules" in name.split("/") or "__pycache__" in name.split("/")
            for name in names
        ):
            raise ValueError("Source distribution includes local dependencies or caches.")
        info = source.extractfile(prefix + "PKG-INFO")
        if info is None:
            raise ValueError("Source metadata is missing.")
        with info:
            metadata = BytesParser().parsebytes(info.read())
        if metadata["Name"] != project["name"] or metadata["Version"] != version:
            raise ValueError("Source metadata does not match pyproject.toml.")
    return [wheels[0], sources[0]]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--tag")
    parser.add_argument("--checksums", action="store_true")
    arguments = parser.parse_args()
    packages = verify_distributions(arguments.directory, arguments.tag)
    if arguments.checksums:
        lines = [
            f"{hashlib.sha256(package.read_bytes()).hexdigest()}  {package.name}\n"
            for package in sorted(packages)
        ]
        (arguments.directory / "SHA256SUMS").write_text(
            "".join(lines),
            encoding="utf-8",
        )
    print("Verified package versions, assets, license, and source build inputs.")


if __name__ == "__main__":
    main()

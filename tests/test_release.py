"""Release metadata and source distribution validation, independent of widget behavior."""

from email.message import EmailMessage
import io
from pathlib import Path
import runpy
import tarfile
import tomllib
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
verify_distributions = runpy.run_path(
    str(ROOT / "scripts" / "verify_release.py"),
)["verify_distributions"]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


@pytest.fixture
def distributions(tmp_path: Path) -> Path:
    """Create small archives with real release metadata and source file names."""
    metadata = EmailMessage()
    metadata["Name"] = PROJECT["name"]
    metadata["Version"] = PROJECT["version"]
    with zipfile.ZipFile(tmp_path / "package.whl", "w") as wheel:
        wheel.writestr("forge_cpp_mcp.dist-info/METADATA", metadata.as_bytes())
        wheel.writestr(
            "forgemcp/__init__.py",
            f'__version__ = "{PROJECT["version"]}"\n',
        )
        wheel.writestr("forge_cpp_mcp.dist-info/licenses/LICENSE", "MIT")
    required = {
        "hatch_build.py",
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "src/forgemcp/__init__.py",
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/vite.config.js",
    }
    required.update(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "frontend" / "src").rglob("*")
        if path.is_file()
    )
    required.update(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "frontend").glob("*.html")
    )
    with tarfile.open(tmp_path / "package.tar.gz", "w:gz") as source:
        for name in sorted(required | {"PKG-INFO"}):
            content = metadata.as_bytes() if name == "PKG-INFO" else b"source\n"
            member = tarfile.TarInfo("package/" + name)
            member.size = len(content)
            source.addfile(member, io.BytesIO(content))
    return tmp_path


def test_release_metadata_and_source_inputs_are_accepted(distributions: Path) -> None:
    assert len(verify_distributions(distributions, f'v{PROJECT["version"]}')) == 2


def test_release_rejects_wrong_tag(distributions: Path) -> None:
    with pytest.raises(ValueError, match="Expected tag"):
        verify_distributions(distributions, "v99.0.0")


def test_release_rejects_stale_packages(distributions: Path) -> None:
    (distributions / "old.whl").write_bytes(b"stale")
    with pytest.raises(ValueError, match="exactly one wheel"):
        verify_distributions(distributions)


def test_release_rejects_runtime_version_mismatch(distributions: Path) -> None:
    with zipfile.ZipFile(distributions / "package.whl") as wheel:
        files = {name: wheel.read(name) for name in wheel.namelist()}
    files["forgemcp/__init__.py"] = b'__version__ = "99.0.0"\n'
    with zipfile.ZipFile(distributions / "package.whl", "w") as wheel:
        for name, content in files.items():
            wheel.writestr(name, content)
    with pytest.raises(ValueError, match="Runtime version"):
        verify_distributions(distributions)


def test_release_rejects_source_without_lockfile(distributions: Path) -> None:
    with tarfile.open(distributions / "package.tar.gz") as source:
        files = {}
        for member in source.getmembers():
            extracted = source.extractfile(member)
            assert extracted is not None
            with extracted:
                files[member.name] = extracted.read()
    del files["package/frontend/package-lock.json"]
    with tarfile.open(distributions / "package.tar.gz", "w:gz") as source:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            source.addfile(member, io.BytesIO(content))
    with pytest.raises(ValueError, match="package-lock.json"):
        verify_distributions(distributions)

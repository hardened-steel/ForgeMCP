"""Read-only qualification tests for the Phase-0 standalone lldb-dap gate."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.models import ProcessOutput, ProcessResult
from forgemcp.processes import LldbDapCandidate, LldbDapQualifier, ProcessEnvironmentMode


def _result(*, exit_code: int = 0, stdout: str = "", stderr: str = "") -> ProcessResult:
    now = datetime.now(UTC)
    return ProcessResult(
        exit_code=exit_code,
        started_at=now,
        finished_at=now,
        stdout=ProcessOutput(text=stdout),
        stderr=ProcessOutput(text=stderr),
    )


class _FakeHandle:
    required_ownership = True
    ownership_established = True
    environment_mode = ProcessEnvironmentMode.SCRUBBED
    returncode: int | None = None

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        self.returncode = 0


class _FakeRuntime:
    def __init__(self, results: list[ProcessResult]) -> None:
        self.results = list(results)
        self.run_calls: list[tuple[tuple[str, ...], tuple[Path, ...]]] = []
        self.start_calls: list[tuple[tuple[str, ...], tuple[Path, ...]]] = []
        self.handle = _FakeHandle()
        self.closed = False

    async def run_trusted_adapter(
        self,
        argv,
        *,
        approved_path_directories=(),
        timeout_seconds=None,
    ) -> ProcessResult:
        self.run_calls.append((tuple(argv), tuple(approved_path_directories)))
        return self.results.pop(0)

    async def start_trusted_adapter(self, argv, *, approved_path_directories=()):
        self.start_calls.append((tuple(argv), tuple(approved_path_directories)))
        return self.handle

    async def aclose(self) -> None:
        self.closed = True


def test_discovery_orders_explicit_configuration_before_path(tmp_path: Path):
    executable_name = "lldb-dap.exe" if os.name == "nt" else "lldb-dap"
    explicit = tmp_path / "explicit" / executable_name
    path_candidate = tmp_path / "path" / executable_name
    explicit.parent.mkdir()
    path_candidate.parent.mkdir()
    explicit.write_bytes(b"candidate")
    path_candidate.write_bytes(b"candidate")
    explicit.chmod(0o755)
    path_candidate.chmod(0o755)

    qualifier = LldbDapQualifier(
        ForgeConfig(workspace_root=tmp_path, lldb_dap_path=explicit),
        create_logger("CRITICAL"),
        environment={"PATH": str(path_candidate.parent)},
    )

    discovered = qualifier.discover()

    assert [(candidate.path, candidate.source) for candidate in discovered[:2]] == [
        (explicit.absolute(), "FORGEMCP_LLDB_DAP"),
        (path_candidate.absolute(), "PATH"),
    ]


def test_qualification_uses_exact_strict_runtime_and_closes_a_runnable_candidate(tmp_path: Path):
    async def exercise() -> None:
        executable = tmp_path / ("lldb-dap.exe" if os.name == "nt" else "lldb-dap")
        companion = tmp_path / "llvm-bin"
        executable.write_bytes(b"candidate")
        executable.chmod(0o755)
        companion.mkdir()
        fake_runtime = _FakeRuntime([_result(stdout="lldb-dap version 19.1.0\n")])
        qualifier = LldbDapQualifier(
            ForgeConfig(workspace_root=tmp_path),
            create_logger("CRITICAL"),
            runtime_factory=lambda policy: fake_runtime,  # type: ignore[arg-type]
        )

        result = await qualifier.qualify(
            LldbDapCandidate(executable, "test", companion_directories=(companion,))
        )

        assert result.available is True
        assert result.version == "19.1.0"
        assert result.executable_path == executable.resolve()
        assert result.process_tree_ownership is True
        assert result.environment_isolated is True
        assert result.confirmed_object_formats == ()
        assert "PE/COFF object support" in result.unverified_capabilities
        assert fake_runtime.run_calls == [((str(executable), "--version"), (companion,))]
        assert fake_runtime.start_calls == [((str(executable),), (companion,))]
        assert fake_runtime.handle.closed is True
        assert fake_runtime.closed is True

    asyncio.run(exercise())


def test_qualification_rejects_a_broken_candidate_after_version_and_help_probes(tmp_path: Path):
    async def exercise() -> None:
        executable = tmp_path / ("lldb-dap.exe" if os.name == "nt" else "lldb-dap")
        executable.write_bytes(b"candidate")
        executable.chmod(0o755)
        fake_runtime = _FakeRuntime([_result(exit_code=1), _result(exit_code=1)])
        qualifier = LldbDapQualifier(
            ForgeConfig(workspace_root=tmp_path),
            create_logger("CRITICAL"),
            runtime_factory=lambda policy: fake_runtime,  # type: ignore[arg-type]
        )

        result = await qualifier.qualify(LldbDapCandidate(executable, "test"))

        assert result.available is False
        assert result.version is None
        assert result.unavailable_reason == "adapter did not return a recognized successful version banner"
        assert [call[0][1] for call in fake_runtime.run_calls] == ["--version", "--help"]
        assert fake_runtime.start_calls == []
        assert fake_runtime.closed is True

    asyncio.run(exercise())

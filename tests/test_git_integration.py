"""Read-only Git Phase 1 fake/security and disposable real-service coverage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from forgemcp.core.application import ForgeApplication
from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.git import GitService
from forgemcp.git.errors import GitRequestError, GitUnavailableError
from forgemcp.git.models import MAX_GIT_STATUS_RECORDS
from forgemcp.models import ProcessOutput, ProcessResult
from forgemcp.processes import ProcessEnvironmentMode, ProcessPolicy, ProcessRuntime
from tests.acceptance_manifest import McpToolCallCollector


FIXTURE_ROOT = Path(__file__).parents[1] / "examples" / "cpp-acceptance-project"


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=root, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return completed.stdout


def _git_result(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=root, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )


def _commit(root: Path, message: str) -> None:
    _git(
        root, "-c", "user.name=Fixture Author", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", message,
    )


def _arm_malicious_project_helpers(root: Path) -> Path:
    """Install inert test-only helpers which must never be executed by ForgeMCP."""
    marker = root / "git-helper-invoked"
    helper = root.parent / "git-malicious-helper.sh"
    helper.write_text("#!/bin/sh\nprintf invoked > git-helper-invoked\n", encoding="utf-8")
    if os.name != "nt":
        helper.chmod(0o700)
    included = root / "malicious-git-include"
    helper_config_value = helper.as_posix()
    included.write_text(
        "[core]\n"
        f"\tfsmonitor = {helper_config_value}\n"
        f"\tpager = {helper_config_value}\n"
        f"\teditor = {helper_config_value}\n"
        f"\tsshCommand = {helper_config_value}\n"
        f"\taskPass = {helper_config_value}\n"
        "[diff]\n"
        f"\texternal = {helper_config_value}\n"
        "[diff \"malicious\"]\n"
        f"\tcommand = {helper_config_value}\n"
        f"\ttextconv = {helper_config_value}\n"
        "[credential]\n"
        f"\thelper = {helper_config_value}\n"
        "[alias]\n"
        f"\tcanary = !{helper_config_value}\n"
        "[submodule \"canary\"]\n"
        f"\tupdate = !{helper_config_value}\n",
        encoding="utf-8",
    )
    _git(root, "config", "include.path", str(included))
    for key in ("diff.external", "diff.malicious.textconv", "diff.malicious.command", "core.fsmonitor"):
        _git(root, "config", key, str(helper))
    return marker


def _fixture(destination: Path) -> Path:
    """Create an isolated Git history over the committed C++ acceptance fixture."""
    root = destination / "cpp-acceptance-project"
    shutil.copytree(FIXTURE_ROOT, root)
    _git(root, "init")
    (root / ".gitattributes").write_text("external.sensitive diff=malicious\n", encoding="utf-8")
    (root / "external.sensitive").write_text("before\n", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"\x00\x01initial\xff")
    _git(root, "add", ".")
    _commit(root, "initial fixture")
    _git(root, "branch", "topic")
    source = root / "app" / "good_main.cpp"
    source.write_text(source.read_text(encoding="utf-8") + "\n// unstaged Git fixture change\n", encoding="utf-8")
    (root / "external.sensitive").write_text("after\n", encoding="utf-8")
    (root / "analysis" / "format_me.cpp").rename(root / "analysis" / "format_renamed.cpp")
    (root / "analysis" / "code_action.cpp").unlink()
    (root / "binary.bin").write_bytes(b"\x00\x02changed\xfe")
    (root / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(root, "add", "-A", "--", "analysis/format_me.cpp", "analysis/format_renamed.cpp", "analysis/code_action.cpp")
    _git(root, "add", "staged.txt")
    (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    _arm_malicious_project_helpers(root)
    return root


def _fixture_hashes() -> dict[str, str]:
    return {
        path.relative_to(FIXTURE_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(FIXTURE_ROOT.rglob("*")) if path.is_file()
    }


def _repository_state(root: Path) -> dict[str, object]:
    """Capture the Git state Phase 1 is prohibited from changing."""
    tracked = _git(root, "-c", "core.fsmonitor=false", "ls-files", "-z").split("\0")
    tracked_hashes = {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in tracked if path
    }
    git_dir = root / ".git"
    return {
        "status": _git(root, "-c", "core.fsmonitor=false", "--no-pager", "status", "--porcelain=v2", "-z"),
        "head": _git(root, "-c", "core.fsmonitor=false", "rev-parse", "HEAD"),
        "refs": _git(root, "-c", "core.fsmonitor=false", "show-ref", "--head"),
        "index": hashlib.sha256((git_dir / "index").read_bytes()).hexdigest(),
        "config": hashlib.sha256((git_dir / "config").read_bytes()).hexdigest(),
        "tracked": tracked_hashes,
        "reflogs": _git(root, "-c", "core.fsmonitor=false", "reflog", "show", "--all", "--date=raw"),
    }


@pytest.fixture
def git_repository(tmp_path: Path) -> Iterator[Path]:
    """Return a disposable fixture and remove all test-owned Git state afterwards."""
    repository = _fixture(tmp_path)
    try:
        yield repository
    finally:
        def retry_readonly(remove: object, raw_path: str, _: object) -> None:
            path = Path(raw_path)
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
            remove(raw_path)  # type: ignore[operator]

        shutil.rmtree(repository, onerror=retry_readonly)
        for temporary in (
            tmp_path / "git-malicious-helper.sh",
            tmp_path / "git-helper-invoked",
        ):
            temporary.unlink(missing_ok=True)
        assert not repository.exists()


def _process_result(stdout: str, *, stdout_truncated: bool = False) -> ProcessResult:
    now = datetime.now(UTC)
    return ProcessResult(
        exit_code=0, started_at=now, finished_at=now,
        stdout=ProcessOutput(text=stdout, truncated=stdout_truncated), stderr=ProcessOutput(text=""),
    )


@pytest.mark.git_fixture_mcp
def test_git_service_real_disposable_repository_all_six_tools(git_repository: Path) -> None:
    async def exercise() -> None:
        repository = git_repository
        application = ForgeApplication.create(ForgeConfig(workspace_root=repository))
        await application.start()
        service = application.services.get("plugins")._records["git"].plugin.service
        assert isinstance(service, GitService)
        state_before = _repository_state(repository)
        status = await service.status()
        assert status.repository.value == "available"
        assert status.staged_count >= 2 and status.untracked_count >= 1
        assert any(record.original_path == "analysis/format_me.cpp" for record in status.files)
        assert not (repository / "git-helper-invoked").exists()
        unstaged = await service.diff(scope="unstaged", paths=("app/good_main.cpp", "binary.bin"), context_lines=1)
        assert "app/good_main.cpp" in unstaged.patch and unstaged.summary.scope == "unstaged"
        assert unstaged.summary.binary_file_count >= 1 and "GIT binary patch" not in unstaged.patch
        textconv = await service.diff(scope="unstaged", paths=("external.sensitive",))
        assert "external.sensitive" in textconv.patch
        assert not (repository / "git-helper-invoked").exists()
        staged = await service.diff(scope="staged")
        assert staged.summary.scope == "staged" and "staged.txt" in staged.patch
        log = await service.log(limit=10)
        assert log.commits and log.commits[0].author_name == "Fixture Author"
        shown = await service.show_commit(log.commits[0].oid)
        assert shown.commit.oid == log.commits[0].oid
        blame = await service.blame(path="app/good_main.cpp", start_line=1, end_line=2)
        assert blame.ranges and all(item.author_name for item in blame.ranges)
        branches = await service.list_branches()
        names = {item.name for item in branches.branches}
        assert "topic" in names and ("master" in names or "main" in names)
        assert _repository_state(repository) == state_before
        # Detached status is repository metadata, not an unavailable state.
        _git(repository, "checkout", "--detach", "HEAD")
        assert (await service.status()).detached is True
        await application.aclose()

    asyncio.run(exercise())


def test_git_status_detects_conflict_in_a_separate_disposable_copy(git_repository: Path) -> None:
    async def exercise() -> None:
        repository = git_repository
        _git(repository, "config", "--unset", "core.fsmonitor")
        base_branch = _git(repository, "branch", "--show-current").strip()
        source = repository / "app" / "good_main.cpp"
        _git(repository, "checkout", "-b", "conflict-left")
        source.write_text("// left conflict\n", encoding="utf-8")
        _git(repository, "add", "app/good_main.cpp")
        _commit(repository, "left conflict")
        _git(repository, "checkout", base_branch)
        source.write_text("// right conflict\n", encoding="utf-8")
        _git(repository, "add", "app/good_main.cpp")
        _commit(repository, "right conflict")
        assert _git_result(repository, "merge", "conflict-left").returncode != 0
        application = ForgeApplication.create(ForgeConfig(workspace_root=repository))
        await application.start()
        service = application.services.get("plugins")._records["git"].plugin.service
        assert isinstance(service, GitService)
        status = await service.status()
        assert status.conflicted_count >= 1
        await application.aclose()

    asyncio.run(exercise())


def test_git_rejects_revision_expressions_and_unsafe_paths(git_repository: Path) -> None:
    async def exercise() -> None:
        repository = git_repository
        _git(repository, "config", "--unset", "core.fsmonitor")
        (repository / "--option").write_text("literal option-looking filename\n", encoding="utf-8")
        (repository / "@response").write_text("literal response-looking filename\n", encoding="utf-8")
        _git(repository, "add", "--", "--option", "@response")
        (repository / "--option").write_text("literal option-looking filename changed\n", encoding="utf-8")
        (repository / "@response").write_text("literal response-looking filename changed\n", encoding="utf-8")
        application = ForgeApplication.create(ForgeConfig(workspace_root=repository))
        await application.start()
        service = application.services.get("plugins")._records["git"].plugin.service
        with pytest.raises(GitRequestError):
            await service.show_commit("HEAD")
        with pytest.raises(GitRequestError):
            await service.show_commit("a" * 64)
        # Option-looking filenames remain valid literal pathspecs after --;
        # they are never parsed as Git options or response files.
        literal = await service.diff(scope="unstaged", paths=("--option", "@response"))
        assert "literal option-looking filename changed" in literal.patch
        assert "literal response-looking filename changed" in literal.patch
        with pytest.raises(GitRequestError):
            await service.diff(scope="unstaged", paths=("../outside",))
        with pytest.raises(GitRequestError):
            await service.blame(path="C:/outside.txt")
        await application.aclose()

    asyncio.run(exercise())


def test_git_status_protocol_is_fail_closed_and_uses_fixed_read_only_controls(git_repository: Path) -> None:
    class FakeGitRuntime:
        def __init__(self, repository: Path) -> None:
            self.repository = repository
            self.mode = "malformed"
            self.calls: list[tuple[str, ...]] = []

        async def run_git(self, argv: tuple[str, ...], *, cwd: str, timeout_seconds: float) -> ProcessResult:
            values = tuple(argv)
            self.calls.append(values)
            assert cwd == "." and timeout_seconds > 0
            assert values[1:11] == (
                "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "credential.helper=",
                "-c", "diff.external=", "-c", "submodule.recurse=false", "--no-pager",
            )
            if values[-1] == "--version":
                return _process_result("git version 2.48.1\n")
            if "rev-parse" in values:
                if "--show-object-format" in values:
                    return _process_result("sha1\n")
                return _process_result(f"true\n{self.repository}\n")
            if "status" in values:
                if self.mode == "malformed":
                    return _process_result("# branch.oid " + "a" * 40 + "\0? ../secret-project-data\0")
                if self.mode == "oversized":
                    records = "".join(f"? generated-{index}\0" for index in range(MAX_GIT_STATUS_RECORDS + 1))
                    return _process_result("# branch.oid " + "a" * 40 + "\0" + records)
                return _process_result("# branch.oid " + "a" * 40 + "\0", stdout_truncated=True)
            raise AssertionError(f"Unexpected fixed Git command: {values[-1]!r}")

    async def exercise() -> None:
        repository = git_repository
        application = ForgeApplication.create(ForgeConfig(workspace_root=repository))
        await application.start()
        try:
            service = application.services.get("plugins")._records["git"].plugin.service
            assert isinstance(service, GitService)
            runtime = FakeGitRuntime(repository)
            service._runtime = runtime
            malformed = await service.status()
            assert malformed.repository.value == "error" and malformed.incomplete is True
            assert malformed.error is not None and "secret-project-data" not in malformed.error
            runtime.mode = "oversized"
            oversized = await service.status()
            assert oversized.repository.value == "available" and oversized.truncated is True and oversized.incomplete is True
            runtime.mode = "truncated"
            truncated = await service.status()
            assert truncated.repository.value == "available" and truncated.truncated is True and truncated.incomplete is True
            assert runtime.calls
        finally:
            await application.aclose()

    asyncio.run(exercise())


def test_git_runtime_uses_only_the_fixed_scrubbed_environment(git_repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The private Git runner cannot inherit remote, prompt, trace, or secret state."""
    git_path = Path(shutil.which("git") or "").resolve()
    assert git_path.is_file()
    runtime = ProcessRuntime(
        ForgeConfig(workspace_root=git_repository),
        create_logger("ERROR"),
        policy=ProcessPolicy(allowed_executable_paths=frozenset({git_path})),
    )
    captured: dict[str, object] = {}

    async def record_run(*_args: object, **kwargs: object) -> ProcessResult:
        captured.update(kwargs)
        return _process_result("git version 2.53.0\n")

    monkeypatch.setattr(runtime, "run", record_run)
    asyncio.run(runtime.run_git((str(git_path), "--version")))
    assert captured["environment_mode"] is ProcessEnvironmentMode.SCRUBBED
    assert captured["require_exact_executable"] is True
    assert captured["environment"] == {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
    }


def test_git_rejects_external_object_alternates_and_malformed_gitfile(git_repository: Path, tmp_path: Path) -> None:
    async def exercise() -> None:
        alternates = git_repository / ".git" / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(str(tmp_path / "foreign-object-store") + "\n", encoding="utf-8")
        application = ForgeApplication.create(ForgeConfig(workspace_root=git_repository))
        await application.start()
        try:
            service = application.services.get("plugins")._records["git"].plugin.service
            assert isinstance(service, GitService)
            status = await service.status()
            assert status.repository.value == "error"
            assert status.error is not None and "foreign-object-store" not in status.error
        finally:
            await application.aclose()

        malicious = tmp_path / "malicious-workspace"
        malicious.mkdir()
        (malicious / ".git").write_text(
            f"gitdir: {git_repository / '.git'}\n", encoding="utf-8"
        )
        application = ForgeApplication.create(ForgeConfig(workspace_root=malicious))
        await application.start()
        try:
            service = application.services.get("plugins")._records["git"].plugin.service
            assert isinstance(service, GitService)
            status = await service.status()
            assert status.repository.value == "error"
        finally:
            await application.aclose()

    asyncio.run(exercise())


def test_git_accepts_a_well_formed_linked_worktree_and_detects_pointer_replacement(
    git_repository: Path, tmp_path: Path,
) -> None:
    linked = tmp_path / "linked-worktree"
    _git(git_repository, "-c", "core.fsmonitor=false", "worktree", "add", "--detach", str(linked), "HEAD")
    original_pointer = (linked / ".git").read_text(encoding="utf-8")

    async def exercise() -> None:
        application = ForgeApplication.create(ForgeConfig(workspace_root=linked))
        await application.start()
        try:
            service = application.services.get("plugins")._records["git"].plugin.service
            assert isinstance(service, GitService)
            assert (await service.status()).repository.value == "available"
            assert await service._repository_available() is None
            # The next launch revalidates the exact workspace .git pointer,
            # rather than trusting the earlier boundary check.
            pointer = linked / ".git"
            if os.name == "nt":
                subprocess.run(["attrib", "-H", "-R", str(pointer)], check=True)
            pointer.chmod(pointer.stat().st_mode | stat.S_IWRITE)
            pointer.write_text("gitdir: invalid\n", encoding="utf-8")
            with pytest.raises(GitUnavailableError):
                await service._run(("status", "--porcelain=v2", "-z"), timeout_seconds=1.0)
        finally:
            await application.aclose()

    try:
        asyncio.run(exercise())
    finally:
        pointer = linked / ".git"
        if os.name == "nt":
            subprocess.run(["attrib", "-H", "-R", str(pointer)], check=True)
        pointer.chmod(pointer.stat().st_mode | stat.S_IWRITE)
        pointer.write_text(original_pointer, encoding="utf-8")
        _git(git_repository, "-c", "core.fsmonitor=false", "worktree", "remove", "--force", str(linked))


def test_git_log_cursor_is_application_local_bounded_and_expires(git_repository: Path) -> None:
    async def exercise() -> None:
        application = ForgeApplication.create(ForgeConfig(workspace_root=git_repository))
        await application.start()
        try:
            service = application.services.get("plugins")._records["git"].plugin.service
            assert isinstance(service, GitService)
            cursor = (await service.log(limit=1)).next_cursor
            assert cursor is not None
            service._cursors[cursor] = replace(service._cursors[cursor], expires_at=datetime.now(UTC))
            with pytest.raises(GitRequestError):
                await service.log(limit=1, cursor=cursor)
        finally:
            await application.aclose()

    asyncio.run(exercise())


def test_git_resource_and_project_status_are_cache_only(git_repository: Path) -> None:
    async def exercise() -> None:
        application = ForgeApplication.create(ForgeConfig(workspace_root=git_repository))
        await application.start()
        try:
            plugin = application.services.get("plugins")._records["git"].plugin
            service = plugin.service

            class NoGitRuntime:
                async def run_git(self, *_args: object, **_kwargs: object) -> ProcessResult:
                    raise AssertionError("A cache-only status surface launched Git")

            service._runtime = NoGitRuntime()
            resource = plugin._status_resource()
            assert resource["error"]["code"] == "git_cache_unavailable"
            assert await application.services.get("project_status_service").status()
        finally:
            await application.aclose()

    asyncio.run(exercise())


def test_git_partial_clone_metadata_cannot_lazy_fetch_or_run_a_remote_helper(git_repository: Path) -> None:
    """A missing promisor blob must fail locally, without even launching ext::."""
    marker = git_repository / "git-network-helper-invoked"
    helper = git_repository.parent / "git-network-helper.sh"
    helper.write_text(f"#!/bin/sh\nprintf network > {marker.as_posix()}\n", encoding="utf-8")
    if os.name != "nt":
        helper.chmod(0o700)
    blob_oid = _git(git_repository, "rev-parse", "HEAD:app/good_main.cpp").strip()
    blob = git_repository / ".git" / "objects" / blob_oid[:2] / blob_oid[2:]
    assert blob.is_file()
    if os.name == "nt":
        subprocess.run(["attrib", "-H", "-R", str(blob)], check=True)
    blob.unlink()
    for key, value in (
        ("protocol.ext.allow", "always"),
        ("remote.origin.url", f"ext::{helper.as_posix()}"),
        ("remote.origin.promisor", "true"),
        ("remote.origin.partialclonefilter", "blob:none"),
        ("extensions.partialclone", "origin"),
    ):
        _git(git_repository, "config", key, value)

    async def exercise() -> None:
        application = ForgeApplication.create(ForgeConfig(workspace_root=git_repository))
        await application.start()
        try:
            service = application.services.get("plugins")._records["git"].plugin.service
            assert isinstance(service, GitService)
            # log need not materialize blobs, while show/diff/blame do on
            # supported Git builds.  Every path must remain local either way.
            assert (await service.log(limit=1)).commits
            for operation in (
                lambda: service.show_commit(_git(git_repository, "rev-parse", "HEAD").strip()),
                lambda: service.diff(scope="unstaged", paths=("app/good_main.cpp",)),
                lambda: service.blame(path="app/good_main.cpp", start_line=1, end_line=2),
            ):
                try:
                    await operation()
                except GitUnavailableError:
                    pass
                assert not marker.exists()
        finally:
            await application.aclose()

    try:
        asyncio.run(exercise())
    finally:
        helper.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)


@pytest.mark.git_fixture_mcp
def test_git_mcp_sdk_disposable_repository_all_six_tools(git_repository: Path) -> None:
    """Official SDK evidence for every declared Git tool on a disposable repo."""
    committed_fixture_before = _fixture_hashes()
    repository = git_repository
    state_before = _repository_state(repository)

    def payload(result: object) -> dict[str, object]:
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        content = getattr(result, "content")
        assert len(content) == 1
        value = json.loads(getattr(content[0], "text"))
        assert isinstance(value, dict)
        return value

    async def exercise() -> None:
        helper = repository.parent / "git-malicious-helper.sh"
        hostile_environment = {
            # These values would redirect Git, invoke helpers, or carry a
            # credential if they reached a Git child.  The Git adapter must
            # construct its own environment instead of inheriting any of them.
            "GIT_DIR": str(repository.parent / "hostile-git-dir"),
            "GIT_WORK_TREE": str(repository.parent / "hostile-worktree"),
            "GIT_INDEX_FILE": str(repository.parent / "hostile-index"),
            "GIT_OBJECT_DIRECTORY": str(repository.parent / "hostile-objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(repository.parent / "hostile-alternates"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": str(helper),
            "GIT_EXEC_PATH": str(repository.parent / "hostile-exec-path"),
            "GIT_SSH": str(helper),
            "GIT_SSH_COMMAND": str(helper),
            "GIT_ASKPASS": str(helper),
            "SSH_ASKPASS": str(helper),
            "GIT_PAGER": str(helper),
            "PAGER": str(helper),
            "GIT_EDITOR": str(helper),
            "EDITOR": str(helper),
            "VISUAL": str(helper),
            "GIT_TRACE": "1",
            "GIT_TRACE_PACKET": "1",
            "HTTPS_PROXY": "http://secret.invalid:8080",
            "HTTP_PROXY": "http://secret.invalid:8080",
            "ALL_PROXY": "http://secret.invalid:8080",
            "CREDENTIAL_TOKEN": "never-inherit-this",
        }
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "forgemcp.server"], cwd=Path.cwd(),
            env={
                **os.environ,
                **hostile_environment,
                "FORGEMCP_WORKSPACE": str(repository),
                "FORGEMCP_LOG_LEVEL": "INFO",
            },
        )
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                listed = await session.list_tools()
                schemas = {
                    tool.name: (tool.inputSchema, tool.outputSchema)
                    for tool in listed.tools if tool.name.startswith("git__")
                }
                assert set(schemas) == {
                    "git__status", "git__diff", "git__log", "git__show_commit", "git__blame", "git__list_branches",
                }
                assert all(input_schema.get("additionalProperties") is False for input_schema, _ in schemas.values())
                assert all(
                    isinstance(output_schema, dict) and output_schema.get("additionalProperties") is False
                    for _, output_schema in schemas.values()
                )
                collector = McpToolCallCollector("git_fixture")
                status = payload(await collector.call(session, "git__status", {}))
                assert status["repository"] == "available" and status["untracked_count"] >= 1
                diff = payload(await collector.call(session, "git__diff", {"scope": "staged"}))
                assert diff["summary"]["scope"] == "staged"
                log = payload(await collector.call(session, "git__log", {"limit": 10}))
                commits = log["commits"]
                assert isinstance(commits, list) and commits
                oid = commits[0]["oid"]
                assert isinstance(oid, str)
                shown = payload(await collector.call(session, "git__show_commit", {"commit_oid": oid}))
                assert shown["commit"]["oid"] == oid
                blame = payload(await collector.call(session, "git__blame", {"path": "app/good_main.cpp", "start_line": 1, "end_line": 2}))
                assert blame["ranges"]
                branches = payload(await collector.call(session, "git__list_branches", {}))
                assert any(item["name"] == "topic" for item in branches["branches"])
                collector.complete_assertions({
                    "git__status", "git__diff", "git__log", "git__show_commit", "git__blame", "git__list_branches",
                })
                assert not (repository / "git-helper-invoked").exists()

    asyncio.run(exercise())
    assert _fixture_hashes() == committed_fixture_before
    assert _repository_state(repository) == state_before

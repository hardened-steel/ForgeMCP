"""Readable plain-text Git results independent of Apps and JSON formatting."""

from pydantic import BaseModel

from .models import (
    GitBlame,
    GitBlob,
    GitCommitDetails,
    GitConflicts,
    GitDiff,
    GitHistory,
    GitMutation,
    GitRefs,
    GitRemotes,
    GitStashes,
    GitStashResult,
    GitStatus,
)


def render_status(result: GitStatus) -> str:
    """Show branch identity and independent index/worktree states without abbreviating paths."""
    lines = [
        f"Branch: {result.branch or '(detached)'}",
        f"HEAD: {result.head or '(unborn)'}",
    ]
    if result.upstream:
        lines.append(f"Upstream: {result.upstream}; ahead={result.ahead}, behind={result.behind}")
    if result.operation:
        lines.append(f"Unfinished operation: {result.operation}")
    lines.extend(
        f"{entry.index_status}{entry.worktree_status} {entry.path}"
        + (f" (from {entry.original_path})" if entry.original_path else "")
        for entry in result.entries
    )
    if not result.entries:
        lines.append("Working tree clean.")
    return "\n".join(lines)


def render_diff(result: GitDiff) -> str:
    """Return the original patch, falling back to file metadata when no patch is available."""
    return result.patch or "\n".join(
        f"{file.status} {file.path} {file.old_mode} -> {file.new_mode}"
        for file in result.files
    ) or "No differences."


def render_history(result: GitHistory) -> str:
    """Keep original multiline messages and author/committer identities in each commit."""
    text = "\n\n".join(
        f"Commit: {commit.oid}\nParents: {', '.join(commit.parents) or '(root)'}\n"
        f"Author: {commit.author_name} <{commit.author_email}> {commit.author_time}\n"
        f"Committer: {commit.committer_name} <{commit.committer_email}> {commit.committer_time}\n"
        f"{commit.message}"
        for commit in result.commits
    ) or "No commits."
    if result.next_skip is not None:
        text += f"\nMore commits: skip={result.next_skip}."
    return text


def render_commit(result: GitCommitDetails) -> str:
    """Show the selected commit's original metadata and its native change patch."""
    history = GitHistory(revision=result.commit.oid, commits=[result.commit])
    return render_history(history) + "\n" + render_diff(result.diff)


def render_blame(result: GitBlame) -> str:
    """Show attributed source lines with complete commit IDs and saved source text."""
    return "\n".join(
        f"{line.line} | {line.oid} {line.author} | {line.text}"
        for line in result.lines
    ) or "No attributed lines."


def render_refs(result: GitRefs) -> str:
    """List complete reference names, current-branch markers, and upstreams."""
    return "\n".join(
        f"{'*' if ref.current else ' '} {ref.name} {ref.oid}"
        + (f" -> {ref.upstream}" if ref.upstream else "")
        for ref in result.refs
    ) or "No references."


def render_remotes(result: GitRemotes) -> str:
    """Show every supplied fetch and push address without additional remote queries."""
    return "\n".join(
        f"{remote.name}: fetch={', '.join(remote.fetch_urls)}; push={', '.join(remote.push_urls)}"
        for remote in result.remotes
    ) or "No remotes."


def render_stashes(result: GitStashes) -> str:
    """Keep stash selectors, full object identities, and saved messages visible."""
    return "\n".join(
        f"{stash.reference} {stash.oid} {stash.message}"
        for stash in result.stashes
    ) or "No stashes."


def render_blob(blob: GitBlob | None) -> str:
    """Distinguish missing and binary conflict stages while preserving textual contents."""
    if blob is None:
        return "(absent)"
    if blob.text is None:
        return f"(binary; oid={blob.oid}; mode={blob.mode})"
    return blob.text


def render_conflicts(result: GitConflicts) -> str:
    """Present the available base, ours, and theirs stages for each conflict."""
    lines = [f"Operation: {result.operation or '(none)'}"]
    for file in result.files:
        lines.append(str(file.path))
        for name in ("base", "ours", "theirs"):
            lines.append(f"{name}: {render_blob(getattr(file, name))}")
    return "\n".join(lines)


def render_mutation(result: GitMutation) -> str:
    """Report the actual command outcome, remaining conflicts, and process identifiers."""
    lines = [
        f"Result: {result.error or 'success'}",
        f"HEAD: {result.head_before} -> {result.head_after}",
    ]
    if result.operation:
        lines.append(f"Unfinished operation: {result.operation}")
    lines.extend(f"Conflict: {path}" for path in result.conflicts)
    lines.extend(
        f"{entry.index_status}{entry.worktree_status} {entry.path}"
        for entry in result.changes
    )
    lines.extend(result.updates)
    if result.synchronization_error:
        lines.append(result.synchronization_error)
    lines.append("Processes: " + ", ".join(map(str, result.process_ids)))
    return "\n".join(lines)


def render_stash_result(result: GitStashResult) -> str:
    """Include retained stash records beside the stash action's repository outcome."""
    records = GitStashes(stashes=result.stashes)
    return render_mutation(result) + "\n" + render_stashes(records)


RENDERERS = {
    GitStatus: render_status,
    GitDiff: render_diff,
    GitCommitDetails: render_commit,
    GitHistory: render_history,
    GitBlame: render_blame,
    GitRefs: render_refs,
    GitRemotes: render_remotes,
    GitStashes: render_stashes,
    GitStashResult: render_stash_result,
    GitConflicts: render_conflicts,
    GitMutation: render_mutation,
}


def render(result: BaseModel) -> str:
    """Dispatch a typed Git result to its concrete plain-text presentation."""
    renderer = RENDERERS.get(type(result))
    if renderer is None:
        raise TypeError(f"Unsupported Git result: {type(result).__name__}")
    return renderer(result)

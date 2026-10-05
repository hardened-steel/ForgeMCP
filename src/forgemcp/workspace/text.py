"""Plain-text trees, source lines, metadata, search hits, and mutation confirmations."""

from pathlib import PurePosixPath

from forgemcp.text import numbered_lines, timestamp

from .models import (
    TreeEntry,
    DirectoryTree,
    FilePaths,
    FileContent,
    FileInfo,
    SearchResult,
    FileWriteResult,
    FileEditResult,
    PathOperationResult,
)


def render_text(
    result: DirectoryTree | FilePaths | FileContent | FileInfo | SearchResult
    | FileWriteResult | FileEditResult | PathOperationResult,
) -> str:
    """Present workspace data as plain text without exposing provider resource links."""
    if isinstance(result, (FileWriteResult, FileEditResult, PathOperationResult)):
        return render_mutation(result)
    return render_listing(result)


def render_listing(
    result: DirectoryTree | FilePaths | FileContent | FileInfo | SearchResult,
) -> str:
    """Show the complete tree, source, metadata, or search records supplied by a read."""
    if isinstance(result, DirectoryTree):
        lines = [str(result.path)]

        def visit(entries: list[TreeEntry], prefix: str = "") -> None:
            """Draw tree branches and distinguish empty and unexpanded directories."""
            for position, entry in enumerate(entries, start=1):
                last = position == len(entries)
                label = PurePosixPath(entry.path.relative).name
                if entry.kind == "directory":
                    label += "/"
                    if entry.children is None:
                        label += " (not expanded)"
                elif entry.kind == "symlink":
                    label += " (symlink)"
                elif entry.kind == "other":
                    label += " (other)"
                lines.append(f"{prefix}{'└── ' if last else '├── '}{label}")
                if entry.children is not None:
                    visit(entry.children, prefix + ("    " if last else "│   "))

        visit(result.entries)
        if not result.entries:
            lines.append("(empty)")
        return "\n".join(lines)
    if isinstance(result, FilePaths):
        lines = [f"Found {len(result.paths)} files."]
        metadata = {item.path: item for item in result.files}
        for path in result.paths:
            lines.append(str(path))
            if item := metadata.get(path):
                lines.append(
                    f"  Size: {item.size_bytes} bytes; modified: {timestamp(item.modified_at)}"
                )
        return "\n".join(lines)
    if isinstance(result, FileContent):
        return numbered_lines(result.text, result.start_line)
    if isinstance(result, FileInfo):
        return "\n".join(
            [
                str(result.path),
                f"  Size: {result.size_bytes} bytes",
                f"  Created: {timestamp(result.created_at)}",
                f"  Modified: {timestamp(result.modified_at)}",
                f"  Owner: {result.owner if result.owner is not None else 'Unavailable'}",
            ]
        )
    lines = [
        f"Returned {len(result.matches)} of {result.matches_count} matching lines in "
        f"{len({match.path for match in result.matches})} displayed files.",
        f"Matches truncated: {result.matches_truncated}.",
        f"Skipped files: {result.skipped_files_count}.",
    ]
    previous = None
    for match in result.matches:
        if match.path != previous:
            lines.extend(["", str(match.path)])
            previous = match.path
        lines.append(f"  {match.line} | {match.text}")
    return "\n".join(lines)


def render_mutation(result: FileWriteResult | FileEditResult | PathOperationResult) -> str:
    """Confirm the actual mutation action without copying linked provider data."""
    if isinstance(result, FileWriteResult):
        if result.action == "created":
            return f"Created {result.path}. Added {result.lines_added} lines."
        return (
            f"Overwrote {result.path}. Removed {result.lines_removed} lines; "
            f"added {result.lines_added} lines."
        )
    if isinstance(result, FileEditResult):
        noun = "occurrence" if result.replacements == 1 else "occurrences"
        return f"Updated {result.path}. Replaced {result.replacements} {noun}."
    messages = {
        "moved": f"Moved {result.source} to {result.path}.",
        "deleted": f"Deleted {result.path}.",
        "already_exists": f"Directory {result.path} already exists.",
        "created": f"Created directory {result.path}.",
    }
    return messages[result.action]

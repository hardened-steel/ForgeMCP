"""Thin Workspace notification adapter for CMake-owned input freshness."""

from forgemcp.workspace.models import (
    FileEditResult,
    FileWriteResult,
    PathOperationResult,
)
from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.service import ResultProvider

from .file_api import WorkspaceChange
from .service import CMakeService


class CMakeProvider(ResultProvider[WorkspaceChange]):
    """Delegate mutation capture, verification, and failed-operation cleanup to CMake."""

    TOOLS = (
        "workspace_write_file",
        "workspace_move",
        "workspace_edit_file",
        "workspace_mkdir",
        "workspace_delete",
    )

    def __init__(self, service: CMakeService) -> None:
        """Use the service wired by the composition root without owning filesystem policy."""
        self.service = service

    async def before_workspace_write_file(
        self,
        call_id: str,
        path: WorkspacePath,
        text: str,
    ) -> WorkspaceChange | None:
        """Capture dependencies affected by replacing one file."""
        return await self.service.begin_workspace((path,))

    async def after_workspace_write_file(
        self,
        call_id: str,
        context: WorkspaceChange,
        result: FileWriteResult,
    ) -> None:
        """Verify the completed write without adding a result resource."""
        await self.service.finish_workspace(context)

    async def before_workspace_edit_file(
        self,
        call_id: str,
        path: WorkspacePath,
        old_text: str,
        new_text: str,
        replace_all: bool,
    ) -> WorkspaceChange | None:
        """Capture dependencies affected by an exact file edit."""
        return await self.service.begin_workspace((path,))

    async def after_workspace_edit_file(
        self,
        call_id: str,
        context: WorkspaceChange,
        result: FileEditResult,
    ) -> None:
        """Verify the completed edit without adding a result resource."""
        await self.service.finish_workspace(context)

    async def before_workspace_move(
        self,
        call_id: str,
        source: WorkspacePath,
        destination: WorkspacePath,
    ) -> WorkspaceChange | None:
        """Capture known dependencies under both source and destination."""
        return await self.service.begin_workspace((source, destination))

    async def after_workspace_move(
        self,
        call_id: str,
        context: WorkspaceChange,
        result: PathOperationResult,
    ) -> None:
        """Verify a file or directory move without scanning the project."""
        await self.service.finish_workspace(context)

    async def before_workspace_delete(
        self,
        call_id: str,
        path: WorkspacePath,
    ) -> WorkspaceChange | None:
        """Capture known dependencies beneath the deletion target."""
        return await self.service.begin_workspace((path,))

    async def after_workspace_delete(
        self,
        call_id: str,
        context: WorkspaceChange,
        result: PathOperationResult,
    ) -> None:
        """Verify deletion without adding a result resource."""
        await self.service.finish_workspace(context)

    async def before_workspace_mkdir(
        self,
        call_id: str,
        path: WorkspacePath,
    ) -> WorkspaceChange | None:
        """Capture the path kind for conservative glob invalidation."""
        return await self.service.begin_workspace((path,))

    async def after_workspace_mkdir(
        self,
        call_id: str,
        context: WorkspaceChange,
        result: PathOperationResult,
    ) -> None:
        """Verify actual directory creation, ignoring an already existing directory."""
        await self.service.finish_workspace(context)

    async def error(self, call_id: str, context: WorkspaceChange) -> None:
        """Check partial changes and release the mutation lock after failure or cancellation."""
        await self.service.finish_workspace(context)

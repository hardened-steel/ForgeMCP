"""Detached local operator programs and process identities used by the admin Hub."""

from __future__ import annotations

import asyncio
import os
import subprocess
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import psutil

from .errors import ProcessStartError

reapers: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class ProcessIdentity:
    """Identify one OS process across PID reuse without retaining its environment."""

    pid: int
    started_at: float

    @classmethod
    def current(cls) -> ProcessIdentity:
        """Capture the current process birth time for an authenticated IPC registration."""
        process = psutil.Process()
        return cls(process.pid, process.create_time())

    def is_alive(self) -> bool:
        """Check both PID and birth time; a disconnected live runtime still owns the Hub."""
        try:
            process = psutil.Process(self.pid)
            return (
                process.create_time() == self.started_at
                and process.is_running()
                and process.status() != psutil.STATUS_ZOMBIE
            )
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            # Loss of observability is not evidence that the runtime ended.
            return True


async def start_detached(
    executable: str | Path,
    arguments: Sequence[str],
    *,
    cwd: Path,
) -> int:
    """Start an independent Hub without inheriting MCP streams or transcript ownership."""
    def start() -> subprocess.Popen:
        """Perform the short native spawn without transferring parent stream handles."""
        options: dict[str, object] = {}
        if os.name == "nt":
            options["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            options["start_new_session"] = True
        try:
            process = subprocess.Popen(
                [str(executable), *arguments],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                **options,
            )
        except OSError as error:
            raise ProcessStartError("Cannot start the local admin Hub.") from error
        return process

    process = await asyncio.to_thread(start)

    async def reap() -> None:
        """Reap an exited child without blocking interpreter shutdown on a live Hub."""
        while process.poll() is None:
            await asyncio.sleep(1)

    task = asyncio.create_task(reap())
    reapers.add(task)
    task.add_done_callback(reapers.discard)
    return process.pid


async def open_browser(url: str) -> None:
    """Open an operator-requested local panel without placing its capability in logs."""
    if not await asyncio.to_thread(webbrowser.open, url, new=2):
        raise ProcessStartError("The system could not open the admin panel in a browser.")

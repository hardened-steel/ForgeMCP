"""Owned LLDB-DAP connections, framed messages, and executable version probes."""

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, TypedDict

from pydantic import JsonValue

from forgemcp.process.errors import ProcessError
from forgemcp.process.models import ProcessEncoding, ProcessTimeout
from forgemcp.process.service import ProcessService
from forgemcp.process.service import ProcessSession

from ..errors import ToolCommandError, ToolParserError
from ..spec import ToolInfo, ToolKind, ToolSpec


@dataclass(frozen=True)
class DapRequest:
    """A DAP request whose sequence is assigned by its owning debug session."""

    seq: int
    command: str
    arguments: JsonValue = None


@dataclass(frozen=True)
class DapResponse:
    """A success or failure response correlated with a DAP request sequence."""

    seq: int
    request_seq: int
    command: str
    success: bool
    body: JsonValue = None
    message: str | None = None


@dataclass(frozen=True)
class DapEvent:
    """An unsolicited DAP event interpreted by the owning debug session."""

    seq: int
    event: str
    body: JsonValue = None


type DapMessage = DapRequest | DapResponse | DapEvent


def valid_sequence(value: object) -> bool:
    """Accept nonnegative integer protocol identities while excluding booleans."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_message(value: object) -> DapMessage:
    """Validate a DAP envelope without interpreting operation-specific payloads."""
    if (
        not isinstance(value, dict)
        or not valid_sequence(value.get("seq"))
    ):
        raise ToolParserError("lldb-dap returned an invalid message sequence.")
    sequence = value["seq"]
    kind = value.get("type")
    if kind in ("request", "response"):
        command = value.get("command")
        if not isinstance(command, str) or not command:
            raise ToolParserError("lldb-dap returned an invalid command name.")
        if kind == "request":
            return DapRequest(sequence, command, value.get("arguments"))
        request_sequence = value.get("request_seq")
        if (
            not valid_sequence(request_sequence)
            or not isinstance(value.get("success"), bool)
            or ("message" in value and not isinstance(value["message"], str))
        ):
            raise ToolParserError("lldb-dap returned an invalid response envelope.")
        return DapResponse(
            sequence,
            request_sequence,
            command,
            value["success"],
            value.get("body"),
            value.get("message"),
        )
    if kind == "event":
        event = value.get("event")
        if isinstance(event, str) and event:
            return DapEvent(sequence, event, value.get("body"))
    raise ToolParserError("lldb-dap returned an invalid message envelope.")


def reject_constant(value: str) -> None:
    """Reject non-finite values which are outside the JSON protocol."""
    raise ValueError(f"Non-finite JSON constant: {value}.")


def content_length(header: bytes, maximum: int) -> int:
    """Validate the framing header and bound its advertised UTF-8 body length."""
    lengths = []
    for field in header.decode("ascii").split("\r\n"):
        key, separator, value = field.partition(":")
        if not separator:
            raise ValueError("Malformed DAP header.")
        if key.strip().lower() == "content-length":
            lengths.append(value.strip())
    if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
        raise ValueError("Invalid DAP content length.")
    length = int(lengths[0])
    if length > maximum:
        raise ToolParserError(f"DAP message exceeds the {maximum}-byte limit for a body.")
    return length


class Connection:
    """Exchange bounded DAP frames through one process-owned output reader."""

    MAX_HEADER_BYTES = 8192
    MAX_MESSAGE_BYTES = 16 * 1024 * 1024

    def __init__(self, session: ProcessSession) -> None:
        """Retain the owned process and serialize complete outgoing frames."""
        self.session = session
        self.write_lock = asyncio.Lock()
        self.claimed = False

    @property
    def process_id(self) -> int:
        """Expose the ProcessService identifier for transcript inspection."""
        return self.session.process_id

    async def close(self) -> None:
        """Close the physical process transport without interpreting target ownership."""
        await self.session.close()

    async def send(self, message: DapMessage) -> None:
        """Validate and atomically queue a UTF-8 byte-counted DAP frame."""
        # Both LSP and DAP require these explicit UTF-8 framing steps.
        # pylint: disable=duplicate-code
        if isinstance(message, DapRequest):
            kind = "request"
        elif isinstance(message, DapResponse):
            kind = "response"
        elif isinstance(message, DapEvent):
            kind = "event"
        else:
            raise ToolParserError("Cannot send an unsupported DAP message.")
        value = {"type": kind, **asdict(message)}
        for key in ("arguments", "body", "message"):
            if value.get(key) is None:
                value.pop(key, None)
        parse_message(value)
        try:
            body = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(body) > self.MAX_MESSAGE_BYTES:
                raise ToolParserError(
                    "Outgoing DAP message exceeds the "
                    f"{self.MAX_MESSAGE_BYTES}-byte limit for a body."
                )
            header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            async with self.write_lock:
                await self.session.write_stdin((header + body).decode("latin_1"))
        except (TypeError, ValueError, UnicodeError) as error:
            raise ToolParserError("Cannot serialize a DAP message.") from error
        except ProcessError as error:
            raise ToolCommandError(
                "Cannot send a message to lldb-dap.",
                process_id=self.process_id,
            ) from error

    async def messages(self) -> AsyncGenerator[DapMessage]:
        """Decode fragmented stdout frames while draining stderr through ProcessService."""
        # Content-Length framing deliberately matches the existing LSP wire format.
        # pylint: disable=duplicate-code
        if self.claimed:
            raise ToolCommandError("lldb-dap messages already have a reader.")
        self.claimed = True
        buffer = bytearray()
        length = None
        try:
            async for chunk in self.session.output():
                if chunk.stream != "stdout":
                    continue
                buffer.extend(chunk.text.encode("latin_1"))
                while True:
                    if length is None:
                        end = buffer.find(b"\r\n\r\n")
                        if end < 0:
                            if len(buffer) > self.MAX_HEADER_BYTES:
                                raise ToolParserError(
                                    f"DAP header exceeds the {self.MAX_HEADER_BYTES}-byte limit."
                                )
                            break
                        if end > self.MAX_HEADER_BYTES:
                            raise ToolParserError(
                                f"DAP header exceeds the {self.MAX_HEADER_BYTES}-byte limit."
                            )
                        length = content_length(
                            bytes(buffer[:end]),
                            self.MAX_MESSAGE_BYTES,
                        )
                        del buffer[:end + 4]
                    if len(buffer) < length:
                        break
                    body = bytes(buffer[:length])
                    del buffer[:length]
                    length = None
                    yield parse_message(
                        json.loads(
                            body.decode("utf-8"),
                            parse_constant=reject_constant,
                        ),
                    )
            if buffer or length is not None:
                raise ToolParserError("lldb-dap closed an incomplete message.")
            code = await self.session.wait()
            if code != 0:
                raise ToolCommandError(
                    f"lldb-dap exited with code {code}.",
                    process_id=self.process_id,
                )
        except (ValueError, UnicodeError, RecursionError) as error:
            raise ToolParserError("lldb-dap returned malformed protocol data.") from error
        except ProcessError as error:
            raise ToolCommandError(
                "lldb-dap process communication failed.",
                process_id=self.process_id,
            ) from error


class Connect(Protocol):  # pylint: disable=too-few-public-methods
    """The typed factory for an owned LLDB-DAP standard-stream connection."""

    def __call__(self, cwd: Path) -> AbstractAsyncContextManager[Connection]:
        """Open a process connection using the selected toolset environment."""
        ...


class Methods(TypedDict):
    """The typed operations supported by the bound lldb-dap executable."""

    connect: Connect
    version: Callable[[], Awaitable[str]]


def create_spec(
    path: Path,
    processes: ProcessService,
    environment: Mapping[str, str] | None = None,
    inherit_environment: bool = True,
) -> ToolSpec:
    """Bind the resolved lldb-dap executable to process execution and environment settings."""
    path = path.resolve()
    async def version() -> str:
        """Drain a bounded version probe and extract the LLVM release identifier."""
        # Version probes deliberately retain each tool's explicit process policy.
        # pylint: disable=duplicate-code
        output = {"stdout": "", "stderr": ""}
        session = None
        try:
            async with await processes.launch(
                path,
                ("--version",),
                env=environment,
                inherit_environment=inherit_environment,
                timeout=ProcessTimeout(total=15),
            ) as session:
                await session.close_stdin()
                async for chunk in session.output():
                    remaining = 4096 - len(output[chunk.stream])
                    output[chunk.stream] += chunk.text[:remaining]
                code = await session.wait()
                if code != 0:
                    raise ToolCommandError(
                        f"lldb-dap.version failed (exit {code}).",
                        process_id=session.process_id,
                    )
        except ProcessError as error:
            raise ToolCommandError(
                "Cannot read lldb-dap version within its 15-second limit. "
                "The operator should check the selected executable and toolset environment.",
                process_id=session.process_id if session is not None else None,
            ) from error
        for text in output.values():
            if match := re.search(
                r"(?:LLVM|lldb-dap|lldb) version ([0-9][^\s]*)",
                text,
                re.IGNORECASE,
            ):
                return match.group(1)
        raise ToolCommandError(
            "Cannot parse lldb-dap version from the first 4096 characters of each stream. "
            f"Inspect process_get(process_id={session.process_id}).",
            process_id=session.process_id,
        )

    @asynccontextmanager
    async def connect(cwd: Path) -> AsyncGenerator[Connection]:
        """Own an adapter process with reversible bytes and no session-wide timeout."""
        # Each ToolSpec explicitly owns its process environment and stream policy.
        # pylint: disable=duplicate-code
        session = None
        try:
            async with await processes.launch(
                path,
                (),
                cwd=cwd,
                env=environment,
                inherit_environment=inherit_environment,
                encoding=ProcessEncoding(driver="latin_1", errors="strict"),
                timeout=ProcessTimeout(),
            ) as session:
                yield Connection(session)
        except ProcessError as error:
            raise ToolCommandError(
                "Cannot run lldb-dap for the selected toolset_id/adapter. "
                "Call debugger_adapters(); the operator should check the selected "
                "executable and toolset environment before opening another target.",
                process_id=session.process_id if session is not None else None,
            ) from error

    methods: Methods = {
        "connect": connect,
        "version": version,
    }
    return ToolSpec(INFO.name, INFO.kind, path, methods)


INFO = ToolInfo(name='lldb-dap', kind=ToolKind.DEBUGGER, create_spec=create_spec)

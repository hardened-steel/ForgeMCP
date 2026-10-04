"""Typed methods for clangd."""

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
from forgemcp.process.service import ProcessService, ProcessSession

from ..errors import ToolCommandError, ToolParserError
from ..spec import ToolInfo, ToolKind, ToolSpec


type LspId = int | str


@dataclass(frozen=True)
class LspRequest:
    """A JSON-RPC request with an identifier, method, and optional parameters."""

    id: LspId
    method: str
    params: JsonValue = None


@dataclass(frozen=True)
class LspNotification:
    """A JSON-RPC notification without a response identifier."""

    method: str
    params: JsonValue = None


@dataclass(frozen=True)
class LspResponse:
    """A successful JSON-RPC response with its request identifier."""

    id: LspId
    result: JsonValue


@dataclass(frozen=True)
class LspErrorResponse:
    """A JSON-RPC failure with its identifier, code, message, and optional data."""

    id: LspId | None
    code: int
    message: str
    data: JsonValue = None


type LspMessage = LspRequest | LspNotification | LspResponse | LspErrorResponse


def parse_message(value: object) -> LspMessage:
    """Validate the JSON-RPC envelope without interpreting a language operation."""
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise ToolParserError("clangd returned an invalid JSON-RPC envelope.")
    identifier = value.get("id")
    valid_id = type(identifier) in (int, str)
    if "method" in value:
        if (
            not isinstance(value["method"], str)
            or not value["method"]
            or "result" in value
            or "error" in value
            or ("params" in value and not isinstance(value["params"], (dict, list)))
        ):
            raise ToolParserError("clangd returned an invalid request or notification.")
        if "id" not in value:
            return LspNotification(value["method"], value.get("params"))
        if valid_id:
            return LspRequest(identifier, value["method"], value.get("params"))
    elif "result" in value and "error" not in value and valid_id:
        return LspResponse(identifier, value["result"])
    elif "error" in value and "result" not in value and "id" in value:
        error = value["error"]
        if (
            (valid_id or identifier is None)
            and isinstance(error, dict)
            and type(error.get("code")) is int
            and isinstance(error.get("message"), str)
        ):
            return LspErrorResponse(
                identifier,
                error["code"],
                error["message"],
                error.get("data"),
            )
    raise ToolParserError("clangd returned an invalid JSON-RPC response or identifier.")


def reject_constant(value: str) -> None:
    """Reject nonfinite numeric constants while decoding JSON messages."""
    raise ValueError("Non-finite JSON number.")


class Connection:
    """A single-reader framed connection; process ownership stays in this module."""

    def __init__(self, session: ProcessSession) -> None:
        """Bind the process session and initialize single-reader and serialized-write state."""
        self._session = session
        self.write_lock = asyncio.Lock()
        self.claimed = False

    @property
    def process_id(self) -> int:
        """Return the process identifier for this framed connection."""
        return self._session.process_id

    async def close(self) -> None:
        """Close the transport, leaving process cleanup inside the tool module."""
        await self._session.close()

    async def send(self, message: LspMessage) -> None:
        """Validate and serialize a message with a UTF-8 byte-counted LSP header."""
        value = {"jsonrpc": "2.0", **asdict(message)}
        if isinstance(message, LspErrorResponse):
            value = {
                "jsonrpc": "2.0",
                "id": message.id,
                "error": {
                    "code": message.code,
                    "message": message.message,
                    "data": message.data,
                },
            }
        elif isinstance(message, (LspRequest, LspNotification)) and message.params is None:
            value.pop("params")
        parse_message(value)
        try:
            body = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            async with self.write_lock:
                await self._session.write_stdin((header + body).decode("latin_1"))
        except (TypeError, ValueError, UnicodeError) as error:
            raise ToolParserError("Cannot serialize an LSP message.") from error
        except ProcessError as error:
            raise ToolCommandError(
                "Cannot send an LSP message to clangd.",
                process_id=self.process_id,
            ) from error

    async def messages(self) -> AsyncGenerator[LspMessage]:
        """Yield validated JSON-RPC messages from fragmented stdout frames with one reader."""
        if self.claimed:
            raise ToolCommandError("clangd messages already have a reader.")
        self.claimed = True
        buffer = bytearray()
        length = None
        try:
            async for chunk in self._session.output():
                if chunk.stream != "stdout":
                    continue
                buffer.extend(chunk.text.encode("latin_1"))
                while True:
                    if length is None:
                        end = buffer.find(b"\r\n\r\n")
                        if end < 0:
                            break
                        headers = bytes(buffer[:end]).decode("ascii")
                        del buffer[:end + 4]
                        lengths = []
                        for header in headers.split("\r\n"):
                            key, separator, value = header.partition(":")
                            if not separator:
                                raise ValueError("Malformed LSP header.")
                            if key.strip().lower() == "content-length":
                                lengths.append(value.strip())
                        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
                            raise ValueError("Invalid LSP content length.")
                        length = int(lengths[0])
                    if len(buffer) < length:
                        break
                    body = bytes(buffer[:length])
                    del buffer[:length]
                    length = None
                    yield parse_message(
                        json.loads(body.decode("utf-8"), parse_constant=reject_constant),
                    )
            if buffer or length is not None:
                raise ToolParserError("clangd closed an incomplete LSP message.")
            code = await self._session.wait()
            if code != 0:
                raise ToolCommandError(
                    f"clangd exited with code {code}.",
                    process_id=self.process_id,
                )
        except (ValueError, UnicodeError, RecursionError) as error:
            raise ToolParserError("clangd returned malformed LSP data.") from error
        except ProcessError as error:
            raise ToolCommandError(
                "clangd process communication failed.",
                process_id=self.process_id,
            ) from error


class Connect(Protocol):
    """The callable contract for opening an owned clangd connection."""

    def __call__(
        self,
        project: Path,
        compilation_database_directory: Path,
    ) -> AbstractAsyncContextManager[Connection]:
        """Open a connection for the project and compilation database directory."""
        ...


class Methods(TypedDict):
    """The typed operations supported by the bound clangd executable."""

    version: Callable[[], Awaitable[str]]
    connect: Connect


def create_spec(
    path: Path,
    processes: ProcessService,
    environment: Mapping[str, str] | None = None,
    inherit_environment: bool = True,
) -> ToolSpec:
    """Bind the resolved clangd executable to process execution and environment settings."""
    path = path.resolve()

    async def version() -> str:
        """Run a bounded version probe, drain both output streams, and parse the clangd banner."""
        output = {"stdout": "", "stderr": ""}
        try:
            async with await processes.launch(
                path,
                ('--version',),
                env=environment,
                inherit_environment=inherit_environment,
                timeout=ProcessTimeout(total=15),
            ) as session:
                await session.close_stdin()
                async for chunk in session.output():
                    # Only the short version banner is needed; drain the rest.
                    remaining = 4096 - len(output[chunk.stream])
                    output[chunk.stream] += chunk.text[:remaining]
                code = await session.wait()
                if code != 0:
                    raise ToolCommandError(f"{INFO.name}.version failed (exit {code}).")
        except ProcessError as error:
            raise ToolCommandError(f"Cannot read {INFO.name} version.") from error
        for text in output.values():
            if match := re.search(
                'clangd version ([0-9][^\\s]*)',
                text,
                re.IGNORECASE | re.MULTILINE,
            ):
                return match.group(1)
        raise ToolParserError(f"Cannot parse {INFO.name} version.")

    @asynccontextmanager
    async def connect(
        project: Path,
        compilation_database_directory: Path,
    ) -> AsyncGenerator[Connection]:
        """Launch clangd against an existing compilation database and own its connection
        lifetime.
        """
        if not (compilation_database_directory / "compile_commands.json").is_file():
            raise ToolCommandError("clangd requires an existing compilation database.")
        try:
            async with await processes.launch(
                path,
                (
                    f"--compile-commands-dir={compilation_database_directory}",
                    "--background-index",
                ),
                cwd=project,
                env=environment,
                inherit_environment=inherit_environment,
                encoding=ProcessEncoding(driver="latin_1", errors="strict"),
                timeout=ProcessTimeout(),
            ) as session:
                yield Connection(session)
        except ProcessError as error:
            raise ToolCommandError("Cannot run clangd.") from error

    methods: Methods = {
        "version": version,
        "connect": connect,
    }
    return ToolSpec(INFO.name, INFO.kind, path, methods)


INFO = ToolInfo(name='clangd', kind=ToolKind.LANGUAGE_SERVER, create_spec=create_spec)

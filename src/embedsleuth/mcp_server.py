"""Local STDIO adapter. MCP never receives SSH endpoints, paths or commands."""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import signal
import sys

import anyio
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams, CallToolResult, ListToolsResult, PaginatedRequestParams,
    TextContent, Tool, ToolAnnotations,
)
from pydantic import BaseModel, ValidationError

from embedsleuth import __version__
from embedsleuth.config import ConfigurationError, load_configuration
from embedsleuth.core import DiagnosticCore, SessionError
from embedsleuth.storage import RunStore, StorageError, StoreBusyError, sanitize
from embedsleuth.targets import TargetRegistry
from embedsleuth.tools import TOOL_REQUEST_MODELS, tool_input_schemas, validate_arguments
from embedsleuth.transport import ParamikoTransport


LOG = logging.getLogger(__name__)
REMOTE_TOOLS = frozenset({"inspect_target", "inspect_i2c_environment", "read_device_identity"})
DESCRIPTIONS = {
    "get_component_profile": "Load the reviewed component profile and datasheet references; no device access.",
    "start_diagnosis": "Start a session for a locally registered target_id. Default read_only; passive skips sensor IO.",
    "inspect_target": "Inspect the registered SSH target and bind board, boot and verified host key evidence.",
    "inspect_i2c_environment": "Inspect the registered I2C adapter and prerequisites without sensor transfers.",
    "read_device_identity": "Read only the reviewed identity register after core prerequisites pass. Reuse operation_id for replay; never automatically retry errors.",
    "evaluate_findings": "Evaluate deterministic rules from this run's recorded evidence; no device access.",
    "finalize_report": "Finalize recorded evidence and optional AI hypotheses with validated evidence references.",
    "get_report": "Recover and retrieve the current report. Partial/interrupted does not mean sensor failure.",
    "cancel_diagnosis": "Persist cancellation and return the last durable report. Remote completion may remain unknown.",
}
OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "result", "error"],
    "properties": {
        "schema_version": {"const": "1.0"},
        "result": {},
        "error": {"anyOf": [{"type": "null"}, {
            "type": "object", "additionalProperties": False,
            "required": ["code", "message"],
            "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
        }]},
    },
}


def _json_value(value):
    if isinstance(value, BaseModel):
        return json.loads(value.model_dump_json(warnings="error"))
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _response(value=None, *, code: str | None = None, message: str = "") -> CallToolResult:
    result = _json_value(value)
    error = {"code": code, "message": message} if code else None
    if isinstance(result, dict) and result.get("status") in {"error", "blocked", "unsupported", "timeout"}:
        error = {key: result["error"][key] for key in ("code", "message")}
    body = sanitize({"schema_version": "1.0", "result": result, "error": error})
    encoded = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 1024 * 1024:
        return _response(code="resource_limit", message="Response exceeds the MCP result budget; inspect local evidence")
    return CallToolResult(content=[TextContent(type="text", text=encoded)],
                          structured_content=body, is_error=error is not None)


class MCPRuntime:
    """Bounded sync workers; cancellation markers survive handler cancellation.

    A separate control worker leaves cancel_diagnosis available when all four
    ordinary workers are occupied. There is no unbounded executor queue.
    The store remains open until all admitted workers have finished committing.
    """

    def __init__(self, core: DiagnosticCore):
        self.core = core
        self._workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="embedsleuth")
        self._control = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embedsleuth-cancel")
        self._active: dict[asyncio.Future, tuple[str, str | None]] = {}
        self._closing = False

    def _cancel(self, run_id: str | None) -> None:
        if run_id is None:
            return
        try:
            self.core.store.request_cancel(run_id)
        except (OSError, ValueError):
            # Never log raw exceptions: paths and attacker-controlled input may
            # be included. A pending intent still triggers durable crash recovery.
            LOG.error("Could not persist diagnostic cancellation; review local evidence")

    async def call(self, name: str, arguments: dict | None) -> CallToolResult:
        if name not in TOOL_REQUEST_MODELS or (arguments is not None and not isinstance(arguments, dict)):
            return _response(code="invalid_request", message="Unknown tool or invalid tool arguments")
        arguments = {} if arguments is None else arguments
        try:
            if len(json.dumps(arguments, allow_nan=False).encode("utf-8")) > 262144:
                return _response(code="resource_limit", message="Tool arguments exceed the MCP request budget")
            request = validate_arguments(name, arguments)
            # Detach input before another task or caller can mutate it.
            arguments = json.loads(request.model_dump_json())
        except (ValueError, TypeError, RecursionError):
            return _response(code="invalid_request", message="Arguments do not match the diagnostic tool schema")
        if self._closing:
            return _response(code="resource_limit", message="MCP server is shutting down")
        control = name == "cancel_diagnosis"
        occupied = sum((tool == "cancel_diagnosis") == control for tool, _ in self._active.values())
        if occupied >= (1 if control else 4):
            return _response(code="resource_limit", message="MCP worker capacity is occupied; no call was queued")
        future = asyncio.get_running_loop().run_in_executor(
            self._control if control else self._workers, self.core.call, name, arguments,
        )
        run_id = getattr(request, "run_id", None)
        self._active[future] = (name, run_id)

        def finished(done):
            self._active.pop(done, None)
            # Observe exceptions even if the requesting client has disconnected.
            if not done.cancelled():
                done.exception()

        future.add_done_callback(finished)
        try:
            value = await asyncio.shield(future)
            return _response(value)
        except asyncio.CancelledError:
            if name in REMOTE_TOOLS:
                self._cancel(run_id)
            raise
        except SessionError as exc:
            allowed = {"policy_denied", "target_busy", "state_changed", "remote_state_unknown"}
            return _response(code=exc.code if exc.code in allowed else "invalid_request",
                             message="Diagnostic session policy blocked the request")
        except (StoreBusyError, BlockingIOError):
            return _response(code="target_busy", message="Diagnostic state is busy")
        except FileNotFoundError:
            return _response(code="invalid_request", message="Requested diagnostic resource is unavailable")
        except (StorageError, OSError):
            return _response(code="storage_error", message="Diagnostic evidence storage is unavailable or unsafe")
        except (ValidationError, ValueError):
            return _response(code="invalid_request", message="Unknown diagnostic resource or invalid evidence references")
        except Exception:
            LOG.error("Unexpected diagnostic tool failure; review local evidence")
            return _response(code="storage_error", message="Diagnostic tool failed; review local evidence before continuing")

    async def close(self) -> None:
        self._closing = True
        # The SDK uses AnyIO cancellation scopes. Shield the commit/drain phase
        # from EOF, SIGINT, SIGTERM and repeated cancellation of request handlers.
        with anyio.CancelScope(shield=True):
            active = list(self._active.items())
            for _, (name, run_id) in active:
                if name in REMOTE_TOOLS:
                    self._cancel(run_id)
            if active:
                await asyncio.gather(*(asyncio.shield(future) for future, _ in active), return_exceptions=True)
            self._workers.shutdown(wait=True)
            self._control.shutdown(wait=True)


def create_server(core: DiagnosticCore) -> Server:
    runtime = MCPRuntime(core)

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield {}
        finally:
            await runtime.close()

    async def list_tools(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=[
            Tool(name=name, description=DESCRIPTIONS[name], input_schema=schema,
                 output_schema=OUTPUT_SCHEMA, annotations=ToolAnnotations(
                     read_only_hint=name == "get_component_profile",
                     destructive_hint=False,
                     idempotent_hint=name in {"get_component_profile", "read_device_identity", "evaluate_findings", "get_report", "cancel_diagnosis"},
                     open_world_hint=name in REMOTE_TOOLS,
                 ))
            for name, schema in tool_input_schemas().items()
        ])

    async def call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        return await runtime.call(params.name, params.arguments)

    return Server("EmbedSleuth", version=__version__, lifespan=lifespan,
                  on_list_tools=list_tools, on_call_tool=call_tool)


async def serve(core: DiagnosticCore) -> None:
    server = create_server(core)
    loop = asyncio.get_running_loop()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    # Scope cancellation is shieldable during the core's final commit/drain.
    with anyio.CancelScope() as shutdown:
        for sig in previous:
            loop.add_signal_handler(sig, shutdown.cancel)
        try:
            async with _stdio_pipes(shutdown.cancel) as (stdin, stdout):
                async with stdio_server(stdin=stdin, stdout=stdout) as (reader, writer):
                    await server.run(reader, writer, server.create_initialization_options())
        finally:
            for sig, handler in previous.items():
                loop.remove_signal_handler(sig)
                signal.signal(sig, handler)


@asynccontextmanager
async def _stdio_pipes(disconnected):
    """Cancellable Linux pipes, including a full output pipe at host shutdown.

    The SDK's AsyncFile streams wait in non-cancellable worker threads. Use
    asyncio pipe transports while retaining official framing and dispatch.
    Redirect actual fd 0 to /dev/null and fd 1 to stderr so stray handler or
    child IO cannot read requests or contaminate protocol output.
    """
    loop = asyncio.get_running_loop()
    sys.stdout.flush()
    saved = {fd: os.dup(fd) for fd in (0, 1)}
    blocking = {fd: os.get_blocking(duplicate) for fd, duplicate in saved.items()}
    input_pipe = os.fdopen(os.dup(saved[0]), "rb", buffering=0)
    output_pipe = os.fdopen(os.dup(saved[1]), "wb", buffering=0)
    input_transport = output_transport = writer = None
    try:
        with open(os.devnull, "rb") as null:
            os.dup2(null.fileno(), 0)
        os.dup2(2, 1)
        reader = asyncio.StreamReader(limit=524288)
        input_transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), input_pipe)
        protocol = asyncio.streams.FlowControlMixin(loop=loop)
        output_transport, _ = await loop.connect_write_pipe(lambda: protocol, output_pipe)
        writer = asyncio.StreamWriter(output_transport, protocol, None, loop)

        class Lines:
            async def __aiter__(self):
                while True:
                    try:
                        line = await reader.readline()
                        if not line:
                            return
                        decoded = line.decode("utf-8")
                    except (ValueError, UnicodeError):
                        LOG.error("Invalid or oversized MCP input; closing the local connection")
                        return
                    yield decoded

        class Output:
            async def write(self, value):
                try:
                    writer.write(value.encode("utf-8"))
                    await writer.drain()
                except (ConnectionError, OSError):
                    disconnected()
                    raise anyio.ClosedResourceError from None

            async def flush(self):
                try:
                    await writer.drain()
                except (ConnectionError, OSError):
                    disconnected()
                    raise anyio.ClosedResourceError from None

        yield Lines(), Output()
    finally:
        if output_transport is not None:
            # Do not wait for a host that has stopped reading the wire.
            if not output_transport.is_closing():
                output_transport.abort()
        else:
            output_pipe.close()
        if input_transport is not None:
            input_transport.close()
        else:
            input_pipe.close()
        sys.stdout.flush()
        for fd, duplicate in saved.items():
            os.set_blocking(duplicate, blocking[fd])
            os.dup2(duplicate, fd)
            os.close(duplicate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EmbedSleuth local STDIO MCP server")
    parser.add_argument("--config", required=True, type=Path, help="Absolute path to trusted local JSON configuration")
    args = parser.parse_args(argv)
    # Protocol data is the only stdout output during serving; the cancellable
    # pipe adapter redirects accidental process stdout to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        config = load_configuration(args.config)
        registry = TargetRegistry(config.targets)
        with RunStore(config.state_directory) as store:
            core = DiagnosticCore(registry, ParamikoTransport(registry), store)
            asyncio.run(serve(core))
        return 0
    except (ConfigurationError, StorageError, OSError):
        print("EmbedSleuth startup failed: check trusted configuration and private state storage", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

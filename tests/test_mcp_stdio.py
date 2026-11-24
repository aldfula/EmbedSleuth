"""Exercise actual STDIO framing, process signals and durable worker cleanup."""

from contextlib import contextmanager
import asyncio
import fcntl
import json
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from embedsleuth.core import DiagnosticCore
from embedsleuth.storage import RunStore
from embedsleuth.targets import TargetRegistry
from test_config import target, write_config


@contextmanager
def child_process(command):
    # Binary unbuffered streams keep selectors and readline from disagreeing
    # about a second response already buffered in a TextIOWrapper.
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, bufsize=0)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def send(process, message):
    process.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())


def receive(process, request_id):
    deadline = time.monotonic() + 5
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            assert selector.select(timeout=max(0, deadline - time.monotonic())), "MCP response deadline expired"
            line = process.stdout.readline()
            assert line, "MCP subprocess unexpectedly exited"
            response = json.loads(line)  # Non-protocol stdout is a test failure.
            assert response["jsonrpc"] == "2.0"
            if response.get("id") == request_id:
                return response
    pytest.fail("No matching MCP response")


def initialize(process):
    send(process, {"id": 0, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "offline-acceptance", "version": "1.0"},
    }})
    initialized = receive(process, 0)["result"]
    assert initialized["serverInfo"]["name"] == "EmbedSleuth"
    assert "tools" in initialized["capabilities"]
    send(process, {"method": "notifications/initialized"})


def tool(process, request_id, name, arguments):
    send(process, {"id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    result = receive(process, request_id)["result"]
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result


@pytest.mark.parametrize("ending", ["eof", "sigterm", "sigint"])
def test_production_stdio_handshake_tools_errors_and_clean_exit(tmp_path, ending):
    config = write_config(tmp_path)
    with child_process([sys.executable, "-I", "-m", "embedsleuth.mcp_server", "--config", str(config)]) as process:
        initialize(process)
        send(process, {"id": 1, "method": "tools/list"})
        assert len(receive(process, 1)["result"]["tools"]) == 9
        assert tool(process, 2, "get_component_profile", {"profile_id": "mpu6050"})["isError"] is False
        error = tool(process, 3, "start_diagnosis", {"target_id": "unregistered", "profile_id": "mpu6050"})
        assert error["isError"] is True
        assert error["structuredContent"]["error"]["code"] == "invalid_request"
        if ending == "eof":
            process.stdin.close()
        else:
            process.send_signal(signal.SIGTERM if ending == "sigterm" else signal.SIGINT)
        assert process.wait(timeout=5) == 0
        assert process.stdout.read() == b""
        assert process.stderr.read() == b""


def test_invalid_startup_keeps_stdout_empty_and_secrets_out_of_stderr(tmp_path):
    config = write_config(tmp_path, password="secret-config-value")
    result = subprocess.run([sys.executable, "-I", "-m", "embedsleuth.mcp_server", "--config", str(config)],
                            capture_output=True, timeout=5)
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"startup failed" in result.stderr
    assert b"secret" not in result.stderr
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize("ending", ["notification", "eof", "sigterm", "sigint", "sigkill", "broken_stdout"])
def test_running_stdio_call_is_cancelled_committed_and_quarantined(tmp_path, ending):
    config = write_config(tmp_path, targets=[target(tmp_path)])
    started = tmp_path / "started"
    command = [sys.executable, "-I", str(Path(__file__).with_name("mcp_child.py")), str(config), str(started)]
    with child_process(command) as process:
        initialize(process)
        run = tool(process, 1, "start_diagnosis", {"target_id": "lab-pi", "profile_id": "mpu6050"})["structuredContent"]["result"]["run_id"]
        send(process, {"id": 2, "method": "tools/call", "params": {"name": "inspect_target", "arguments": {"run_id": run}}})
        deadline = time.monotonic() + 3
        while not started.exists():
            assert time.monotonic() < deadline, "offline transport did not start"
            time.sleep(0.005)
        if ending == "notification":
            send(process, {"method": "notifications/cancelled", "params": {"requestId": 2, "reason": "test cancellation"}})
            marker = tmp_path / "state" / "runs" / run / "cancel.json"
            while not marker.exists():
                assert time.monotonic() < deadline, "cancellation marker was not persisted"
                time.sleep(0.005)
            # EOF then drains the worker and closes storage, just as the host
            # shutting down after cancelling a request would do.
            process.stdin.close()
        elif ending == "eof":
            process.stdin.close()
        elif ending == "sigkill":
            process.kill()
        elif ending == "broken_stdout":
            process.stdout.close()
            send(process, {"id": 3, "method": "tools/call", "params": {
                "name": "get_component_profile", "arguments": {"profile_id": "mpu6050"},
            }})
        else:
            process.send_signal(signal.SIGTERM if ending == "sigterm" else signal.SIGINT)
        assert process.wait(timeout=5) == (-signal.SIGKILL if ending == "sigkill" else 0)
        # Any remaining wire output must also be JSON, never diagnostic prints.
        if not process.stdout.closed:
            for line in process.stdout.read().splitlines():
                assert json.loads(line)["jsonrpc"] == "2.0"
        stderr = process.stderr.read()
        assert b"fixture diagnostic log" in stderr
        assert b"Traceback" not in stderr
    with RunStore(tmp_path / "state") as store:
        assert store.cancel_requested(run) is (ending != "sigkill")
        assert store.is_quarantined("lab-pi")
        assert store.load_state(run)["pending_operation"] is None
        report = DiagnosticCore(TargetRegistry(), None, store).get_report(run)
        assert report.outcome == "interrupted"
        if ending == "sigkill":
            # No observation was committed before the crash. Recovery must
            # preserve that absence rather than invent a transfer result.
            assert report.evidence == ()
            assert store.load_state(run)["remote_state"] == "unknown"
        else:
            assert report.evidence[-1].result.error.code == "remote_state_unknown"


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_real_sdk_client_negotiates_with_production_stdio_process(tmp_path, mode):
    config = write_config(tmp_path)

    async def scenario():
        parameters = StdioServerParameters(command=sys.executable, args=[
            "-I", "-m", "embedsleuth.mcp_server", "--config", str(config),
        ])
        async with Client(parameters, mode=mode, read_timeout_seconds=3) as client:
            assert len((await client.list_tools()).tools) == 9
            response = await client.call_tool("get_component_profile", {"profile_id": "mpu6050"})
            assert response.is_error is False
            assert response.structured_content["result"]["profile_id"] == "mpu6050"
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [b"x" * 524290 + b"\n", b"\xff\n"])
def test_invalid_or_oversize_stdio_input_closes_without_reflection(tmp_path, payload):
    config = write_config(tmp_path)
    result = subprocess.run([sys.executable, "-I", "-m", "embedsleuth.mcp_server", "--config", str(config)],
                            input=payload, capture_output=True, timeout=5)
    assert result.returncode == 0
    assert result.stdout == b""
    assert b"Invalid or oversized MCP input" in result.stderr
    assert b"Traceback" not in result.stderr


def test_sigterm_exits_when_host_stops_reading_stdout(tmp_path):
    config = write_config(tmp_path)
    with child_process([sys.executable, "-I", "-m", "embedsleuth.mcp_server", "--config", str(config)]) as process:
        initialize(process)
        fcntl.fcntl(process.stdout.fileno(), fcntl.F_SETPIPE_SZ, 4096)
        send(process, {"id": 1, "method": "tools/call", "params": {
            "name": "get_component_profile", "arguments": {"profile_id": "mpu6050"},
        }})
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=3)
        # Leave the output pipe full: a broken host may send SIGTERM without
        # consuming the pending response or closing its stdin pipe.
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=3) == 0
        assert b"Traceback" not in process.stderr.read()

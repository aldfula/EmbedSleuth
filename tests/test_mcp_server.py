"""Real SDK clients, real core/storage, fake transport; no SSH, Pi or LLM."""

import asyncio
import json
from threading import Event
import time

from jsonschema import validate as validate_json_schema
from mcp import Client
from mcp.shared.exceptions import MCPError
import pytest

from embedsleuth.mcp_server import MCPRuntime, OUTPUT_SCHEMA, _response, create_server
from embedsleuth.storage import StorageError
from embedsleuth.tools import tool_input_schemas
from embedsleuth.transport import ProbeResponse
from test_core import make_harness, ready


def body(result):
    value = result.structured_content
    assert json.loads(result.content[0].text) == value
    validate_json_schema(value, OUTPUT_SCHEMA)
    assert result.is_error is (value["error"] is not None)
    return value


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_sdk_initialization_and_exact_nine_tool_schemas(make_harness, mode):
    h = make_harness()

    async def scenario():
        async with Client(create_server(h.core), mode=mode, read_timeout_seconds=3) as client:
            tools = (await client.list_tools()).tools
            assert {tool.name: tool.input_schema for tool in tools} == tool_input_schemas()
            for tool in tools:
                assert tool.output_schema == OUTPUT_SCHEMA
                assert tool.annotations.destructive_hint is False
                assert tool.annotations.read_only_hint is (tool.name == "get_component_profile")
            profile = body(await client.call_tool("get_component_profile", {"profile_id": "mpu6050"}))
            assert profile["result"]["profile_id"] == "mpu6050"
            assert profile["error"] is None
    asyncio.run(scenario())
    assert h.transport.calls == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_full_mcp_workflow_preserves_evidence_and_idempotent_identity(make_harness, mode):
    h = make_harness()

    async def scenario():
        async with Client(create_server(h.core), mode=mode, read_timeout_seconds=3) as client:
            async def call(name, **args):
                response = body(await client.call_tool(name, args))
                assert response["error"] is None
                return response["result"]
            run = (await call("start_diagnosis", target_id="lab-pi", profile_id="mpu6050"))["run_id"]
            await call("inspect_target", run_id=run)
            await call("inspect_i2c_environment", run_id=run)
            args = {"run_id": run, "bus_ref": "bus-1", "address": 104, "operation_id": "read-once"}
            first = await call("read_device_identity", **args)
            replay = await call("read_device_identity", **args)
            assert first == replay
            assert len(await call("evaluate_findings", run_id=run)) == 1
            report = await call("finalize_report", run_id=run)
            assert report["outcome"] == "complete"
            assert report == await call("get_report", run_id=run)
            assert report == await call("cancel_diagnosis", run_id=run)
            assert first["evidence_ids"][0] in {e["evidence_id"] for e in report["evidence"]}
            assert "known_hosts_path" not in json.dumps(report)
    asyncio.run(scenario())
    assert h.transport.count("read_device_identity") == 1


@pytest.mark.parametrize("name,arguments", [
    ("execute_shell", {"command": "secret-command"}),
    ("start_diagnosis", {"target_id": "lab-pi", "profile_id": "mpu6050", "host": "secret-host"}),
    ("start_diagnosis", {"target_id": "lab-pi", "profile_id": "mpu6050", "approved": True}),
    ("start_diagnosis", {"target_id": "unregistered", "profile_id": "mpu6050"}),
    ("get_component_profile", {"profile_id": "unknown"}),
    ("get_report", {"run_id": "../secret-path"}),
    ("get_report", {"run_id": "run-missing"}),
    ("read_device_identity", {"run_id": "run-x", "bus_ref": "bus-1", "address": True, "operation_id": "op"}),
    ("read_device_identity", {"run_id": "run-x", "bus_ref": "bus-1", "address": "104", "operation_id": "op"}),
    ("get_component_profile", {"profile_id": "mpu6050", "key_path": "/secret-key"}),
])
def test_mcp_rejects_untrusted_inputs_without_remote_io_or_reflection(make_harness, name, arguments):
    h = make_harness()

    async def scenario():
        async with Client(create_server(h.core), mode="legacy", read_timeout_seconds=3) as client:
            response = body(await client.call_tool(name, arguments))
            assert response["error"]["code"] == "invalid_request"
            assert "secret" not in json.dumps(response)
    asyncio.run(scenario())
    assert h.transport.calls == []


@pytest.mark.parametrize("mode,conflicts", [("passive", True), ("read_only", False)])
def test_mcp_cannot_bypass_core_read_preconditions(make_harness, mode, conflicts):
    h = make_harness(target_changes={"conflicting_use_excluded": conflicts})

    async def scenario():
        async with Client(create_server(h.core), read_timeout_seconds=3) as client:
            run = body(await client.call_tool("start_diagnosis", {
                "target_id": "lab-pi", "profile_id": "mpu6050", "mode": mode,
            }))["result"]["run_id"]
            for name in ("inspect_target", "inspect_i2c_environment"):
                assert not (await client.call_tool(name, {"run_id": run})).is_error
            blocked = body(await client.call_tool("read_device_identity", {
                "run_id": run, "bus_ref": "bus-1", "address": 104, "operation_id": "blocked",
            }))
            assert blocked["error"]["code"] == "policy_denied"
            assert blocked["result"]["assessment"] == "unknown"
            report = body(await client.call_tool("finalize_report", {"run_id": run}))
            assert report["result"]["outcome"] == "partial"
    asyncio.run(scenario())
    assert h.transport.count("read_device_identity") == 0


def test_mcp_rejects_fabricated_hypothesis_references(make_harness):
    h = make_harness()
    run = ready(h)

    async def scenario():
        async with Client(create_server(h.core), read_timeout_seconds=3) as client:
            response = body(await client.call_tool("finalize_report", {
                "run_id": run,
                "hypotheses": [{"hypothesis_id": "hyp-1", "explanation": "secret-unsupported-claim", "supporting_evidence": ["ev-fake"]}],
            }))
            assert response["error"]["code"] == "invalid_request"
            assert "secret" not in json.dumps(response)
    asyncio.run(scenario())
    assert h.store.load_state(run)["state"] != "reported"


@pytest.mark.parametrize("case", ["mismatch", "unsupported"])
def test_mcp_distinguishes_comparison_failure_from_execution_failure(make_harness, case):
    h = make_harness()
    run = ready(h)
    h.transport.overrides["read_device_identity"] = (
        {"identity_value": 0} if case == "mismatch" else
        ProbeResponse(status="unsupported", error_code="dependency_missing", message="Required tool is unavailable")
    )

    async def scenario():
        async with Client(create_server(h.core), read_timeout_seconds=3) as client:
            response = body(await client.call_tool("read_device_identity", {
                "run_id": run, "bus_ref": "bus-1", "address": 104, "operation_id": "identity-1",
            }))
            assert response["result"]["assessment"] == ("fail" if case == "mismatch" else "unknown")
            assert (response["error"] is not None) is (case == "unsupported")
            assert response["result"]["evidence_ids"]
    asyncio.run(scenario())


@pytest.mark.parametrize("error,code", [
    (StorageError("secret-path"), "storage_error"),
    (OSError("secret-path"), "storage_error"),
    (RuntimeError("secret-token"), "storage_error"),
])
def test_mcp_errors_do_not_disclose_raw_exceptions(make_harness, monkeypatch, caplog, error, code):
    h = make_harness()

    def fail(*args):
        raise error
    monkeypatch.setattr(h.core, "call", fail)

    async def scenario():
        runtime = MCPRuntime(h.core)
        try:
            response = body(await runtime.call("get_component_profile", {"profile_id": "mpu6050"}))
            assert response["error"]["code"] == code
            assert "secret" not in json.dumps(response)
        finally:
            await runtime.close()
    asyncio.run(scenario())
    assert "secret" not in caplog.text


class WaitingTransport:
    def __init__(self):
        self.started = Event()
        self.stopped = Event()

    def execute(self, target, request, *, cancelled, **limits):
        self.started.set()
        try:
            deadline = time.monotonic() + 4
            while not cancelled():
                if time.monotonic() >= deadline:
                    raise RuntimeError("test worker was not cancelled")
                time.sleep(0.005)
            return ProbeResponse(status="error", error_code="remote_state_unknown",
                                 message="Cancelled; remote completion is unconfirmed", remote_state_unknown=True)
        finally:
            self.stopped.set()


async def wait_for_event(event):
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("action", ["cancel_task", "shutdown", "cancel_tool"])
def test_runtime_cancellation_drains_worker_and_retains_unknown_state(make_harness, action):
    h = make_harness()
    run = h.core.start_diagnosis("lab-pi")["run_id"]
    transport = WaitingTransport()
    h.core.transport = transport

    async def scenario():
        runtime = MCPRuntime(h.core)
        task = asyncio.create_task(runtime.call("inspect_target", {"run_id": run}))
        try:
            await wait_for_event(transport.started)
            if action == "cancel_task":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif action == "cancel_tool":
                report = body(await runtime.call("cancel_diagnosis", {"run_id": run}))
                assert report["result"]["outcome"] == "interrupted"
            await runtime.close()
            if action != "cancel_task":
                assert body(await task)["error"]["code"] == "remote_state_unknown"
            assert transport.stopped.is_set()
            assert body(await runtime.call("inspect_target", {"run_id": run}))["error"]["code"] == "resource_limit"
        finally:
            await runtime.close()
    asyncio.run(scenario())
    assert h.store.cancel_requested(run)
    assert h.store.is_quarantined("lab-pi")
    assert h.core.get_report(run).outcome == "interrupted"
    assert h.store.load_state(run)["pending_operation"] is None


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_sdk_timeout_cancels_running_core_operation(make_harness, mode):
    h = make_harness()
    run = h.core.start_diagnosis("lab-pi")["run_id"]
    transport = WaitingTransport()
    h.core.transport = transport

    async def scenario():
        async with Client(create_server(h.core), mode=mode, read_timeout_seconds=3) as client:
            with pytest.raises(MCPError):
                await client.call_tool("inspect_target", {"run_id": run}, read_timeout_seconds=0.15)
            await wait_for_event(transport.stopped)
    asyncio.run(scenario())
    assert h.store.cancel_requested(run)
    assert h.store.is_quarantined("lab-pi")


def test_worker_admission_is_bounded_and_cancel_has_reserved_capacity(make_harness, monkeypatch):
    h = make_harness()
    release = Event()
    count = []

    def call(name, args):
        count.append(name)
        if name == "cancel_diagnosis":
            return {"cancelled": True}
        assert release.wait(3)
        return {"done": True}
    monkeypatch.setattr(h.core, "call", call)

    async def scenario():
        runtime = MCPRuntime(h.core)
        tasks = [asyncio.create_task(runtime.call("get_component_profile", {"profile_id": "mpu6050"})) for _ in range(4)]
        try:
            async with asyncio.timeout(2):
                while len(count) < 4:
                    await asyncio.sleep(0.005)
            overflow = body(await runtime.call("get_component_profile", {"profile_id": "mpu6050"}))
            assert overflow["error"]["code"] == "resource_limit"
            assert body(await runtime.call("cancel_diagnosis", {"run_id": "run-test"}))["error"] is None
        finally:
            release.set()
            await asyncio.gather(*tasks)
            await runtime.close()
    asyncio.run(scenario())
    assert len(count) == 5


def test_adapter_bounds_arguments_and_serialized_results(make_harness):
    h = make_harness()

    async def scenario():
        runtime = MCPRuntime(h.core)
        try:
            result = await runtime.call("get_component_profile", {"profile_id": "x" * 262145})
            assert body(result)["error"]["code"] == "resource_limit"
        finally:
            await runtime.close()
    asyncio.run(scenario())
    assert body(_response({"value": "x" * 1048577}))["error"]["code"] == "resource_limit"

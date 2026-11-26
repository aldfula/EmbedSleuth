"""Test evaluation infrastructure only: no model, credentials or real device."""

import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

from mcp import Client
from mcp.client.stdio import StdioServerParameters
import pytest


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("model_acceptance", ROOT / "scripts/run_model_acceptance.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


@pytest.mark.parametrize("fixture,error,identity_calls", [
    ({}, None, 1),
    ({"observations": {"inspect_i2c_environment": {"driver_bound": {"104": True, "105": False}}}}, "policy_denied", 0),
])
def test_evaluation_backend_uses_production_mcp_and_fake_device_only(tmp_path, fixture, error, identity_calls):
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture))
    directory = tmp_path / "device"
    params = StdioServerParameters(command=sys.executable, args=["-I", str(ROOT / "evals/diagnose-mpu6050/server.py"),
        "--fixture", str(path), "--directory", str(directory)])

    async def check():
        async with Client(params, read_timeout_seconds=5) as client:
            async def call(tool, **arguments):
                result = await client.call_tool(tool, arguments)
                return result.structured_content
            run = (await call("start_diagnosis", target_id="lab-pi", profile_id="mpu6050"))["result"]["run_id"]
            await call("inspect_target", run_id=run)
            await call("inspect_i2c_environment", run_id=run)
            body = await call("read_device_identity", run_id=run, bus_ref="bus-1", address=104, operation_id="one")
            assert (body["error"]["code"] if body["error"] else None) == error
            await call("finalize_report", run_id=run)

    asyncio.run(check())
    events = [json.loads(line) for line in (directory / "calls.jsonl").read_text().splitlines()]
    assert sum(item.get("operation") == "read_device_identity" for item in events) == identity_calls
    assert len([item for item in events if item["kind"] == "tool"]) == 5


def test_host_driver_deadline_covers_partial_json_lines():
    script = "import sys,time; sys.stdout.write('{'); sys.stdout.flush(); time.sleep(2)"
    with subprocess.Popen([sys.executable, "-I", "-c", script], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, bufsize=0) as process:
        try:
            with pytest.raises(driver.EvaluationError, match="deadline"):
                driver.HostSession(process).receive(time.monotonic() + 0.1)
        finally:
            process.kill()
            process.stdin.close()
            process.stdout.close()
            process.wait(timeout=2)


def test_host_driver_never_grants_tool_approval():
    script = '''import json,sys
request=json.loads(sys.stdin.readline())
print(json.dumps({'id':99,'method':'item/commandExecution/requestApproval','params':{}}),flush=True)
reply=json.loads(sys.stdin.readline())
assert 'error' in reply and 'result' not in reply
print(json.dumps({'id':request['id'],'result':{'denied':True}}),flush=True)
'''
    with subprocess.Popen([sys.executable, "-I", "-c", script], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, bufsize=0) as process:
        try:
            session = driver.HostSession(process)
            assert session.request("test", {}, timeout=2) == {"denied": True}
            assert len(session.messages) == 1
        finally:
            process.stdin.close()
            process.stdout.close()
            process.wait(timeout=2)


def test_existing_evaluation_evidence_is_not_overwritten(tmp_path):
    path = tmp_path / "previous"
    path.mkdir()
    with pytest.raises(driver.EvaluationError, match="never overwritten"):
        driver.run("codex", ROOT, path, "passive", False, False, 1)


def test_model_scope_is_checked_before_sending_a_turn(tmp_path):
    calls = []

    class Session:
        def request(self, method, params):
            calls.append(method)
            return {} if method == "initialize" else {"model": "different-model", "modelProvider": "openai"}

        def send(self, value):
            pass

    with pytest.raises(driver.EvaluationError, match="approved"):
        driver.evaluate(Session(), tmp_path, "test", timeout=1)
    assert calls == ["initialize", "thread/start"]


@pytest.mark.parametrize("change,allowed", [({}, True), ({"serverName": "other"}, False),
    ({"environmentId": "other"}, False), ({"tool": "cancel_diagnosis"}, False),
    ({"run_id": "unknown"}, False), ({"tool": "read_device_identity", "address": 105}, False)])
def test_fixture_preauthorization_is_narrow_and_never_implicit(tmp_path, change, allowed):
    from embedsleuth.storage import RunStore
    root = tmp_path / "fixture-state"
    with RunStore(root) as store:
        store.create("run-one", {"target_id": "lab-pi"})
    tool = change.get("tool", "inspect_target")
    arguments = {"run_id": change.get("run_id", "run-one")}
    if tool == "read_device_identity":
        arguments.update({"address": change["address"], "bus_ref": "bus-1", "operation_id": "one"})
    message = {"method": "mcpServer/elicitation/request", "params": {
        "serverName": change.get("serverName", "embedsleuth"), "environmentId": change.get("environmentId"),
        "mode": "form", "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_name": tool, "tool_params": arguments}}}
    assert driver.HostSession(None).approved_fixture_read(message) is False
    session = driver.HostSession(None, root)
    assert session.approved_fixture_read(message) is allowed
    if allowed:
        assert session.approved_fixture_read(message) is False


def test_fixture_preauthorization_cannot_be_applied_to_real_project(tmp_path):
    with pytest.raises(driver.EvaluationError, match="limited"):
        driver.run("codex", ROOT, tmp_path / "new", "identity-104", False, True, 1, True)

"""Operator acceptance client is tested with fake devices, never real approval."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from embedsleuth.mcp_server import MCPRuntime
from embedsleuth.sampling import approve_plan
from embedsleuth.tools import tool_input_schemas
from test_core import make_harness
from test_sampling import observations

spec = importlib.util.spec_from_file_location("sampling_acceptance", Path(__file__).parents[1] / "scripts/run_sampling_acceptance.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Client:
    def __init__(self, runtime, names=None):
        self.runtime = runtime
        self.names = tool_input_schemas() if names is None else names
        self.calls = []

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in self.names])

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        return await self.runtime.call(name, arguments)


@pytest.mark.parametrize("approval", ["granted", "denied", "missing", "expired"])
def test_real_mcp_core_workflow_never_samples_without_current_approval(make_harness, approval):
    h = make_harness()

    async def check():
        runtime = MCPRuntime(h.core)
        client = Client(runtime)

        async def operator(run_id, plan_id):
            h.transport.overrides["sample_sensor_data"] = observations(plan_id)
            if approval == "denied":
                return False
            if approval != "missing":
                approve_plan(h.store, h.registry, run_id, plan_id, now=h.clock.utcnow())
            if approval == "expired":
                h.clock.now += 121
            return True

        try:
            report, result = await runner.exercise(client, h.target, 104, operator)
            assert result["assessment"] == ("pass" if approval == "granted" else "incomplete")
            assert report.outcome == ("complete" if approval == "granted" else "partial")
            assert h.transport.count("sample_sensor_data") == int(approval == "granted")
            assert client.calls[-3:] == ["evaluate_findings", "finalize_report", "get_report"]
            assert h.store.load_state(report.run_id)["pending_operation"] is None
        finally:
            await runtime.close()

    asyncio.run(check())


def test_old_server_cannot_start_a_sampling_run(make_harness):
    h = make_harness()

    async def check():
        runtime = MCPRuntime(h.core)
        client = Client(runtime, names=["start_diagnosis"])
        try:
            with pytest.raises(runner.AcceptanceError, match="v0.2"):
                await runner.exercise(client, h.target, 104, None)
            assert client.calls == [] and h.transport.calls == []
        finally:
            await runtime.close()

    asyncio.run(check())


def test_noninteractive_acceptance_refuses_before_loading_config(tmp_path):
    with pytest.raises(runner.AcceptanceError, match="interactive"):
        asyncio.run(runner.run(tmp_path / "absent.json", "lab-pi", 104))
    assert list(tmp_path.iterdir()) == []


def test_current_prompt_cannot_override_false_registration(make_harness, monkeypatch, tmp_path):
    h = make_harness(target_changes={"conflicting_use_excluded": False})
    monkeypatch.setattr(runner.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(runner.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(runner, "load_configuration", lambda _: SimpleNamespace(targets=(h.target,)))
    with pytest.raises(runner.AcceptanceError, match="conflicting_use_excluded=true"):
        asyncio.run(runner.run(tmp_path / "config.json", h.target.target_id, 104))
    assert h.transport.calls == []

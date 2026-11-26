"""Offline acceptance checks use real MCP/core, never a real Pi or model."""

import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from mcp import Client
import pytest

from embedsleuth.acceptance import ResourceSample, summarize
from embedsleuth.config import ServerConfiguration
from embedsleuth.mcp_server import create_server
from embedsleuth import remote_probe as probe
from test_core import make_harness, ready


spec = importlib.util.spec_from_file_location("hardware_acceptance", Path(__file__).parents[1] / "scripts/run_hardware_acceptance.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def sample(own=8192, child=1024, **updates):
    return {"schema_version": "1.0", "available": True, "measurement": "linux_getrusage",
            "scope": "probe_and_single_waited_child", "probe_peak_rss_kib": own,
            "child_peak_rss_kib": child, "peak_sum_bound_kib": own + child,
            "elapsed_ms": 4, "cpu_ms": 3, "pid": 123, "process_start_ticks": "456", **updates}


def test_real_standalone_probe_reports_linux_resources_and_exits():
    with subprocess.Popen([sys.executable, "-I", "-B", str(Path(probe.__file__))],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        output, errors = process.communicate(b'{"op":"inspect_target"}', timeout=3)
        assert process.returncode == 0 and not errors
        body = json.loads(output)
        metrics = ResourceSample.model_validate_json(json.dumps(body["observations"]["resource_usage"]))
        assert metrics.pid == process.pid and metrics.child_peak_rss_kib == 0
        assert metrics.probe_peak_rss_kib > 0 and metrics.process_start_ticks is not None


@pytest.mark.parametrize("cause", ["unsupported", "os_error", "bad_stat"])
def test_missing_resource_measurement_is_unknown_not_zero(monkeypatch, cause):
    import resource
    if cause == "unsupported":
        monkeypatch.setattr(probe.sys, "platform", "other")
    elif cause == "os_error":
        def unavailable(*args):
            raise OSError("do not echo this path")
        monkeypatch.setattr(resource, "getrusage", unavailable)
    else:
        monkeypatch.setattr(probe, "read_fixed_file", lambda path: "malformed")
    assert probe.resource_usage(0) == {"available": False}


@pytest.mark.parametrize("updates", [
    {"probe_peak_rss_kib": True}, {"probe_peak_rss_kib": 0}, {"child_peak_rss_kib": -1},
    {"peak_sum_bound_kib": 1}, {"elapsed_ms": -1}, {"pid": "123"}, {"process_start_ticks": "../x"},
    {"scope": "whole_process_tree"}, {"extra": True},
])
def test_resource_contract_rejects_misleading_or_coerced_samples(updates):
    with pytest.raises(ValueError):
        ResourceSample.model_validate_json(json.dumps(sample(**updates)))


@pytest.mark.parametrize("own,child,expected", [(32768, 32768, "pass"), (32769, 32768, "fail")])
def test_memory_budget_uses_peak_bound_not_sum_of_sequential_calls(make_harness, own, child, expected):
    h = make_harness()
    for name in ("inspect_target", "inspect_i2c_environment"):
        h.transport.overrides[name] = {"resource_usage": sample(own, child)}
    report = h.core.finalize_report(ready(h, mode="passive"))
    checks = summarize(report)
    assert checks["probe_memory"]["assessment"] == expected
    assert checks["probe_memory"]["peak_sum_bound_kib"] == own + child
    assert checks["probe_memory"]["sample_count"] == 2
    assert checks["probe_memory"]["system_wide_incremental_rss"] == "not_measured"
    assert checks["model_behavior"] == "not_evaluated"
    assert checks["identity"]["assessment"] == "unknown"


def test_missing_telemetry_never_establishes_memory_pass(make_harness):
    h = make_harness()
    report = h.core.finalize_report(ready(h))
    memory = summarize(report)["probe_memory"]
    assert memory["assessment"] == "unknown" and memory["peak_sum_bound_kib"] is None
    assert memory["unmeasured_result_count"] == 2


@pytest.mark.parametrize("address,exclusive,registered,allowed", [
    (None, False, False, True), (104, False, True, False), (104, True, False, False),
    (104, True, True, True), (105, True, True, True), (True, True, True, False), (112, True, True, False),
])
def test_identity_acceptance_requires_current_and_registered_confirmation(make_harness, address, exclusive, registered, allowed):
    h = make_harness(target_changes={"conflicting_use_excluded": registered})
    config = ServerConfiguration(state_directory=h.store.root, targets=(h.target,))
    if allowed:
        assert runner.check_request(config, h.target.target_id, address, exclusive) == h.target
    else:
        with pytest.raises(ValueError):
            runner.check_request(config, h.target.target_id, address, exclusive)
    assert h.transport.calls == []


@pytest.mark.parametrize("address", [None, 104, 105])
def test_acceptance_workflow_uses_real_mcp_with_no_retry(make_harness, address):
    h = make_harness()

    async def check():
        async with Client(create_server(h.core)) as client:
            return await runner.exercise(client, h.target, address=address)

    report = asyncio.run(check())
    assert report.outcome == ("partial" if address is None else "complete")
    assert h.transport.count("read_device_identity") == (0 if address is None else 1)
    assert h.store.load_state(report.run_id)["state"] == "reported"


def test_acceptance_stops_after_connection_failure_and_keeps_partial_report(make_harness):
    from embedsleuth.transport import ProbeResponse
    h = make_harness()
    h.transport.overrides["inspect_target"] = ProbeResponse(status="error", error_code="authentication_failed", message="Authentication failed")

    async def check():
        async with Client(create_server(h.core)) as client:
            return await runner.exercise(client, h.target, address=104)

    report = asyncio.run(check())
    assert report.outcome == "partial"
    assert h.transport.count("inspect_target") == 1
    assert h.transport.count("inspect_i2c_environment") == h.transport.count("read_device_identity") == 0
    assert report.evidence[0].result.error.code == "authentication_failed"
    assert summarize(report)["execution"] == {"assessment": "fail", "error_codes": ["authentication_failed"]}


def test_quarantine_rejected_before_starting_mcp_or_creating_new_runs(make_harness, tmp_path, monkeypatch):
    h = make_harness()
    h.store.quarantine_target("lab-pi", "unknown previous completion")
    path = tmp_path / "targets.json"
    path.write_text(ServerConfiguration(state_directory=h.store.root, targets=(h.target,)).model_dump_json())
    path.chmod(0o600)
    monkeypatch.setattr(runner, "Client", lambda *args, **kwargs: pytest.fail("must not start MCP"))
    with pytest.raises(runner.AcceptanceError, match="quarantined"):
        asyncio.run(runner.run(path, "lab-pi"))
    assert h.store.list_runs() == [] and h.transport.calls == []

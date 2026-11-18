"""Independent transport/probe safety checks without SSH or I2C access."""

import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from embedsleuth import remote_probe as probe
from embedsleuth.targets import RegisteredTarget, TargetRegistry
from embedsleuth.transport import ParamikoTransport


def _ignores_termination(registry, target, request, timeout, output_limit, mailbox, result_size):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    result_size.value = -1
    time.sleep(30)


def test_supervisor_kills_unresponsive_worker_without_leaving_local_child():
    target = RegisteredTarget(target_id="pi", host="192.0.2.1", username="pi",
                              known_hosts_path=Path("/tmp/not-used"))
    transport = ParamikoTransport(TargetRegistry((target,)), supervisor_worker=_ignores_termination)
    original = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    result = transport.execute(target, {"op": "inspect_target"}, timeout=0.4)
    assert time.monotonic() - started < 1.5
    assert result.status == "timeout" and result.remote_state_unknown
    assert not ({child.pid for child in multiprocessing.active_children()} - original)


def _running(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0] not in ("Z", "X")
    except FileNotFoundError:
        return False


def test_stopping_fixed_transfer_kills_child_group_members(tmp_path):
    """A timeout must not leave a userspace descendant continuing the probe."""
    marker = tmp_path / "child-pid"
    source = (
        "import subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(30)'])\n"
        f"open({str(marker)!r},'w').write(str(child.pid))\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen([sys.executable, "-I", "-c", source],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    child_pid = None
    try:
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), "local child fixture failed to initialize"
        child_pid = int(marker.read_text())
        assert _running(child_pid)
        with pytest.raises(subprocess.TimeoutExpired):
            probe.communicate_bounded(process, timeout=0.02)
        probe.stop_transfer(process)
        deadline = time.monotonic() + 1
        while _running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert process.poll() is not None
        assert not _running(child_pid)
    finally:
        probe.stop_transfer(process)
        process.stdout.close()
        process.stderr.close()
        if child_pid is not None and _running(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_driver_binding_changed_since_host_observation_prevents_transfer(monkeypatch):
    """The Pi must recheck ownership instead of trusting the PC's old snapshot."""
    observed = {"bus": 1, "boot_id": "boot", "adapter_fingerprint": "adapter",
                "adapter_present": True, "access_allowed": True,
                "driver_bound": {"104": True}, "combined_transfer_supported": True,
                "dependency_available": True}
    monkeypatch.setattr(probe, "collect_environment", lambda bus, addresses: observed)
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("driver-owned transfer executed"))
    result = probe.dispatch({"op": "read_device_identity", "bus": 1, "addresses": [104], "address": 104,
                             "expected_boot_id": "boot", "expected_adapter_fingerprint": "adapter"})
    assert result["status"] == "blocked" and result["error_code"] == "driver_bound"


def test_missing_precondition_fingerprint_cannot_launch_transfer(monkeypatch):
    monkeypatch.setattr(probe, "collect_environment", lambda bus, addresses: {
        "bus": 1, "boot_id": None, "adapter_fingerprint": None,
    })
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("unbound transfer executed"))
    result = probe.dispatch({"op": "read_device_identity", "bus": 1, "addresses": [104], "address": 104,
                             "expected_boot_id": "boot", "expected_adapter_fingerprint": "adapter"})
    assert result["status"] == "blocked"
    assert result["error_code"] == "policy_denied"

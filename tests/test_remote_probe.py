import json
import io
from pathlib import Path
import subprocess
import sys

import pytest

from embedsleuth import remote_probe as probe


@pytest.fixture(autouse=True)
def fake_pipe_reader(monkeypatch):
    # Command construction tests use fake children; the real bounded reader has
    # separate subprocess tests below.
    monkeypatch.setattr(probe, "communicate_bounded", lambda process, timeout: process.communicate(timeout))


def identity_request(**overrides):
    return {"op": "read_device_identity", "bus": 1, "addresses": [104, 105], "address": 104,
            "expected_boot_id": "boot", "expected_adapter_fingerprint": "adapter"} | overrides


def environment(**overrides):
    return {"bus": 1, "boot_id": "boot", "adapter_fingerprint": "adapter", "adapter_present": True,
            "access_allowed": True, "driver_bound": {"104": False, "105": False},
            "combined_transfer_supported": True, "dependency_available": True} | overrides


@pytest.mark.parametrize("probe_request", [
    {}, {"op": "shell"}, {"op": "inspect_target", "path": "/etc/shadow"},
    {"op": "inspect_i2c_environment", "bus": True, "addresses": [104]},
    {"op": "inspect_i2c_environment", "bus": -1, "addresses": [104]},
    {"op": "inspect_i2c_environment", "bus": 1, "addresses": [True]},
    identity_request(register=1), identity_request(address=32), identity_request(expected_boot_id=""),
    identity_request(addresses=[104, 104]),
])
def test_protocol_rejects_untrusted_execution_fields(probe_request):
    with pytest.raises(ValueError):
        probe.validate_request(probe_request)


def test_inspection_contains_no_i2c_transfer(monkeypatch):
    monkeypatch.setattr(probe, "collect_environment", lambda bus, addresses: environment())
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *a, **kw: pytest.fail("hardware transfer"))
    assert probe.dispatch({"op": "inspect_i2c_environment", "bus": 1, "addresses": [104]})["status"] == "ok"


@pytest.mark.parametrize("changes,code", [
    ({"boot_id": "changed"}, "policy_denied"),
    ({"adapter_fingerprint": "changed"}, "policy_denied"),
    ({"access_allowed": False}, "permission_denied"),
    ({"driver_bound": {"104": True, "105": False}}, "driver_bound"),
    ({"combined_transfer_supported": None}, "dependency_missing"),
])
def test_identity_rechecks_environment_before_transfer(monkeypatch, changes, code):
    monkeypatch.setattr(probe, "collect_environment", lambda bus, addresses: environment(**changes))
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *a, **kw: pytest.fail("unsafe transfer"))
    assert probe.dispatch(identity_request())["error_code"] == code


class FakeProcess:
    stdout = io.BytesIO()
    stderr = io.BytesIO()
    returncode = 0
    def communicate(self, timeout):
        return b"0x68\n", b""


def test_identity_exact_command_and_both_addresses(monkeypatch):
    monkeypatch.setattr(probe, "collect_environment", lambda bus, addresses: environment())
    monkeypatch.setattr(probe, "find_i2ctransfer", lambda: "/usr/sbin/i2ctransfer")
    for address in (104, 105):
        calls = []
        monkeypatch.setattr(probe.subprocess, "Popen", lambda command, **kw: calls.append((command, kw)) or FakeProcess())
        result = probe.dispatch(identity_request(address=address))
        assert result["observations"]["identity_value"] == 104
        assert result["observations"]["boot_id"] == "boot"
        command, options = calls[0]
        assert command == ["/usr/sbin/i2ctransfer", "-y", "1", "w1@0x%02x" % address, "0x75", "r1"]
        assert "shell" not in options and "-f" not in command


def test_missing_transfer_tool_does_not_install(monkeypatch):
    monkeypatch.setattr(probe, "collect_environment", lambda bus, addresses: environment())
    monkeypatch.setattr(probe, "find_i2ctransfer", lambda: None)
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *a, **kw: pytest.fail("installation or transfer"))
    assert probe.dispatch(identity_request())["status"] == "unsupported"


def test_transfer_timeout_is_always_unknown(monkeypatch):
    class HungProcess:
        pid = 12345
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        killed = False
        def wait(self, timeout):
            raise subprocess.TimeoutExpired("fixed", timeout)
        def communicate(self, timeout):
            raise subprocess.TimeoutExpired("fixed", timeout)
        def kill(self):
            self.killed = True
    process = HungProcess()
    monkeypatch.setattr(probe.os, "killpg", lambda pid, sig: process.kill())
    monkeypatch.setattr(probe, "collect_environment", lambda bus, addresses: environment())
    monkeypatch.setattr(probe, "find_i2ctransfer", lambda: "/usr/sbin/i2ctransfer")
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *a, **kw: process)
    result = probe.dispatch(identity_request())
    assert result["status"] == "timeout" and result["remote_state_unknown"]
    assert process.killed


@pytest.mark.parametrize("payload", [b"not json", b"x" * 4097, b'{"op":"shell","command":"reboot"}'])
def test_standalone_wire_rejects_invalid_input(payload):
    result = subprocess.run([sys.executable, "-I", "-B", str(Path(probe.__file__))], input=payload,
                            capture_output=True, timeout=2)
    assert result.returncode == 0
    assert json.loads(result.stdout)["error_code"] == "invalid_request"
    assert not result.stderr


@pytest.mark.parametrize("output", [b"0x-1", b"0x+1", b"0xGG", b"0x6800", b"secret\n0x68"])
def test_malformed_identity_is_not_a_byte(monkeypatch, output):
    class InvalidIdentity:
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        returncode = 0
        def communicate(self, timeout):
            return output, b""
    monkeypatch.setattr(probe, "collect_environment", lambda bus, addresses: environment())
    monkeypatch.setattr(probe, "find_i2ctransfer", lambda: "/usr/sbin/i2ctransfer")
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *a, **kw: InvalidIdentity())
    result = probe.dispatch(identity_request())
    assert result["error_code"] == "transfer_failed"
    assert "identity_value" not in result["observations"]


def test_transfer_error_does_not_leak_stderr(monkeypatch):
    class InvalidIdentity:
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        returncode = 1
        def communicate(self, timeout):
            return b"", b"my secret token"
    monkeypatch.setattr(probe, "collect_environment", lambda bus, addresses: environment())
    monkeypatch.setattr(probe, "find_i2ctransfer", lambda: "/usr/sbin/i2ctransfer")
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *a, **kw: InvalidIdentity())
    result = probe.dispatch(identity_request())
    assert result["error_code"] == "transfer_failed"
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_real_child_output_is_bounded_before_buffering(monkeypatch, stream):
    monkeypatch.undo()
    process = subprocess.Popen([sys.executable, "-I", "-c",
                                "import sys; sys.%s.write('x' * 1000000)" % stream],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        with pytest.raises(ValueError, match="output limit"):
            probe.communicate_bounded(process, timeout=2)
    finally:
        probe.stop_transfer(process)
        process.stdout.close()
        process.stderr.close()


def test_real_bounded_reader_handles_normal_child(monkeypatch):
    monkeypatch.undo()
    process = subprocess.Popen([sys.executable, "-I", "-c", "print('0x68')"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        assert probe.communicate_bounded(process, timeout=2) == (b"0x68\n", b"")
        assert process.returncode == 0
    finally:
        probe.stop_transfer(process)
        process.stdout.close()
        process.stderr.close()


def test_real_bounded_reader_times_out_and_child_can_be_stopped(monkeypatch):
    monkeypatch.undo()
    process = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(5)"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            probe.communicate_bounded(process, timeout=0.02)
    finally:
        probe.stop_transfer(process)
        process.stdout.close()
        process.stderr.close()
    assert process.poll() is not None


def test_boot_config_returns_only_relevant_sanitized_facts(monkeypatch):
    monkeypatch.setattr(probe, "read_fixed_file", lambda path: """
[all]
# PRIVATE_TOKEN=secret
api_key=secret
include secret-path.txt
[secret-section]
dtparam=i2c_arm=on # token=secret
dtparam=i2c_arm_baudrate=100000,private=secret
dtparam=i2c_arm=secret
dtoverlay=i2c1,pin=secret
dtoverlay=unrelated,key=secret
""")
    summary = probe.config_summary("/boot/config.txt")
    assert summary["available"] is True
    assert "dtparam=i2c_arm=on" in summary["directives"]
    assert "dtparam=i2c_arm_baudrate=100000" in summary["directives"]
    assert "include present; not resolved" in summary["directives"]
    assert "secret" not in json.dumps(summary)
    assert "unrelated" not in json.dumps(summary)


def test_missing_boot_config_is_unknown(monkeypatch):
    monkeypatch.setattr(probe, "read_fixed_file", lambda path: None)
    assert probe.config_summary("/boot/config.txt") == {"available": False, "directives": []}

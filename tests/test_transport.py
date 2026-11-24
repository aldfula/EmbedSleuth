import json
from pathlib import Path
import socket

import paramiko
import pytest

from embedsleuth.targets import RegisteredTarget, TargetRegistry
from embedsleuth.transport import ParamikoTransport


class FakeChannel:
    def __init__(self, stdout=None, stderr=b"", exit_status=0, finish=True):
        self.stdout = bytearray(stdout if stdout is not None else json.dumps({
            "status": "ok", "error_code": None, "message": "", "observations": {"boot_id": "boot"},
            "remote_state_unknown": False}).encode())
        self.stderr = bytearray(stderr)
        self.exit_status = exit_status
        self.finish = finish
        self.closed = False
        self.command = None
        self.input = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def exec_command(self, command):
        self.command = command

    def sendall(self, payload):
        self.input = payload

    def shutdown_write(self):
        pass

    def recv_ready(self):
        return bool(self.stdout)

    def recv_stderr_ready(self):
        return bool(self.stderr)

    def recv(self, size):
        output = bytes(self.stdout[:size])
        del self.stdout[:size]
        return output

    def recv_stderr(self, size):
        output = bytes(self.stderr[:size])
        del self.stderr[:size]
        return output

    def exit_status_ready(self):
        return self.finish

    def recv_exit_status(self):
        return self.exit_status

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, channel=None, error=None):
        self.channel = channel or FakeChannel()
        self.error = error
        self.closed = False
        self.connected = False

    def load_host_keys(self, path):
        self.host_keys = path

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, **kwargs):
        self.options = kwargs
        if self.error:
            raise self.error
        self.connected = True

    def get_transport(self):
        return self

    def get_remote_server_key(self):
        class Key:
            def asbytes(self):
                return b"trusted public host key"
        return Key()

    def is_active(self):
        return True

    def open_session(self, timeout):
        return self.channel

    def close(self):
        self.closed = True


def setup_transport(client=None):
    target = RegisteredTarget(target_id="pi", host="192.0.2.1", username="pi",
                              known_hosts_path=Path("/tmp/hosts"))
    client = client or FakeClient()
    return target, client, ParamikoTransport(TargetRegistry((target,)), lambda: client)


def test_fixed_probe_with_request_on_stdin_and_host_verification():
    target, client, transport = setup_transport()
    result = transport.execute(target, {"op": "inspect_target"})
    assert result.status == "ok"
    assert result.observations["boot_id"] == "boot"
    assert result.observations["host_key_fingerprint"].startswith("sha256:")
    assert isinstance(client.policy, paramiko.RejectPolicy)
    assert client.host_keys == "/tmp/hosts"
    assert client.options["look_for_keys"] is False
    assert client.options["allow_agent"] is True
    assert client.channel.command.startswith("python3 -I -B -c ")
    assert json.loads(client.channel.input) == {"op": "inspect_target"}
    assert client.channel.closed and client.closed


@pytest.mark.parametrize("probe_request", [
    {"op": "run", "command": "reboot"}, {"op": "inspect_target", "command": "sudo"},
    {"op": "inspect_i2c_environment", "bus": 3, "addresses": [104]},
    {"op": "inspect_i2c_environment", "bus": 1, "addresses": [32]},
])
def test_invalid_or_outside_registration_never_connects(probe_request):
    target, client, transport = setup_transport()
    assert transport.execute(target, probe_request).status == "blocked"
    assert not client.connected


def test_equal_but_not_registered_target_never_connects():
    target, client, transport = setup_transport()
    assert transport.execute(target.model_copy(), {"op": "inspect_target"}).status == "blocked"
    assert not client.connected


@pytest.mark.parametrize("error,code", [
    (paramiko.AuthenticationException("secret"), "authentication_failed"),
    (paramiko.SSHException("No authentication methods available"), "authentication_failed"),
    (paramiko.SSHException("Server 'pi' not found in known_hosts"), "host_key_unknown"),
    (socket.timeout("secret"), "network_timeout"),
    (FileNotFoundError("private key secret"), "dependency_missing"),
])
def test_connection_errors_are_distinct_and_do_not_leak(error, code):
    target, client, transport = setup_transport(FakeClient(error=error))
    result = transport.execute(target, {"op": "inspect_target"})
    assert result.error_code == code
    assert "secret" not in result.message
    assert not result.remote_state_unknown
    assert client.closed


@pytest.mark.parametrize("field", ["stdout", "stderr"])
def test_stream_limits_enforced_independently(field):
    channel = FakeChannel(finish=False)
    setattr(channel, field, bytearray(b"x" * 65))
    target, client, transport = setup_transport(FakeClient(channel))
    result = transport.execute(target, {"op": "inspect_target"}, output_limit=64)
    assert result.error_code == "resource_limit"
    assert result.truncated and result.remote_state_unknown


def test_timeout_is_unknown_even_after_channel_close():
    target, client, transport = setup_transport(FakeClient(FakeChannel(stdout=b"", finish=False)))
    result = transport.execute(target, {"op": "inspect_target"}, timeout=0.01)
    assert result.status == "timeout" and result.remote_state_unknown
    assert client.channel.closed


@pytest.mark.parametrize("payload", [b"not json", b"{}", b'{"status":"ok"}',
    b'{"status":"ok","error_code":null,"message":"","observations":{},"remote_state_unknown":true}'])
def test_malformed_response_cannot_establish_success(payload):
    target, client, transport = setup_transport(FakeClient(FakeChannel(payload)))
    result = transport.execute(target, {"op": "inspect_target"})
    assert result.status != "ok" and result.remote_state_unknown


def test_exec_acknowledgment_failure_is_unknown():
    channel = FakeChannel()
    def uncertain_exec(command):
        raise socket.timeout()
    channel.exec_command = uncertain_exec
    target, client, transport = setup_transport(FakeClient(channel))
    assert transport.execute(target, {"op": "inspect_target"}).remote_state_unknown


def test_authentication_text_after_dispatch_cannot_clear_unknown_completion():
    channel = FakeChannel()
    def uncertain_exec(command):
        raise paramiko.SSHException("No authentication methods available")
    channel.exec_command = uncertain_exec
    target, client, transport = setup_transport(FakeClient(channel))
    result = transport.execute(target, {"op": "inspect_target"})
    assert result.error_code == "remote_state_unknown"
    assert result.remote_state_unknown


def test_host_key_mismatch_is_distinct():
    key = paramiko.RSAKey.generate(1024)
    error = paramiko.BadHostKeyException("pi", key, key)
    target, client, transport = setup_transport(FakeClient(error=error))
    result = transport.execute(target, {"op": "inspect_target"})
    assert result.error_code == "host_key_mismatch"
    assert not result.remote_state_unknown


def test_explicit_private_key_disables_agent():
    target, client, transport = setup_transport()
    target = target.model_copy(update={"key_path": Path("/tmp/private_key")})
    transport = ParamikoTransport(TargetRegistry((target,)), lambda: client)
    assert transport.execute(target, {"op": "inspect_target"}).status == "ok"
    assert client.options["key_filename"] == "/tmp/private_key"
    assert client.options["allow_agent"] is False


def test_host_fingerprint_is_local_transport_evidence():
    channel = FakeChannel(json.dumps({"status": "ok", "error_code": None, "message": "",
        "observations": {"host_key_fingerprint": "attacker supplied"}, "remote_state_unknown": False}).encode())
    target, client, transport = setup_transport(FakeClient(channel))
    result = transport.execute(target, {"op": "inspect_target"})
    assert result.observations["host_key_fingerprint"].startswith("sha256:")


def test_unknown_exit_status_is_quarantined():
    target, client, transport = setup_transport(FakeClient(FakeChannel(exit_status=-1)))
    assert transport.execute(target, {"op": "inspect_target"}).remote_state_unknown


@pytest.mark.parametrize("limit", [True, 0, -1, 65537])
def test_output_limit_cannot_be_weakened(limit):
    target, client, transport = setup_transport()
    assert transport.execute(target, {"op": "inspect_target"}, output_limit=limit).status == "blocked"
    assert not client.connected


def test_missing_remote_python_is_unsupported_with_confirmed_exit():
    channel = FakeChannel(stdout=b"", stderr=b"shell: python3: not found", exit_status=127)
    target, client, transport = setup_transport(FakeClient(channel))
    result = transport.execute(target, {"op": "inspect_target"})
    assert result.status == "unsupported"
    assert result.error_code == "dependency_missing"
    assert not result.remote_state_unknown


def test_cancellation_before_connection_has_no_remote_side_effects():
    target, client, transport = setup_transport()
    result = transport.execute(target, {"op": "inspect_target"}, cancelled=lambda: True)
    assert result.status == "blocked" and result.error_code == "policy_denied"
    assert not result.remote_state_unknown
    assert not client.connected and client.channel.command is None


def test_cancellation_after_connect_before_exec_closes_without_unknown():
    target, client, transport = setup_transport()
    result = transport.execute(target, {"op": "inspect_target"}, cancelled=lambda: client.connected)
    assert result.status == "blocked" and not result.remote_state_unknown
    assert client.channel.command is None
    assert client.closed and client.channel.closed


def test_cancellation_after_exec_does_not_claim_kernel_io_stopped():
    target, client, transport = setup_transport()
    result = transport.execute(target, {"op": "inspect_target"},
                               cancelled=lambda: client.channel.command is not None)
    assert result.status == "error" and result.error_code == "remote_state_unknown"
    assert result.remote_state_unknown
    assert client.channel.input is None
    assert client.closed and client.channel.closed


def test_cancellation_while_waiting_for_output_is_unknown():
    target, client, transport = setup_transport(FakeClient(FakeChannel(stdout=b"", finish=False)))
    result = transport.execute(target, {"op": "inspect_target"},
                               cancelled=lambda: client.channel.input is not None)
    assert result.status == "error" and result.remote_state_unknown
    assert client.closed and client.channel.closed


def test_cancellation_callback_error_fails_closed():
    target, client, transport = setup_transport()
    def broken_callback():
        raise OSError("cancel state unavailable")
    result = transport.execute(target, {"op": "inspect_target"}, cancelled=broken_callback)
    assert result.status == "blocked" and not client.connected


def supervisor_delayed_worker(registry, target, request, timeout, output_limit, mailbox, result_size):
    import time
    time.sleep(5)


def supervisor_invalid_length_worker(registry, target, request, timeout, output_limit, mailbox, result_size):
    result_size.value = len(mailbox) + 1


def supervisor_failure_worker(registry, target, request, timeout, output_limit, mailbox, result_size):
    result_size.value = 0


def supervisor_success_worker(registry, target, request, timeout, output_limit, mailbox, result_size):
    payload = json.dumps({"status": "ok", "error_code": None, "message": "",
                          "observations": {"simulated": True}, "remote_state_unknown": False,
                          "truncated": False}).encode()
    mailbox[:len(payload)] = payload
    result_size.value = len(payload)


def supervised_transport(worker):
    target, client, _ = setup_transport()
    return target, ParamikoTransport(TargetRegistry((target,)), supervisor_worker=worker)


def test_spawn_supervisor_bounds_slow_dns_or_worker():
    import time
    target, transport = supervised_transport(supervisor_delayed_worker)
    started = time.monotonic()
    result = transport.execute(target, {"op": "inspect_target"}, timeout=0.05)
    assert time.monotonic() - started < 0.75
    assert result.status == "timeout" and result.remote_state_unknown


@pytest.mark.parametrize("worker", [supervisor_invalid_length_worker, supervisor_failure_worker])
def test_spawn_supervisor_rejects_missing_or_oversized_result(worker):
    target, transport = supervised_transport(worker)
    result = transport.execute(target, {"op": "inspect_target"}, timeout=3)
    assert result.status == "error" and result.remote_state_unknown


def test_spawn_supervisor_returns_bounded_success():
    target, transport = supervised_transport(supervisor_success_worker)
    result = transport.execute(target, {"op": "inspect_target"}, timeout=3)
    assert result.status == "ok" and result.observations == {"simulated": True}


def test_spawn_supervisor_active_cancellation():
    import time
    target, transport = supervised_transport(supervisor_delayed_worker)
    started = time.monotonic()
    result = transport.execute(target, {"op": "inspect_target"}, timeout=3,
                               cancelled=lambda: time.monotonic() - started > 0.04)
    assert time.monotonic() - started < 0.75
    assert result.status == "error" and result.remote_state_unknown


def test_spawn_supervisor_pre_cancel_never_starts_worker():
    target, transport = supervised_transport(supervisor_delayed_worker)
    result = transport.execute(target, {"op": "inspect_target"}, cancelled=lambda: True)
    assert result.status == "blocked" and not result.remote_state_unknown


def test_default_spawn_worker_preserves_registered_object_identity_without_network():
    target, client, _ = setup_transport()
    transport = ParamikoTransport(TargetRegistry((target,)))
    result = transport.execute(target, {"op": "inspect_i2c_environment", "bus": 2, "addresses": [104]}, timeout=3)
    assert result.status == "blocked" and result.error_code == "invalid_request"
    assert not result.remote_state_unknown


def supervisor_raising_worker(registry, target, request, timeout, output_limit, mailbox, result_size):
    raise RuntimeError("simulated worker failure")


def test_spawn_supervisor_worker_exception_is_unknown():
    target, transport = supervised_transport(supervisor_raising_worker)
    result = transport.execute(target, {"op": "inspect_target"}, timeout=3)
    assert result.status == "error" and result.remote_state_unknown
    assert "simulated" not in result.message

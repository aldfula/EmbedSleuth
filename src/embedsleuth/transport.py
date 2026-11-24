"""Bounded SSH execution of the bundled probe, with strict host-key checking."""

from dataclasses import asdict, dataclass, field
from importlib.resources import files
import json
import hashlib
import math
import multiprocessing
import shlex
import socket
import time
from typing import Callable

import paramiko

from embedsleuth.contracts import DiagnosticError
from embedsleuth.remote_probe import validate_request
from embedsleuth.targets import RegisteredTarget, TargetRegistry


@dataclass(frozen=True)
class ProbeResponse:
    status: str = "ok"
    error_code: str | None = None
    observations: dict = field(default_factory=dict)
    message: str = ""
    remote_state_unknown: bool = False
    truncated: bool = False


def _failure(code, message, *, status="error", unknown=False, truncated=False):
    return ProbeResponse(status=status, error_code=code, message=message,
                         remote_state_unknown=unknown, truncated=truncated)


def _parse_response(stdout, stderr, exit_status):
    if exit_status != 0 or stderr:
        raise ValueError("probe execution did not complete cleanly")
    result = json.loads(stdout)
    required = {"status", "error_code", "observations", "message", "remote_state_unknown"}
    if type(result) is not dict or set(result) != required:
        raise ValueError("invalid probe envelope")
    if result["status"] not in ("ok", "error", "blocked", "unsupported", "timeout"):
        raise ValueError("invalid probe status")
    if (type(result["observations"]) is not dict or type(result["remote_state_unknown"]) is not bool or
            type(result["message"]) is not str or len(result["message"]) > 2000):
        raise ValueError("invalid probe metadata")
    if result["status"] == "ok":
        if result["error_code"] is not None or result["remote_state_unknown"]:
            raise ValueError("unsafe successful probe")
    else:
        DiagnosticError(code=result["error_code"], message=result["message"])
    # Reject non-JSON numbers accepted permissively by json.loads.
    json.dumps(result, allow_nan=False)
    return ProbeResponse(**result)


SUPERVISOR_MAILBOX_LIMIT = 131072


def _transport_worker(registry, target, request, timeout, output_limit, mailbox, result_size):
    """Spawn entry point. No DNS or Paramiko operation runs in the supervisor."""
    try:
        result = ParamikoTransport(registry, client_factory=paramiko.SSHClient)._execute_direct(
            target, request, timeout, output_limit)
        payload = json.dumps(asdict(result), allow_nan=False).encode("utf-8")
        if len(payload) > len(mailbox):
            result = _failure("resource_limit", "SSH worker result exceeded bounds", unknown=True, truncated=True)
            payload = json.dumps(asdict(result)).encode("utf-8")
        mailbox[:len(payload)] = payload
        result_size.value = len(payload)  # Publish only after the complete bounded write.
    except Exception:
        # The parent cannot know whether failure happened before remote execution.
        result_size.value = 0


def _cancelled(callback):
    if callback is None:
        return False
    try:
        return callback() is not False
    except Exception:
        return True


class ParamikoTransport:
    def __init__(self, registry: TargetRegistry, client_factory: Callable | None = None,
                 *, supervisor_worker: Callable | None = None):
        self.registry = registry
        self.client_factory = client_factory or paramiko.SSHClient
        self._supervise = client_factory is None
        self._supervisor_worker = supervisor_worker or _transport_worker

    def execute(self, target: RegisteredTarget, request: dict, timeout: float = 10.0,
                output_limit: int = 65536, *,
                cancelled: Callable[[], bool] | None = None) -> ProbeResponse:
        if not self._supervise:
            return self._execute_direct(target, request, timeout, output_limit, cancelled=cancelled)
        if not self.registry.contains_exact(target):
            return _failure("policy_denied", "Target is not the registered local target", status="blocked")
        try:
            validate_request(request)
            if (type(timeout) not in (float, int) or not math.isfinite(timeout) or not 0 < timeout <= 120 or
                    type(output_limit) is not int or not 1 <= output_limit <= 65536):
                raise ValueError("invalid resource limit")
        except (ValueError, TypeError):
            return _failure("invalid_request", "Invalid fixed-probe request", status="blocked")
        if _cancelled(cancelled):
            return _failure("policy_denied", "Diagnosis cancelled before remote execution", status="blocked")
        # Shared memory avoids blocking on a partial pipe frame or unbounded queue.
        context = multiprocessing.get_context("spawn")
        mailbox = context.RawArray("B", SUPERVISOR_MAILBOX_LIMIT)
        result_size = context.RawValue("i", 0)
        worker = context.Process(target=self._supervisor_worker,
                                 args=(self.registry, target, request, timeout, output_limit, mailbox, result_size),
                                 daemon=True)
        deadline = time.monotonic() + timeout
        started = False
        try:
            worker.start()
            started = True
            while worker.is_alive():
                if _cancelled(cancelled):
                    return _failure("remote_state_unknown", "Diagnosis cancelled; remote completion unconfirmed", unknown=True)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _failure("remote_state_unknown", "Overall SSH deadline exceeded; remote completion unconfirmed", status="timeout", unknown=True)
                worker.join(timeout=min(0.01, remaining))
            if _cancelled(cancelled):
                return _failure("remote_state_unknown", "Diagnosis cancelled; remote completion unconfirmed", unknown=True)
            if time.monotonic() >= deadline:
                return _failure("remote_state_unknown", "Overall SSH deadline exceeded; remote completion unconfirmed", status="timeout", unknown=True)
            size = result_size.value
            if worker.exitcode != 0 or not 0 < size <= SUPERVISOR_MAILBOX_LIMIT:
                return _failure("remote_state_unknown", "SSH worker ended without a valid bounded result", unknown=True)
            try:
                envelope = json.loads(bytes(mailbox[:size]))
                if type(envelope) is not dict or set(envelope) != set(ProbeResponse.__dataclass_fields__):
                    raise ValueError("invalid worker response")
                truncated = envelope.pop("truncated")
                if type(truncated) is not bool:
                    raise ValueError("invalid worker truncation flag")
                result = _parse_response(json.dumps(envelope).encode(), b"", 0)
                return ProbeResponse(**(asdict(result) | {"truncated": truncated}))
            except (ValueError, TypeError, UnicodeError):
                return _failure("remote_state_unknown", "SSH worker result could not be validated", unknown=True)
        except (OSError, RuntimeError):
            return _failure("remote_state_unknown", "SSH worker could not complete", unknown=started)
        finally:
            if started:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=0.1)
                if worker.is_alive():
                    worker.kill()
                    worker.join(timeout=0.1)
                if not worker.is_alive():
                    worker.close()

    def _execute_direct(self, target: RegisteredTarget, request: dict, timeout: float = 10.0,
                output_limit: int = 65536, *,
                cancelled: Callable[[], bool] | None = None) -> ProbeResponse:
        if not self.registry.contains_exact(target):
            return _failure("policy_denied", "Target is not the registered local target", status="blocked")
        try:
            validate_request(request)
            if request["op"] != "inspect_target" and (
                request["bus"] != target.expected_bus or
                not set(request["addresses"]).issubset(target.expected_addresses)
            ):
                raise ValueError("request outside registered hardware")
            if (type(timeout) not in (float, int) or not math.isfinite(timeout) or not 0 < timeout <= 120 or
                    type(output_limit) is not int or not 1 <= output_limit <= 65536):
                raise ValueError("invalid resource limit")
            payload = json.dumps(request, allow_nan=False).encode("utf-8")
            if len(payload) > 4096:
                raise ValueError("request too large")
        except (ValueError, TypeError):
            return _failure("invalid_request", "Invalid fixed-probe request", status="blocked")
        def cancellation_requested():
            if cancelled is None:
                return False
            try:
                # A failed callback cannot authorize continued execution.
                return cancelled() is not False
            except Exception:
                return True

        if cancellation_requested():
            return _failure("policy_denied", "Diagnosis cancelled before remote execution", status="blocked")
        client = self.client_factory()
        channel = None
        started = False
        deadline = time.monotonic() + timeout
        try:
            # Only the explicitly chosen trust file is loaded. Never AutoAddPolicy.
            client.load_host_keys(str(target.known_hosts_path))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            if cancellation_requested():
                return _failure("policy_denied", "Diagnosis cancelled before remote execution", status="blocked")
            client.connect(hostname=target.host, username=target.username, port=target.port,
                           key_filename=str(target.key_path) if target.key_path else None,
                           allow_agent=target.key_path is None, look_for_keys=False,
                           timeout=timeout, auth_timeout=timeout, banner_timeout=timeout,
                           channel_timeout=timeout)
            connection = client.get_transport()
            if connection is None or not connection.is_active():
                return _failure("network_timeout", "SSH connection is inactive")
            host_fingerprint = "sha256:" + hashlib.sha256(connection.get_remote_server_key().asbytes()).hexdigest()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _failure("network_timeout", "SSH connection exceeded deadline", status="timeout")
            channel = connection.open_session(timeout=remaining)
            channel.settimeout(remaining)
            source = files("embedsleuth").joinpath("remote_probe.py").read_text(encoding="utf-8")
            command = "python3 -I -B -c " + shlex.quote(source)
            if cancellation_requested():
                return _failure("policy_denied", "Diagnosis cancelled before remote execution", status="blocked")
            # Request fields are sent as data, never interpolated into shell text.
            started = True  # exec may reach the server even if acknowledgment fails.
            channel.exec_command(command)
            if cancellation_requested():
                return _failure("remote_state_unknown", "Diagnosis cancelled; remote completion unconfirmed", unknown=True)
            channel.sendall(payload)
            channel.shutdown_write()
            stdout = bytearray()
            stderr = bytearray()
            while True:
                if cancellation_requested():
                    return _failure("remote_state_unknown", "Diagnosis cancelled; remote completion unconfirmed", unknown=True)
                if time.monotonic() >= deadline:
                    return _failure("remote_state_unknown", "Remote completion could not be confirmed", status="timeout", unknown=True)
                for ready, receive, destination in (
                    (channel.recv_ready, channel.recv, stdout),
                    (channel.recv_stderr_ready, channel.recv_stderr, stderr),
                ):
                    if ready():
                        destination.extend(receive(min(4096, output_limit + 1 - len(destination))))
                        if len(destination) > output_limit:
                            return _failure("resource_limit", "Remote output limit exceeded", unknown=True, truncated=True)
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    exit_status = channel.recv_exit_status()
                    if exit_status == 127 and not stdout:
                        return _failure("dependency_missing", "Remote Python 3 executable unavailable", status="unsupported")
                    try:
                        result = _parse_response(bytes(stdout), bytes(stderr), exit_status)
                        return ProbeResponse(status=result.status, error_code=result.error_code,
                                             observations=result.observations | {"host_key_fingerprint": host_fingerprint},
                                             message=result.message, remote_state_unknown=result.remote_state_unknown)
                    except (ValueError, TypeError, UnicodeError):
                        return _failure("remote_state_unknown", "Remote result could not be validated", unknown=True)
                if channel.closed and not channel.exit_status_ready():
                    return _failure("remote_state_unknown", "SSH channel closed before completion", unknown=True)
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        except paramiko.BadHostKeyException:
            return _failure("host_key_mismatch", "SSH host key does not match trusted key")
        except paramiko.AuthenticationException:
            return _failure("authentication_failed", "SSH authentication failed")
        except (socket.timeout, TimeoutError):
            return _failure("remote_state_unknown" if started else "network_timeout", "SSH operation timed out", status="timeout", unknown=started)
        except FileNotFoundError:
            return _failure("dependency_missing", "Local SSH trust or identity file is unavailable")
        except paramiko.SSHException as exc:
            # Paramiko RejectPolicy raises SSHException; no credentials/raw text are returned.
            if not started and "not found in known_hosts" in str(exc):
                return _failure("host_key_unknown", "SSH host key has not been trusted")
            # With no key, agent identity or password, Paramiko raises the
            # generic SSHException rather than AuthenticationException.
            if not started and str(exc) == "No authentication methods available":
                return _failure("authentication_failed", "No SSH authentication identity is available")
            return _failure("remote_state_unknown" if started else "network_timeout", "SSH connection failed", unknown=started)
        except OSError:
            return _failure("remote_state_unknown" if started else "network_timeout", "SSH transport unavailable", unknown=started)
        finally:
            try:
                if channel is not None:
                    channel.close()
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass

"""Deterministic diagnostic sessions. The AI host selects tools, not commands.

The persisted, validated JSON snapshot is authoritative. Returned models are
detached views; no external observation or policy flag authorizes remote IO.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from pydantic import TypeAdapter

from embedsleuth import __version__
from embedsleuth.contracts import (
    DiagnosticError, DiagnosticReport, ErrorCode, EvidenceRecord, Finding,
    FinalizeReportRequest, Hypothesis, IdentityReadRequest, RunRequest,
    StartDiagnosisRequest, ToolResult,
)
from embedsleuth.policy import IdentityReadContext, POLICY_VERSION, authorize_identity_read
from embedsleuth.profiles import evaluate_identity, load_component_profile
from embedsleuth.storage import RunStore, StorageError, StoreBusyError, sanitize
from embedsleuth.targets import TargetRegistry
from embedsleuth.tools import validate_arguments
from embedsleuth.transport import ProbeResponse


class SessionError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class DiagnosticCore:
    def __init__(
        self, registry: TargetRegistry, transport, store: RunStore, *,
        clock: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        tool_timeout: float = 10.0, active_budget: float = 120.0,
        run_ttl: float = 600.0, max_calls: int = 12,
        freshness: float = 30.0, output_limit: int = 65536,
    ):
        for value in (tool_timeout, active_budget, run_ttl, freshness):
            if type(value) not in (int, float) or not 0 < value < float("inf"):
                raise ValueError("time limits must be finite and positive")
        if type(max_calls) is not int or not 1 <= max_calls <= 12:
            raise ValueError("max_calls must be between 1 and 12")
        if type(output_limit) is not int or not 1 <= output_limit <= 65536:
            raise ValueError("output_limit must be between 1 and 65536")
        if tool_timeout > 10 or active_budget > 120 or run_ttl > 600:
            raise ValueError("limits cannot exceed the MVP safety budget")
        self.registry, self.transport, self.store = registry, transport, store
        self.clock, self.utcnow = clock, utcnow
        self.tool_timeout, self.active_budget, self.run_ttl = tool_timeout, active_budget, run_ttl
        self.max_calls, self.freshness, self.output_limit = max_calls, freshness, output_limit
        self.instance_id = uuid4().hex
        self._unavailable_targets: set[str] = set()

    def call(self, tool_name: str, arguments: dict):
        request = validate_arguments(tool_name, arguments)
        if tool_name == "get_component_profile":
            return load_component_profile(request.profile_id)
        if tool_name == "start_diagnosis":
            return self.start_diagnosis(request.target_id, request.profile_id, request.mode)
        if tool_name == "read_device_identity":
            return self.read_device_identity(request.run_id, request.bus_ref, request.address, request.operation_id)
        if tool_name == "finalize_report":
            return self.finalize_report(request.run_id, request.hypotheses)
        return getattr(self, tool_name)(request.run_id)

    @staticmethod
    def _target_digest(target) -> str:
        payload = target.model_dump(mode="json")
        # Pin the trust store content as well as the registered connection.
        try:
            with Path(target.known_hosts_path).open("rb") as trust_file:
                content = trust_file.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError("SSH trust store exceeds the local limit")
            payload["trust_store_digest"] = hashlib.sha256(content).hexdigest()
        except OSError:
            payload["trust_store_digest"] = "unavailable"
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def start_diagnosis(self, target_id: str, profile_id: str = "mpu6050", mode: str = "read_only") -> dict:
        request = StartDiagnosisRequest(target_id=target_id, profile_id=profile_id, mode=mode)
        target = self.registry.get(request.target_id)
        profile = load_component_profile(request.profile_id)
        if target.profile_id != profile.profile_id:
            raise SessionError("policy_denied", "Registered connection and component profile differ")
        run_id = "run-" + uuid4().hex
        state = {
            "schema_version": "1.0", "run_id": run_id, "target_id": target_id,
            "profile_id": profile_id, "profile_version": profile.version, "mode": mode,
            "instance_id": self.instance_id, "target_digest": self._target_digest(target),
            "state": "created", "remote_state": "idle", "remote_calls": 0,
            "created_at": self.utcnow().isoformat(), "created_mono": self.clock(),
            "evidence": [], "operations": {}, "environment": None, "binding": None,
            "expected_addresses": list(target.expected_addresses), "pending_operation": None,
        }
        self.store.create(run_id, state)
        self._write_report(state)
        return {key: state[key] for key in ("run_id", "target_id", "profile_id", "mode", "state")}

    def _recover_state(self, run_id: str) -> dict:
        state = self.store.recover_pending(run_id)
        if (self.store.cancel_requested(run_id)
                and state["state"] not in ("reported", "cancelled")):
            state["state"], state["environment"] = "cancelled", None
            self.store.save_state(run_id, state)
        return state

    @contextmanager
    def _locked(self, run_id: str):
        RunRequest(run_id=run_id)
        initial = self.store.load_state(run_id)
        try:
            with self.store.target_lock(initial["target_id"]):
                # Another live server may have skipped an in-flight run during
                # startup and survived its owner's crash. Reconcile the whole
                # target under its lock before admitting any new operation.
                self.store.recover_target(initial["target_id"])
                with self.store.run_lock(run_id):
                    state = self._recover_state(run_id)
                    yield state, self.registry.get(state["target_id"])
        except (BlockingIOError, StoreBusyError):
            raise SessionError("target_busy", "Another diagnostic operation owns this target") from None
        except (StorageError, OSError):
            self._unavailable_targets.add(initial["target_id"])
            try:
                self.store.quarantine_target(initial["target_id"], "Local commit failed; remote state must be reviewed")
            except (StorageError, OSError):
                pass
            raise

    def _preflight(self, state: dict, target) -> tuple[str, str] | None:
        if self.store.cancel_requested(state["run_id"]) and state["state"] != "reported":
            state["state"], state["environment"] = "cancelled", None
        if state["target_id"] in self._unavailable_targets or state["remote_state"] == "unknown":
            return "remote_state_unknown", "Target completion state is unconfirmed"
        if state["state"] in ("reported", "blocked", "expired", "cancelled"):
            return "policy_denied", "Run is terminal; only report access is allowed"
        if state["instance_id"] != self.instance_id:
            state["state"] = "blocked"
            return "state_changed", "Start a new run after service restart; prior observations are not fresh"
        elapsed = self.clock() - state["created_mono"]
        if elapsed < 0 or elapsed >= self.run_ttl:
            state["state"] = "expired"
            return "run_expired", "Run has expired"
        if elapsed >= self.active_budget or state["remote_calls"] >= self.max_calls:
            state["state"] = "blocked"
            return "resource_limit", "Diagnostic time or call budget exhausted"
        if self._target_digest(target) != state["target_digest"]:
            state["state"], state["environment"] = "blocked", None
            return "state_changed", "Registered target or trust store changed"
        return None

    def _record(
        self, state: dict, *, status: str, assessment: str = "unknown",
        observations: dict | None = None, code: str | None = None,
        message: str = "", duration_ms: int = 0, truncated: bool = False,
        operation_id: str | None = None,
    ) -> ToolResult:
        if self.store.cancel_requested(state["run_id"]) and state["state"] != "reported":
            state["state"], state["environment"] = "cancelled", None
        evidence_id = "ev-" + uuid4().hex
        error = None
        if status != "ok":
            try:
                error_code = TypeAdapter(ErrorCode).validate_python(code)
            except ValueError:
                error_code = "invalid_request"
            error = DiagnosticError(code=error_code, message=sanitize(message or error_code))
        pending = state.get("pending_operation")
        if pending is not None:
            operation_id = pending["operation_id"]
            duration_ms = pending.get("duration_ms", duration_ms)
        result = ToolResult(
            tool_version=__version__, policy_version=POLICY_VERSION,
            profile_version=state["profile_version"], run_id=state["run_id"], target_id=state["target_id"],
            execution_id="exec-" + uuid4().hex, request_id=operation_id or "req-" + uuid4().hex,
            status=status, assessment=assessment, observations=sanitize(observations or {}), error=error,
            evidence_ids=(evidence_id,), collected_at=self.utcnow(), duration_ms=max(0, duration_ms),
            completeness="partial" if status != "ok" or truncated else "complete", truncated=truncated,
        )
        record = EvidenceRecord(evidence_id=evidence_id, result=result)
        state["evidence"].append(json.loads(record.model_dump_json(warnings="error")))
        if pending and pending["tool"] == "read_device_identity":
            state["operations"][operation_id] = {
                "arguments": pending["arguments"], "result": json.loads(result.model_dump_json()),
            }
        # The result AND idempotency cache must be durable before closing intent.
        self.store.save_state(state["run_id"], state)
        if pending:
            self.store.append_event(state["run_id"], {
                "action": "after", "operation_id": operation_id, "status": status,
                "remote_state_unknown": state["remote_state"] == "unknown",
                "duration_ms": duration_ms, "at": self.utcnow().isoformat(),
            })
            state["pending_operation"] = None
            self.store.save_state(state["run_id"], state)
        self._write_report(state)
        return ToolResult.model_validate_json(result.model_dump_json(warnings="error"))

    def _deny(self, state: dict, code: str, message: str, operation_id: str | None = None) -> ToolResult:
        return self._record(state, status="blocked", code=code, message=message, operation_id=operation_id)

    def _quarantine(self, state: dict) -> None:
        state["state"], state["remote_state"], state["environment"] = "blocked", "unknown", None
        self._unavailable_targets.add(state["target_id"])
        self.store.quarantine_target(state["target_id"], "Remote operation completion is uncertain")

    def _remote(self, state: dict, target, operation: str, request: dict, operation_id: str) -> ProbeResponse:
        """Durable pending intent precedes the only transport call site."""
        started = self.clock()
        self.store.append_event(state["run_id"], {
            "action": "before", "operation_id": operation_id, "tool": operation,
            "request": request, "at": self.utcnow().isoformat(),
        })
        state["remote_calls"] += 1
        state["remote_state"] = "running"
        state["pending_operation"] = {"operation_id": operation_id, "tool": operation}
        if operation == "read_device_identity":
            state["pending_operation"]["arguments"] = {
                "bus_ref": "bus-" + str(request["bus"]), "address": request["address"],
            }
        self.store.save_state(state["run_id"], state)
        # Durable intent may itself take time. Never dispatch using the budget
        # or freshness snapshot from before its fsyncs.
        now = self.clock()
        elapsed = now - state["created_mono"]
        timeout = min(self.tool_timeout - (now - started), self.active_budget - elapsed,
                      self.run_ttl - elapsed)
        expired = elapsed < 0 or elapsed >= self.run_ttl
        stale = (operation == "read_device_identity" and
                 not 0 <= now - state.get("environment_at", float("-inf")) < self.freshness)
        if expired or timeout <= 0 or stale:
            state["remote_state"] = "idle"  # No transport call has occurred.
            state["state"] = "expired" if expired else "blocked"
            state["environment"] = None
            state["pending_operation"]["duration_ms"] = max(0, int((now - started) * 1000))
            return ProbeResponse(
                status="blocked", error_code=("run_expired" if expired else "policy_denied" if stale else "resource_limit"),
                message="Diagnostic deadline or environment freshness expired before dispatch",
            )
        try:
            response = self.transport.execute(
                target, request, timeout=timeout, output_limit=self.output_limit,
                cancelled=lambda: self.store.cancel_requested(state["run_id"]),
            )
        except Exception:
            # Never expose exceptions that may contain credentials or shell text.
            response = ProbeResponse(
                status="error", error_code="remote_state_unknown", observations={},
                message="Transport failed after execution intent was recorded", remote_state_unknown=True,
            )
        except BaseException:
            # Preserve host interrupt semantics, but quarantine before the host
            # can catch the interrupt and continue with a different run.
            self._quarantine(state)
            self.store.save_state(state["run_id"], state)
            raise
        if response.remote_state_unknown:
            self._quarantine(state)
        else:
            state["remote_state"] = "idle"
        self.store.save_state(state["run_id"], state)
        state["pending_operation"]["duration_ms"] = max(0, int((self.clock() - started) * 1000))
        return response

    @staticmethod
    def _boot_valid(observations: dict) -> bool:
        return (isinstance(observations.get("boot_id"), str) and bool(observations["boot_id"])
                and isinstance(observations.get("host_key_fingerprint"), str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", observations["host_key_fingerprint"]) is not None)

    def inspect_target(self, run_id: str) -> ToolResult:
        with self._locked(run_id) as (state, target):
            if denied := self._preflight(state, target):
                return self._deny(state, *denied)
            previous_binding = state["binding"]
            # A failed refresh cannot erase the run's original identity and
            # subsequently permit rebinding to a different boot.
            state["environment"], state["state"] = None, "created"
            response = self._remote(state, target, "inspect_target", {"op": "inspect_target"}, "op-" + uuid4().hex)
            if response.status == "ok" and not response.truncated and self._boot_valid(response.observations):
                if previous_binding is not None and any(
                    response.observations.get(key) != previous_binding.get(key)
                    for key in ("boot_id", "host_key_fingerprint", "board_model")
                ):
                    state["state"] = "blocked"
                    return self._record(state, status="blocked", code="state_changed",
                                        observations=response.observations,
                                        message="Target identity changed; start a new run")
                state["binding"] = {
                    "boot_id": response.observations["boot_id"],
                    "host_key_fingerprint": response.observations["host_key_fingerprint"],
                    "board_model": response.observations.get("board_model"),
                    "expected_connection": (isinstance(response.observations.get("board_model"), str)
                                            and response.observations["board_model"].startswith("Raspberry Pi 3 ")),
                }
                if not response.remote_state_unknown:
                    state["state"] = "connected"
            elif response.status == "ok" and not response.truncated:
                return self._record(state, status="error", code="invalid_request", message="Missing boot or trusted host identity")
            return self._record(state, status=response.status, observations=response.observations,
                                code=response.error_code, message=response.message, truncated=response.truncated)

    def inspect_i2c_environment(self, run_id: str) -> ToolResult:
        with self._locked(run_id) as (state, target):
            if denied := self._preflight(state, target):
                return self._deny(state, *denied)
            if state["binding"] is None:
                return self._deny(state, "policy_denied", "Inspect the target before its I2C environment")
            state["environment"] = None
            request = {"op": "inspect_i2c_environment", "bus": target.expected_bus,
                       "addresses": list(target.expected_addresses)}
            response = self._remote(state, target, "inspect_i2c_environment", request, "op-" + uuid4().hex)
            if response.status == "ok" and not response.truncated and not response.remote_state_unknown:
                env = response.observations
                if not self._same_binding(state, env):
                    state["state"] = "blocked"
                    return self._record(state, status="blocked", code="state_changed", message="Target boot or host changed")
                state["environment"] = sanitize(env)
                state["environment_at"] = self.clock()
                state["state"] = "probe_ready"
            return self._record(state, status=response.status, observations=response.observations,
                                code=response.error_code, message=response.message, truncated=response.truncated)

    @staticmethod
    def _same_binding(state: dict, observations: dict) -> bool:
        binding = state["binding"]
        return (
            binding is not None and observations.get("boot_id") == binding["boot_id"]
            and observations.get("host_key_fingerprint") == binding["host_key_fingerprint"]
        )

    def read_device_identity(self, run_id: str, bus_ref: str, address: int, operation_id: str) -> ToolResult:
        request_model = IdentityReadRequest(run_id=run_id, bus_ref=bus_ref, address=address, operation_id=operation_id)
        with self._locked(run_id) as (state, target):
            identity = {"bus_ref": request_model.bus_ref, "address": address}
            existing = state["operations"].get(operation_id)
            if existing:
                if existing["arguments"] != identity:
                    return self._deny(state, "operation_conflict", "Operation ID already names a different request")
                # Cached evidence is returned with its original timestamp, never a new reading.
                return ToolResult.model_validate_json(json.dumps(existing["result"]))
            if denied := self._preflight(state, target):
                return self._deny(state, *denied, operation_id=operation_id)
            if self.store.is_quarantined(state["target_id"]) or state["target_id"] in self._unavailable_targets:
                return self._deny(state, "remote_state_unknown", "Target is quarantined; no new device reads")
            profile = load_component_profile(state["profile_id"])
            env = state["environment"] or {}
            fresh = (state.get("environment_at") is not None and
                     0 <= self.clock() - state["environment_at"] < self.freshness)
            context = IdentityReadContext(
                profile_id=state["profile_id"], mode=state["mode"],
                state="probe_ready" if state["state"] == "identified" else state["state"],
                target_registered=True,
                expected_connection=(state["binding"] or {}).get("expected_connection") is True,
                expected_addresses=target.expected_addresses,
                bus_validated=(bus_ref == "bus-" + str(target.expected_bus)
                               and type(env.get("bus")) is int and env["bus"] == target.expected_bus
                               and env.get("adapter_present") is True),
                preconditions_fresh=fresh,
                access_allowed=self._bool_or_none(env.get("access_allowed")),
                driver_bound=self._bool_or_none(
                    env["driver_bound"].get(str(address)) if type(env.get("driver_bound")) is dict else None
                ),
                conflicting_use=False if target.conflicting_use_excluded else None,
                combined_transfer_supported=self._bool_or_none(env.get("combined_transfer_supported")),
                dependency_available=env.get("dependency_available") is True,
                budget_available=True, remote_state=state["remote_state"],
            )
            decision = authorize_identity_read(profile, context, address)
            if not decision.allowed:
                return self._deny(state, "policy_denied", decision.reason, operation_id=operation_id)
            fingerprint = env.get("adapter_fingerprint")
            if not isinstance(fingerprint, str) or not fingerprint:
                return self._deny(state, "policy_denied", "Missing adapter fingerprint", operation_id)
            request = {
                "op": "read_device_identity", "bus": target.expected_bus,
                "addresses": list(target.expected_addresses), "address": address,
                "expected_boot_id": state["binding"]["boot_id"],
                "expected_adapter_fingerprint": fingerprint,
            }
            response = self._remote(state, target, "read_device_identity", request, operation_id)
            assessment = "unknown"
            if response.status == "ok" and not response.truncated and not response.remote_state_unknown:
                if (not self._same_binding(state, response.observations)
                        or response.observations.get("adapter_fingerprint") != fingerprint):
                    state["state"], state["environment"] = "blocked", None
                    result = self._record(state, status="blocked", code="state_changed", message="Read target changed",
                                          operation_id=operation_id)
                    return result
                try:
                    if (type(response.observations.get("address")) is not int
                            or response.observations["address"] != address
                            or type(response.observations.get("register")) is not int
                            or response.observations["register"] != profile.identity.register_address):
                        raise ValueError("identity response is not bound to requested register and address")
                    assessment = evaluate_identity(profile, address, response.observations.get("identity_value"))
                except ValueError:
                    state["state"], state["environment"] = "blocked", None
                    result = self._record(state, status="error", code="invalid_request",
                                          message="Malformed identity observation", operation_id=operation_id)
                    return result
                state["state"] = "identified"
            elif response.status != "ok" or response.truncated or response.remote_state_unknown:
                # In particular an I2C timeout/failure cannot be followed by an automatic retry.
                if state["state"] != "expired":
                    state["state"] = "blocked"
                state["environment"] = None
            result = self._record(state, status=response.status, assessment=assessment,
                                  observations=response.observations, code=response.error_code,
                                  message=response.message, truncated=response.truncated, operation_id=operation_id)
            return result

    @staticmethod
    def _bool_or_none(value):
        return value if type(value) is bool else None

    def _findings(self, state: dict) -> tuple[Finding, ...]:
        records = [EvidenceRecord.model_validate_json(json.dumps(record)) for record in state["evidence"]]
        findings = []
        identity_executions = {operation["result"]["execution_id"] for operation in state["operations"].values()}
        for record in records:
            result = record.result
            if (result.execution_id in identity_executions and "identity_value" in result.observations
                    and result.status == "ok" and result.assessment in ("pass", "fail") and not result.truncated):
                address = result.observations.get("address")
                try:
                    assessment = evaluate_identity(load_component_profile(state["profile_id"]), address,
                                                   result.observations["identity_value"])
                except ValueError:
                    assessment = "unknown"
                explanation = {
                    "pass": "Response matches the expected identity; accuracy and authenticity remain unverified.",
                    "fail": "Response differs from the expected identity; this does not establish a counterfeit or dead sensor.",
                    "unknown": "Identity could not be classified from the available observation.",
                }[assessment]
                rule_id = "identity-match"
            elif result.error:
                assessment, rule_id = "unknown", result.error.code
                explanation = "Inspection was unavailable or blocked: " + result.error.code + ". No sensor-failure conclusion."
            else:
                continue
            findings.append(Finding(finding_id="finding-" + record.evidence_id, rule_id=rule_id,
                                    assessment=assessment, explanation=explanation, evidence_ids=(record.evidence_id,)))
        return tuple(findings)

    def evaluate_findings(self, run_id: str) -> tuple[Finding, ...]:
        RunRequest(run_id=run_id)
        with self.store.run_lock(run_id):
            return self._findings(self._recover_state(run_id))

    def _build_report(self, state: dict, hypotheses: tuple[Hypothesis, ...] = ()) -> DiagnosticReport:
        records = tuple(EvidenceRecord.model_validate_json(json.dumps(record)) for record in state["evidence"])
        findings = self._findings(state)
        environment = state["environment"] or {}
        # Historical identity facts remain in findings, but completion refers
        # to the currently inspected boot, host and adapter only.
        current_identity = {
            record.evidence_id for record in records
            if state["binding"] is not None and environment
            and self._same_binding(state, record.result.observations)
            and record.result.observations.get("adapter_fingerprint") == environment.get("adapter_fingerprint")
        }
        current_findings = tuple(
            finding for finding in findings
            if finding.rule_id == "identity-match" and set(finding.evidence_ids).intersection(current_identity)
        )
        has_identity = any(f.assessment == "pass" for f in current_findings)
        attempted_addresses = {
            record.result.observations.get("address") for record in records
            if any(record.evidence_id in f.evidence_ids for f in current_findings)
        }
        coverage_complete = has_identity or set(state["expected_addresses"]).issubset(attempted_addresses)
        outcome = ("interrupted" if state["state"] in ("cancelled", "expired")
                   or state["remote_state"] == "unknown" else "partial")
        if (outcome != "interrupted" and coverage_complete
                and all(r.result.status == "ok" and not r.result.truncated for r in records)):
            outcome = "complete"
        report = DiagnosticReport(
            run_id=state["run_id"], target_id=state["target_id"], outcome=outcome, evidence=records,
            findings=findings, hypotheses=hypotheses,
            limitations=(
                "Electrical voltages, pull-ups and physical wiring were not measured.",
                "Identity response does not prove authenticity, measurement accuracy or full sensor health.",
                "Only the registered adapter and expected addresses were inspected; no whole-bus scan was performed.",
            ), next_actions=("Review the observed facts and unresolved conditions before changing the system.",),
        )
        return report

    def _write_report(self, state: dict, hypotheses: tuple[Hypothesis, ...] = ()) -> DiagnosticReport:
        report = self._build_report(state, hypotheses)
        self.store.save_report(state["run_id"], json.loads(report.model_dump_json(warnings="error")))
        return DiagnosticReport.model_validate_json(report.model_dump_json(warnings="error"))

    def finalize_report(self, run_id: str, hypotheses: tuple[Hypothesis, ...] = ()) -> DiagnosticReport:
        request = FinalizeReportRequest(run_id=run_id, hypotheses=hypotheses)
        cleaned = sanitize(json.loads(request.model_dump_json(warnings="error")))
        request = FinalizeReportRequest.model_validate_json(json.dumps(cleaned))
        with self.store.run_lock(run_id):
            state = self._recover_state(run_id)
            # Validate model-authored references before making the run terminal.
            report = self._write_report(state, request.hypotheses)
            if state["state"] not in ("blocked", "expired", "cancelled"):
                state["state"] = "reported"
            self.store.save_state(run_id, state)
            return report

    def get_report(self, run_id: str) -> DiagnosticReport:
        RunRequest(run_id=run_id)
        with self.store.run_lock(run_id):
            state = self._recover_state(run_id)
            try:
                saved = DiagnosticReport.model_validate_json(json.dumps(self.store.load_report(run_id)))
                hypotheses = saved.hypotheses
            except FileNotFoundError:
                # State may be committed even if the first report write failed.
                hypotheses = ()
            return self._write_report(state, hypotheses)

    def cancel_diagnosis(self, run_id: str) -> DiagnosticReport:
        RunRequest(run_id=run_id)
        initial = self.store.load_state(run_id)
        if initial["state"] == "reported":
            return self.get_report(run_id)
        # This marker uses an independent lock: it must reach a running worker.
        self.store.request_cancel(run_id)
        try:
            with self._locked(run_id) as (state, target):
                if state["state"] != "reported":
                    state["state"], state["environment"] = "cancelled", None
                self.store.save_state(run_id, state)
                return self._write_report(state)
        except SessionError as exc:
            if exc.code != "target_busy":
                raise
            # Atomic state reads provide the last durable evidence. The worker
            # will commit the final outcome; never write over its locked state.
            snapshot = self.store.load_state(run_id)
            snapshot["state"] = "cancelled"
            return self._build_report(snapshot)

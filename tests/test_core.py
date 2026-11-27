"""Core integration tests use real private storage and no SSH or hardware."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from embedsleuth.contracts import Hypothesis
from embedsleuth.core import DiagnosticCore
from embedsleuth.storage import RunStore
from embedsleuth.targets import RegisteredTarget, TargetRegistry
from embedsleuth.transport import ProbeResponse


FINGERPRINT = "sha256:" + "a" * 64


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def utcnow(self):
        return datetime(2026, 11, 18, tzinfo=timezone.utc) + timedelta(seconds=self.now)


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.overrides = {}

    def execute(self, target, request, **limits):
        self.calls.append((deepcopy(request), limits))
        override = self.overrides.get(request["op"])
        if isinstance(override, Exception):
            raise override
        common = {"boot_id": "boot-1", "host_key_fingerprint": FINGERPRINT}
        if request["op"] == "inspect_target":
            observations = common | {"board_model": "Raspberry Pi 3 Model B Rev 1.2"}
        else:
            observations = common | {
                "bus": 1, "adapter_present": True, "access_allowed": True,
                "driver_bound": {"104": False, "105": False},
                "combined_transfer_supported": True, "dependency_available": True,
                "adapter_fingerprint": "b" * 64,
            }
            if request["op"] == "read_device_identity":
                observations |= {"address": request["address"], "identity_value": 104, "register": 117}
        if isinstance(override, ProbeResponse):
            return override
        observations.update(deepcopy(override or {}))
        return ProbeResponse(observations=observations)

    def count(self, operation):
        return sum(request["op"] == operation for request, _ in self.calls)


@pytest.fixture
def make_harness(tmp_path):
    stores = []

    def make(*, target_changes=None, **limits):
        directory = tmp_path / str(len(stores))
        directory.mkdir()
        trust = directory / "known_hosts"
        trust.write_text("registered trust snapshot\n")
        values = {
            "target_id": "lab-pi", "host": "pi.local", "username": "pi",
            "known_hosts_path": trust, "conflicting_use_excluded": True,
        }
        values.update(target_changes or {})
        target = RegisteredTarget(**values)
        registry = TargetRegistry((target,))
        store = RunStore(directory / "store")
        stores.append(store)
        clock, transport = Clock(), FakeTransport()
        core = DiagnosticCore(registry, transport, store, clock=clock, utcnow=clock.utcnow, **limits)
        return SimpleNamespace(core=core, store=store, clock=clock, transport=transport,
                               target=target, registry=registry, trust=trust)

    yield make
    for store in stores:
        store.close()


def ready(harness, *, mode="read_only"):
    run = harness.core.start_diagnosis("lab-pi", mode=mode)["run_id"]
    assert harness.core.inspect_target(run).status == "ok"
    assert harness.core.inspect_i2c_environment(run).status == "ok"
    return run


def read(harness, run, *, address=104, operation="identity-1", bus="bus-1"):
    return harness.core.read_device_identity(run, bus, address, operation)


@pytest.mark.parametrize("address", [104, 105])
def test_happy_diagnosis_has_real_identity_evidence_and_persisted_report(make_harness, address):
    h = make_harness()
    run = ready(h)
    result = read(h, run, address=address)
    assert (result.status, result.assessment) == ("ok", "pass")
    report = h.core.finalize_report(run)
    assert report.outcome == "complete"
    identity = [finding for finding in report.findings if finding.rule_id == "identity-match"]
    assert len(identity) == 1 and identity[0].assessment == "pass"
    assert identity[0].evidence_ids == result.evidence_ids
    assert h.core.get_report(run) == report
    assert h.store.load_state(run)["state"] == "reported"
    request = h.transport.calls[-1][0]
    assert request["expected_boot_id"] == "boot-1"
    assert request["expected_adapter_fingerprint"] == "b" * 64
    assert not {"command", "register", "force"}.intersection(request)


def test_final_report_lookup_preserves_snapshot_without_rejudging_or_writing(make_harness, monkeypatch):
    h = make_harness()
    run = ready(h)
    read(h, run)
    report = h.core.finalize_report(run)

    def forbidden(*args, **kwargs):
        raise AssertionError("A finalized snapshot must not be rejudged or rewritten on lookup")

    monkeypatch.setattr(h.core, "_build_report", forbidden)
    monkeypatch.setattr(h.store, "save_report", forbidden)
    assert h.core.get_report(run) == report


def test_final_report_lookup_still_detects_new_interruption(make_harness):
    h = make_harness()
    run = ready(h)
    read(h, run)
    assert h.core.finalize_report(run).outcome == "complete"
    state = h.store.load_state(run)
    state["remote_state"] = "unknown"
    h.store.save_state(run, state)
    assert h.core.get_report(run).outcome == "interrupted"
    assert h.store.is_quarantined(h.target.target_id)


def test_identity_mismatch_does_not_claim_dead_sensor_or_complete_missing_coverage(make_harness):
    h = make_harness()
    h.transport.overrides["read_device_identity"] = {"identity_value": 0}
    run = ready(h)
    assert read(h, run).assessment == "fail"
    assert h.core.get_report(run).outcome == "partial"
    assert read(h, run, address=105, operation="identity-2").assessment == "fail"
    report = h.core.finalize_report(run)
    assert report.outcome == "complete"
    assert all(f.assessment == "fail" for f in report.findings)
    assert all("does not establish" in f.explanation for f in report.findings)


def test_passive_mode_collects_environment_but_never_reads_sensor(make_harness):
    h = make_harness()
    run = ready(h, mode="passive")
    result = read(h, run)
    assert result.status == "blocked" and result.error.code == "policy_denied"
    assert h.transport.count("read_device_identity") == 0
    assert h.core.get_report(run).outcome == "partial"


def test_unregistered_target_is_rejected_before_creating_or_connecting(make_harness):
    h = make_harness()
    with pytest.raises(ValueError, match="not registered"):
        h.core.start_diagnosis("other-target")
    assert h.store.list_runs() == [] and h.transport.calls == []


def test_no_remote_sensor_access_before_environment_inspection(make_harness):
    h = make_harness()
    run = h.core.start_diagnosis("lab-pi")["run_id"]
    assert read(h, run).status == "blocked"
    assert h.core.inspect_i2c_environment(run).status == "blocked"
    h.core.inspect_target(run)
    assert read(h, run, operation="identity-2").status == "blocked"
    assert h.transport.count("read_device_identity") == 0


@pytest.mark.parametrize("changed", [
    {"adapter_present": False}, {"access_allowed": False}, {"access_allowed": None},
    {"access_allowed": "true"}, {"driver_bound": {"104": True, "105": False}},
    {"driver_bound": {"104": None, "105": False}},
    {"combined_transfer_supported": None}, {"combined_transfer_supported": False},
    {"dependency_available": False}, {"adapter_fingerprint": None}, {"bus": 0},
])
def test_missing_or_unsafe_observations_fail_closed(make_harness, changed):
    h = make_harness()
    h.transport.overrides["inspect_i2c_environment"] = changed
    run = ready(h)
    result = read(h, run)
    assert result.status == "blocked" and result.assessment == "unknown"
    assert h.transport.count("read_device_identity") == 0


def test_unknown_external_conflict_declaration_blocks_read(make_harness):
    h = make_harness(target_changes={"conflicting_use_excluded": False})
    assert read(h, ready(h)).status == "blocked"
    assert h.transport.count("read_device_identity") == 0


@pytest.mark.parametrize("address,bus", [(105, "bus-1"), (104, "bus-0"), (112, "bus-1")])
def test_read_is_bound_to_registered_address_and_bus(make_harness, address, bus):
    h = make_harness(target_changes={"expected_addresses": (104,)})
    assert read(h, ready(h), address=address, bus=bus).status == "blocked"
    assert h.transport.count("read_device_identity") == 0


def test_operation_replay_returns_original_evidence_without_reexecution(make_harness):
    h = make_harness()
    run = ready(h)
    first = read(h, run)
    h.clock.now += 2
    replay = read(h, run)
    assert replay == first
    assert h.transport.count("read_device_identity") == 1
    assert len(h.core.get_report(run).evidence) == 3
    conflict = read(h, run, address=105)
    assert conflict.error.code == "operation_conflict"
    assert h.transport.count("read_device_identity") == 1


def test_stale_environment_requires_refresh_before_any_read(make_harness):
    h = make_harness()
    run = ready(h)
    h.clock.now += 30
    assert read(h, run).status == "blocked"
    assert h.transport.count("read_device_identity") == 0
    h.core.inspect_i2c_environment(run)
    assert read(h, run, operation="identity-2").assessment == "pass"


@pytest.mark.parametrize("limits,elapsed,code,state", [
    ({}, 120, "resource_limit", "blocked"),
    ({"run_ttl": 5}, 5, "run_expired", "expired"),
    ({}, -1, "run_expired", "expired"),
])
def test_time_and_clock_boundaries_stop_remote_execution(make_harness, limits, elapsed, code, state):
    h = make_harness(**limits)
    run = ready(h)
    h.clock.now += elapsed
    assert read(h, run).error.code == code
    assert h.store.load_state(run)["state"] == state
    assert h.transport.count("read_device_identity") == 0


def test_remote_call_budget_is_shared_across_inspections_and_reads(make_harness):
    h = make_harness(max_calls=2)
    run = ready(h)
    assert read(h, run).error.code == "resource_limit"
    assert len(h.transport.calls) == 2


def test_transport_deadline_is_reduced_to_remaining_diagnostic_budget(make_harness):
    h = make_harness(freshness=120)
    run = ready(h)
    h.clock.now += 119
    assert read(h, run).assessment == "pass"
    assert h.transport.calls[-1][1]["timeout"] == 1.0
    assert h.transport.calls[-1][1]["output_limit"] == 65536
    assert callable(h.transport.calls[-1][1]["cancelled"])


@pytest.mark.parametrize("changed", [
    {"boot_id": "boot-2"}, {"host_key_fingerprint": "sha256:" + "c" * 64},
])
def test_environment_from_changed_host_or_boot_is_rejected(make_harness, changed):
    h = make_harness()
    run = h.core.start_diagnosis("lab-pi")["run_id"]
    h.core.inspect_target(run)
    h.transport.overrides["inspect_i2c_environment"] = changed
    assert h.core.inspect_i2c_environment(run).error.code == "state_changed"
    assert read(h, run).status == "blocked"
    assert h.transport.count("read_device_identity") == 0


def test_trust_file_change_invalidates_existing_preconditions_without_connecting(make_harness):
    h = make_harness()
    run = ready(h)
    h.trust.write_text("changed trust snapshot\n")
    assert read(h, run).error.code == "state_changed"
    assert h.transport.count("read_device_identity") == 0


@pytest.mark.parametrize("changed", [{"boot_id": "boot-2"}, {"adapter_fingerprint": "c" * 64}])
def test_read_result_from_changed_adapter_or_boot_never_passes(make_harness, changed):
    h = make_harness()
    run = ready(h)
    h.transport.overrides["read_device_identity"] = changed
    assert read(h, run).error.code == "state_changed"
    assert h.core.get_report(run).outcome == "partial"
    assert not any(f.assessment == "pass" for f in h.core.evaluate_findings(run))


@pytest.mark.parametrize("response", [
    ProbeResponse(status="error", error_code="transfer_failed", message="No response"),
    ProbeResponse(status="timeout", error_code="remote_state_unknown", message="Unknown completion",
                  remote_state_unknown=True),
    ProbeResponse(status="error", error_code="resource_limit", message="Output truncated",
                  remote_state_unknown=True, truncated=True),
])
def test_failed_identity_cannot_be_retried_with_new_operation_id(make_harness, response):
    h = make_harness()
    run = ready(h)
    h.transport.overrides["read_device_identity"] = response
    first = read(h, run)
    assert first.assessment == "unknown"
    assert read(h, run, operation="identity-2").status == "blocked"
    assert h.transport.count("read_device_identity") == 1
    assert read(h, run) == first
    assert not any(f.assessment == "fail" for f in h.core.evaluate_findings(run))


def test_malformed_identity_byte_invalidates_environment_and_blocks_new_id_retry(make_harness):
    h = make_harness()
    run = ready(h)
    h.transport.overrides["read_device_identity"] = {"identity_value": "104"}
    assert read(h, run).error.code == "invalid_request"
    assert read(h, run, operation="identity-2").status == "blocked"
    assert h.transport.count("read_device_identity") == 1


def test_transport_exception_quarantines_target_without_leaking_exception_text(make_harness):
    h = make_harness()
    run = ready(h)
    h.transport.overrides["read_device_identity"] = RuntimeError("password=do-not-leak")
    result = read(h, run)
    assert result.error.code == "remote_state_unknown"
    assert "do-not-leak" not in result.model_dump_json()
    assert h.store.is_quarantined("lab-pi")
    new_run = h.core.start_diagnosis("lab-pi")["run_id"]
    assert h.core.inspect_target(new_run).status == "blocked"
    assert read(h, new_run).error.code == "remote_state_unknown"
    assert h.transport.count("read_device_identity") == 1


def test_spoofed_identity_keys_in_target_metadata_do_not_complete_diagnosis(make_harness):
    h = make_harness()
    h.transport.overrides["inspect_target"] = {"identity_value": 104, "address": 104}
    run = h.core.start_diagnosis("lab-pi")["run_id"]
    h.core.inspect_target(run)
    report = h.core.get_report(run)
    assert report.outcome == "partial"
    assert not any(f.rule_id == "identity-match" for f in report.findings)


def test_cancelling_successful_unfinalized_run_remains_interrupted(make_harness):
    h = make_harness()
    run = ready(h)
    read(h, run)
    assert h.core.cancel_diagnosis(run).outcome == "interrupted"
    assert read(h, run, operation="identity-2").status == "blocked"
    assert h.core.finalize_report(run).outcome == "interrupted"


def test_returned_result_and_report_mutations_do_not_modify_authoritative_evidence(make_harness):
    h = make_harness()
    run = ready(h)
    result = read(h, run)
    result.observations["identity_value"] = 0
    result.observations["driver_bound"]["104"] = True
    report = h.core.get_report(run)
    report.evidence[-1].result.observations["identity_value"] = 1
    assert read(h, run).observations["identity_value"] == 104
    fresh_report = h.core.get_report(run)
    assert fresh_report.evidence[-1].result.observations["identity_value"] == 104
    assert fresh_report.findings[-1].assessment == "pass"


def test_unknown_hypothesis_reference_does_not_finalize_run(make_harness):
    h = make_harness()
    run = ready(h)
    read(h, run)
    hypothesis = Hypothesis(hypothesis_id="h-1", explanation="Claim", supporting_evidence=("missing",))
    with pytest.raises(ValidationError, match="unknown evidence"):
        h.core.finalize_report(run, (hypothesis,))
    assert h.store.load_state(run)["state"] == "identified"


def test_service_restart_allows_cached_evidence_but_requires_new_run_for_new_read(make_harness):
    h = make_harness()
    run = ready(h)
    first = read(h, run)
    restarted = DiagnosticCore(h.registry, h.transport, h.store, clock=h.clock, utcnow=h.clock.utcnow)
    assert restarted.read_device_identity(run, "bus-1", 104, "identity-1") == first
    denied = restarted.read_device_identity(run, "bus-1", 104, "identity-2")
    assert denied.error.code == "state_changed"
    assert h.transport.count("read_device_identity") == 1


def test_pending_intent_recovery_blocks_original_and_new_runs_without_replay(make_harness):
    h = make_harness()
    run = ready(h)
    h.store.append_event(run, {"action": "before", "operation_id": "interrupted-read", "tool": "read_device_identity"})
    state = h.store.load_state(run)
    state["remote_state"] = "running"
    h.store.save_state(run, state)
    with RunStore(h.store.root) as recovered_store:
        restarted = DiagnosticCore(h.registry, h.transport, recovered_store,
                                   clock=h.clock, utcnow=h.clock.utcnow)
        assert recovered_store.is_quarantined("lab-pi")
        assert restarted.read_device_identity(run, "bus-1", 104, "interrupted-read").error.code == "remote_state_unknown"
        h.core = restarted
        new_run = ready(h)
        assert read(h, new_run).error.code == "remote_state_unknown"
        assert h.transport.count("read_device_identity") == 0


def test_model_supplied_execution_flags_are_rejected_before_any_transport_call(make_harness):
    h = make_harness()
    run = ready(h)
    with pytest.raises(ValidationError):
        h.core.call("read_device_identity", {
            "run_id": run, "bus_ref": "bus-1", "address": 104, "operation_id": "identity-1",
            "approved": True,
        })
    assert h.transport.count("read_device_identity") == 0


@pytest.mark.parametrize("model", [None, "Raspberry Pi 4 Model B", "unidentified board"])
def test_unexpected_board_is_observed_but_never_authorizes_sensor_io(make_harness, model):
    h = make_harness()
    h.transport.overrides["inspect_target"] = {"board_model": model}
    run = ready(h)
    assert read(h, run).status == "blocked"
    assert h.transport.count("read_device_identity") == 0


@pytest.mark.parametrize("changed", [
    {"address": 105}, {"address": "104"}, {"address": True},
    {"register": 118}, {"register": "117"}, {"register": True},
    {"register": None}, {"identity_value": True},
])
def test_identity_response_is_bound_to_exact_address_register_and_byte(make_harness, changed):
    h = make_harness()
    run = ready(h)
    h.transport.overrides["read_device_identity"] = changed
    result = read(h, run)
    assert result.status == "error" and result.error.code == "invalid_request"
    assert read(h, run, operation="identity-2").status == "blocked"
    assert h.transport.count("read_device_identity") == 1
    assert not any(f.assessment == "pass" for f in h.core.evaluate_findings(run))


def test_remote_intent_is_durable_before_transport_and_closed_only_after_result_commit(make_harness):
    h = make_harness()
    run = ready(h)
    original = h.transport.execute

    def check_intent(target, request, **limits):
        state = h.store.load_state(run)
        assert state["remote_state"] == "running"
        assert state["pending_operation"]["tool"] == request["op"]
        last = h.store.read_events(run)[-1]
        assert last["action"] == "before" and last["operation_id"] == "identity-1"
        return original(target, request, **limits)

    h.transport.execute = check_intent
    result = read(h, run)
    assert result.assessment == "pass"
    state = h.store.load_state(run)
    assert state["pending_operation"] is None and state["remote_state"] == "idle"
    assert state["operations"]["identity-1"]["result"]["execution_id"] == result.execution_id
    assert h.store.read_events(run)[-1]["action"] == "after"


def test_cancel_running_read_reaches_worker_without_waiting_for_its_target_lock(make_harness):
    h = make_harness()
    run = ready(h)
    entered, release = Event(), Event()
    original = h.transport.execute

    def wait_for_cancel(target, request, **limits):
        original(target, request, **limits)
        entered.set()
        assert release.wait(3), "test worker was not released"
        assert limits["cancelled"](), "worker must receive persisted cancellation"
        return ProbeResponse(status="blocked", error_code="remote_state_unknown",
                             message="Cancelled with remote completion unknown",
                             remote_state_unknown=True)

    h.transport.execute = wait_for_cancel
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(read, h, run)
        try:
            assert entered.wait(3), "test worker did not start"
            immediate = h.core.cancel_diagnosis(run)
            assert immediate.outcome == "interrupted"
            assert not future.done()
        finally:
            release.set()
        assert future.result(timeout=3).error.code == "remote_state_unknown"
    state = h.store.load_state(run)
    assert state["remote_state"] == "unknown" and state["state"] == "cancelled"
    assert h.core.get_report(run).outcome == "interrupted"
    assert h.store.is_quarantined("lab-pi")
    new_run = h.core.start_diagnosis("lab-pi")["run_id"]
    assert read(h, new_run).status == "blocked"
    assert h.transport.count("read_device_identity") == 1


def test_boolean_bus_observation_cannot_impersonate_registered_bus_one(make_harness):
    h = make_harness()
    h.transport.overrides["inspect_i2c_environment"] = {"bus": True}
    run = ready(h)
    result = read(h, run)
    assert result.status == "blocked" and result.assessment == "unknown"
    assert h.transport.count("read_device_identity") == 0


def test_finalized_hypotheses_are_sanitized_before_return_and_keep_valid_references(make_harness):
    h = make_harness()
    run = ready(h)
    observed = read(h, run)
    hypothesis = Hypothesis(
        hypothesis_id="hypothesis-1",
        explanation="\x1b[31mIdentity matched.\x1b[0m password=do-not-leak",
        supporting_evidence=observed.evidence_ids,
    )
    report = h.core.finalize_report(run, (hypothesis,))
    explanation = report.hypotheses[0].explanation
    assert "Identity matched." in explanation and "[REDACTED]" in explanation
    assert "\x1b" not in explanation and "do-not-leak" not in report.model_dump_json()
    assert report.hypotheses[0].supporting_evidence == observed.evidence_ids
    assert report == h.core.get_report(run)
    assert h.store.load_report(run)["hypotheses"][0]["explanation"] == explanation


@pytest.mark.parametrize("method", ["get_report", "finalize_report", "evaluate_findings"])
def test_persisted_cancel_intent_is_honored_by_read_and_finalize_after_restart(make_harness, method):
    h = make_harness()
    run = ready(h)
    read(h, run)
    h.store.request_cancel(run)
    restarted = DiagnosticCore(h.registry, h.transport, h.store, clock=h.clock, utcnow=h.clock.utcnow)
    getattr(restarted, method)(run)
    assert h.store.load_state(run)["state"] == "cancelled"
    assert restarted.get_report(run).outcome == "interrupted"
    assert h.transport.count("read_device_identity") == 1


def test_finalization_race_does_not_cancel_a_reported_run(make_harness, monkeypatch):
    h = make_harness()
    run = ready(h)
    read(h, run)
    original = h.store.request_cancel
    def finalize_then_mark(run_id):
        h.core.finalize_report(run_id)
        original(run_id)
    monkeypatch.setattr(h.store, "request_cancel", finalize_then_mark)
    assert h.core.cancel_diagnosis(run).outcome == "complete"
    assert h.store.load_state(run)["state"] == "reported"

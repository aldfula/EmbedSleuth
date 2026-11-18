"""Crash-boundary tests using real durable storage and a deterministic peer."""

import pytest

from embedsleuth.core import DiagnosticCore
from embedsleuth.storage import RunStore
from embedsleuth.targets import RegisteredTarget, TargetRegistry
from embedsleuth.transport import ProbeResponse


class RecordingPeer:
    def __init__(self):
        self.calls = []

    def execute(self, target, request, **limits):
        self.calls.append(dict(request))
        observed = {"boot_id": "boot-1", "host_key_fingerprint": "sha256:" + "a" * 64,
                    "board_model": "Raspberry Pi 3 Model B Rev 1.2"}
        if request["op"] != "inspect_target":
            observed.update(bus=1, adapter_present=True, access_allowed=True,
                            driver_bound={"104": False}, combined_transfer_supported=True,
                            dependency_available=True, adapter_fingerprint="adapter-1")
        if request["op"] == "read_device_identity":
            observed.update(address=104, register=0x75, identity_value=104)
        return ProbeResponse(observations=observed)

    def reads(self):
        return sum(call["op"] == "read_device_identity" for call in self.calls)


@pytest.fixture
def rig(tmp_path):
    trust = tmp_path / "known_hosts"
    trust.write_text("fake-peer-fixture")
    target = RegisteredTarget(target_id="pi", host="192.0.2.1", username="pi",
                              known_hosts_path=trust, expected_addresses=(104,),
                              conflicting_use_excluded=True)
    registry = TargetRegistry((target,))
    peer = RecordingPeer()
    with RunStore(tmp_path / "store") as store:
        core = DiagnosticCore(registry, peer, store)
        yield core, store, peer, registry


def _ready(core):
    run_id = core.start_diagnosis("pi")["run_id"]
    assert core.inspect_target(run_id).status == "ok"
    assert core.inspect_i2c_environment(run_id).status == "ok"
    return run_id


def test_identity_result_and_cache_are_durable_before_after_event(rig, monkeypatch):
    core, store, peer, registry = rig
    run_id = _ready(core)
    append = store.append_event
    observed = []
    def verify_before_close(run, event):
        if event.get("action") == "after" and event.get("operation_id") == "op-read":
            state = store.load_state(run)
            cached = state["operations"]["op-read"]["result"]
            assert any(record["result"] == cached for record in state["evidence"])
            assert cached["observations"]["identity_value"] == 104
            observed.append(True)
        return append(run, event)
    monkeypatch.setattr(store, "append_event", verify_before_close)
    assert core.read_device_identity(run_id, "bus-1", 104, "op-read").assessment == "pass"
    assert observed == [True]
    assert peer.reads() == 1


@pytest.mark.parametrize("failure_point", ["before", "response_state", "result_state", "after", "clear_marker"])
def test_commit_failures_never_replay_identity_across_calls_runs_or_restart(rig, monkeypatch, failure_point):
    core, store, peer, registry = rig
    run_id = _ready(core)
    save, append = store.save_state, store.append_event
    failed = []
    def inject_save(run, state):
        pending = state.get("pending_operation")
        is_read = pending is not None and pending["operation_id"] == "op-read"
        cached = "op-read" in state["operations"]
        should_fail = (
            failure_point == "response_state" and is_read and state["remote_state"] == "idle" and not cached
            or failure_point == "result_state" and is_read and cached
            or failure_point == "clear_marker" and pending is None and cached
        )
        if should_fail and not failed:
            failed.append(failure_point)
            raise OSError("injected durable state failure")
        return save(run, state)
    def inject_append(run, event):
        if (failure_point == event.get("action") and event.get("operation_id") == "op-read" and not failed):
            failed.append(failure_point)
            raise OSError("injected audit append failure")
        return append(run, event)
    monkeypatch.setattr(store, "save_state", inject_save)
    monkeypatch.setattr(store, "append_event", inject_append)
    with pytest.raises(OSError, match="injected"):
        core.read_device_identity(run_id, "bus-1", 104, "op-read")
    assert failed == [failure_point]
    reads = peer.reads()
    assert reads == (0 if failure_point == "before" else 1)
    assert store.is_quarantined("pi")
    original_cache = store.load_state(run_id)["operations"].get("op-read")
    core.read_device_identity(run_id, "bus-1", 104, "op-read")
    core.read_device_identity(run_id, "bus-1", 104, "op-other")
    assert peer.reads() == reads
    if original_cache is not None:
        assert store.load_state(run_id)["operations"]["op-read"] == original_cache
    next_run = core.start_diagnosis("pi")["run_id"]
    core.read_device_identity(next_run, "bus-1", 104, "op-next-run")
    assert peer.reads() == reads
    # A newly constructed store performs startup recovery, and a new core has
    # no in-memory unavailable-target set to mask persistence defects.
    with RunStore(store.root) as restarted_store:
        restarted = DiagnosticCore(registry, peer, restarted_store)
        next_run = _ready(restarted)
        assert restarted.read_device_identity(next_run, "bus-1", 104, "op-after-restart").status == "blocked"
        assert peer.reads() == reads


def test_recovery_refreshes_saved_complete_report_to_interrupted(rig, monkeypatch):
    core, store, peer, registry = rig
    run_id = _ready(core)
    assert core.read_device_identity(run_id, "bus-1", 104, "op-first").assessment == "pass"
    assert core.get_report(run_id).outcome == "complete"
    save = store.save_state
    def fail_second_result(run, state):
        pending = state.get("pending_operation")
        if pending and pending["operation_id"] == "op-second" and state["remote_state"] == "idle":
            raise OSError("injected second result failure")
        return save(run, state)
    with monkeypatch.context() as patch:
        patch.setattr(store, "save_state", fail_second_result)
        with pytest.raises(OSError):
            core.read_device_identity(run_id, "bus-1", 104, "op-second")
    with RunStore(store.root) as restored:
        restarted = DiagnosticCore(registry, peer, restored)
        report = restarted.get_report(run_id)
        assert report.outcome == "interrupted"
        assert report.evidence
        assert peer.reads() == 2

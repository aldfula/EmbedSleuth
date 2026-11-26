"""Operator reboot acknowledgement must not become a generic quarantine bypass."""

import json

import pytest

from embedsleuth.storage import RunStore, StorageError

OLD = "11111111-1111-4111-8111-111111111111"
NEW = "22222222-2222-4222-8222-222222222222"
FP = "sha256:" + "a" * 64


@pytest.fixture
def cancelled(tmp_path):
    with RunStore(tmp_path / "state") as store:
        state = {"run_id": "run-1", "target_id": "pi-1", "target_digest": "digest", "mode": "passive",
                 "state": "cancelled", "remote_state": "unknown", "remote_calls": 1, "operations": {},
                 "pending_operation": None, "evidence": [{"result": {
                     "request_id": "op-1", "error": {"code": "remote_state_unknown"}}}]}
        store.create("run-1", state)
        store.append_event("run-1", {"action": "before", "operation_id": "op-1", "tool": "inspect_target",
                                     "request": {"op": "inspect_target"}})
        store.append_event("run-1", {"action": "after", "operation_id": "op-1", "remote_state_unknown": True})
        store.request_cancel("run-1")
        store.save_report("run-1", {"outcome": "interrupted", "evidence": state["evidence"]})
        store.quarantine_target("pi-1", "Remote operation completion is uncertain")
        yield store


def resolve(store, **changes):
    arguments = {"previous_boot_id": OLD, "current_boot_id": NEW, "host_key_fingerprint": FP,
                 "target_digest": "digest", "operator_confirmed": True}
    return store.resolve_passive_cancel_after_reboot("run-1", **(arguments | changes))


def test_resolution_preserves_history_and_survives_startup_without_replay(cancelled):
    store = cancelled
    before = [store.load_state("run-1"), store.load_report("run-1"), store.read_events("run-1")]
    record = resolve(store)
    assert record["original_quarantine"]["error_code"] == "remote_state_unknown"
    assert not store.is_quarantined("pi-1")
    assert before == [store.load_state("run-1"), store.load_report("run-1"), store.read_events("run-1")]
    store.recover_pending("run-1")
    with RunStore(store.root) as fresh:
        fresh.recover_target("pi-1")
        assert not fresh.is_quarantined("pi-1")
        assert fresh.load_state("run-1")["remote_state"] == "unknown"
        assert fresh.load_report("run-1")["outcome"] == "interrupted"


@pytest.mark.parametrize("changes", [{"operator_confirmed": False}, {"operator_confirmed": 1},
    {"current_boot_id": OLD}, {"previous_boot_id": "invalid"}, {"current_boot_id": None},
    {"host_key_fingerprint": "not-verified"}, {"target_digest": "changed-registration"}])
def test_bad_confirmation_binding_or_boot_cannot_release(cancelled, changes):
    with pytest.raises(StorageError):
        resolve(cancelled, **changes)
    assert cancelled.is_quarantined("pi-1")


@pytest.mark.parametrize("change", ["identity", "read_only", "pending", "not_cancelled", "extra_operation", "boolean_count"])
def test_only_the_narrow_passive_board_cancellation_is_eligible(cancelled, change):
    state = cancelled.load_state("run-1")
    if change == "read_only": state["mode"] = "read_only"
    if change == "pending": state["pending_operation"] = {"operation_id": "op-2"}
    if change == "not_cancelled": state["state"] = "connected"
    if change == "extra_operation": state["remote_calls"] = 2
    if change == "boolean_count": state["remote_calls"] = True
    if change == "identity":
        cancelled.append_event("run-1", {"action": "before", "operation_id": "identity-1", "tool": "read_device_identity"})
    cancelled.save_state("run-1", state)
    with pytest.raises(StorageError):
        resolve(cancelled)
    assert cancelled.is_quarantined("pi-1")


def test_other_pending_run_blocks_the_whole_target(cancelled):
    cancelled.create("run-2", {"target_id": "pi-1", "remote_state": "running", "pending_operation": {"x": 1}})
    with pytest.raises(StorageError, match="another unresolved"):
        resolve(cancelled)
    assert cancelled.is_quarantined("pi-1")


def test_unexplained_local_storage_quarantine_cannot_be_released(cancelled):
    cancelled.quarantine_target("pi-1", "Local commit failed; remote state must be reviewed")
    with pytest.raises(StorageError, match="separate review"):
        resolve(cancelled)
    assert cancelled.is_quarantined("pi-1")


def test_failure_to_persist_resolution_leaves_quarantine_intact(cancelled, monkeypatch):
    original = cancelled._atomic_json
    def fail(fd, name, value):
        if name == "passive-reboot-resolution.json":
            raise OSError("simulated storage failure")
        return original(fd, name, value)
    monkeypatch.setattr(cancelled, "_atomic_json", fail)
    with pytest.raises(OSError):
        resolve(cancelled)
    assert cancelled.is_quarantined("pi-1")


@pytest.mark.parametrize("change", ["audit", "evidence", "resolution"])
def test_changed_history_or_resolution_fails_closed(cancelled, change):
    resolve(cancelled)
    if change == "audit":
        cancelled.append_event("run-1", {"action": "before", "operation_id": "op-new"})
    if change == "evidence":
        state = cancelled.load_state("run-1"); state["evidence"].append({})
        cancelled.save_state("run-1", state)
    if change == "resolution":
        path = cancelled.root / "runs/run-1/passive-reboot-resolution.json"
        record = json.loads(path.read_text()); record["current_boot_id"] = OLD
        path.write_text(json.dumps(record))
    with pytest.raises(StorageError):
        cancelled.recover_pending("run-1")


def test_read_only_reader_cannot_apply_even_with_confirmation(cancelled):
    with RunStore.open_reader(cancelled.root) as reader:
        with pytest.raises(StorageError, match="confirmation"):
            resolve(reader)
    assert cancelled.is_quarantined("pi-1")


def test_old_reader_bookkeeping_does_not_invalidate_unchanged_remote_history(cancelled):
    resolve(cancelled)
    cancelled.append_event("run-1", {"action": "recovery", "operation_ids": [], "error_code": "remote_state_unknown"})
    assert cancelled.recover_pending("run-1")["remote_state"] == "unknown"
    with RunStore(cancelled.root) as fresh:
        assert not fresh.is_quarantined("pi-1")


@pytest.mark.parametrize("event", [
    {"action": "recovery", "operation_ids": ["other-io"], "error_code": "remote_state_unknown"},
    {"action": "recovery", "operation_ids": [], "error_code": "policy_denied"},
    {"action": "recovery", "operation_ids": [], "error_code": "remote_state_unknown", "extra": True},
])
def test_compatibility_exception_cannot_hide_any_other_audit_change(cancelled, event):
    resolve(cancelled)
    cancelled.append_event("run-1", event)
    with pytest.raises(StorageError):
        cancelled.recover_pending("run-1")

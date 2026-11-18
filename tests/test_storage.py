import json
import multiprocessing
import os
from pathlib import Path
import stat

import pytest

from embedsleuth.storage import RunStore, StorageError, StoreBusyError, sanitize


@pytest.fixture
def store(tmp_path):
    with RunStore(tmp_path / "private") as instance:
        yield instance


def _contend(root, kind, identifier, output):
    with RunStore(root) as store:
        try:
            with getattr(store, f"{kind}_lock")(identifier):
                output.send("acquired")
        except StoreBusyError:
            output.send("busy")
    output.close()


def test_state_report_and_audit_round_trip_are_snapshots(store):
    state = {"target_id": "pi-1", "nested": {"values": [1, None, True]}}
    store.create("run-1", state)
    state["nested"]["values"].append("changed")
    assert store.load_state("run-1")["nested"]["values"] == [1, None, True]
    store.save_state("run-1", {"target_id": "pi-1", "state": "inspected"})
    store.append_event("run-1", {"action": "before", "operation_id": "op-1"})
    store.append_event("run-1", {"action": "after", "operation_id": "op-1", "result": 104})
    store.save_report("run-1", {"outcome": "partial", "limitations": ["Voltage not measured"]})
    assert store.load_state("run-1")["state"] == "inspected"
    assert store.load_report("run-1")["outcome"] == "partial"
    assert store.read_events("run-1")[1]["result"] == 104
    assert store.list_runs() == ["run-1"]
    for path in store.root.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


@pytest.mark.parametrize("identifier", ["", "..", ".", "a/../b", "/tmp/x", "a\\b", "a\x00b", "a" * 97, True])
def test_all_path_ids_are_strict(store, identifier):
    for operation in (
        lambda: store.create(identifier, {}),
        lambda: store.load_state(identifier),
        lambda: store.save_state(identifier, {}),
        lambda: store.save_report(identifier, {}),
        lambda: store.load_report(identifier),
        lambda: store.append_event(identifier, {}),
        lambda: store.read_events(identifier),
        lambda: store.is_quarantined(identifier),
        lambda: store.quarantine_target(identifier, "unconfirmed"),
    ):
        with pytest.raises(StorageError):
            operation()
    with pytest.raises(StorageError), store.target_lock(identifier):
        pass


def test_sensitive_fields_terminal_controls_and_known_secret_formats_are_removed(store):
    value = {
        "password": "plain-secret",
        "nested": [{"api_key": "api-secret", "key": "key-secret", "observed": "\x1b[31mred\x1b[0m\x00\r\u202ehidden"}],
        "message": "password=inline-secret Bearer abc.def token:token-secret sk-abcdefghijklm123456",
        "pem": "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret-data\n-----END OPENSSH PRIVATE KEY-----",
        "escape": "\x1b]8;;https://malicious.example\x07link\x1b]8;;\x07",
    }
    store.create("run-1", value)
    store.append_event("run-1", value)
    store.save_report("run-1", value)
    for name in ("state.json", "audit.jsonl", "report.json"):
        raw = (store.root / "runs" / "run-1" / name).read_text()
        assert "plain-secret" not in raw
        assert "api-secret" not in raw
        assert "key-secret" not in raw
        assert "inline-secret" not in raw
        assert "token-secret" not in raw
        assert "abc.def" not in raw
        assert "secret-data" not in raw
        assert "malicious.example" not in raw
        assert "\\u001b" not in raw and "\\u0000" not in raw
    assert store.load_state("run-1")["nested"][0]["observed"] == "redhidden"


def test_only_exact_public_sha256_host_key_fingerprint_is_preserved(store):
    fingerprint = "sha256:" + "a0123456789bcdef" * 4
    payload = {"host_key_fingerprint": fingerprint,
               "nested": {"host_key_fingerprint": fingerprint},
               "private_key_fingerprint": fingerprint, "key": fingerprint}
    store.create("run-1", payload)
    store.append_event("run-1", payload)
    store.save_report("run-1", payload)
    for restored in (store.load_state("run-1"), store.read_events("run-1")[0], store.load_report("run-1")):
        assert restored["host_key_fingerprint"] == fingerprint
        assert restored["nested"]["host_key_fingerprint"] == fingerprint
        assert restored["private_key_fingerprint"] == "[REDACTED]"
        assert restored["key"] == "[REDACTED]"


@pytest.mark.parametrize("value", [
    "private-key", "sha256:" + "A" * 64, "sha256:" + "a" * 63,
    "sha256:" + "a" * 65, "sha256:" + "a" * 64 + "\n",
    "sha256:" + "a" * 64 + "\x1b[31m", None, ["sha256:" + "a" * 64],
])
def test_invalid_host_key_fingerprint_never_bypasses_redaction(value):
    assert sanitize({"host_key_fingerprint": value}) == {"host_key_fingerprint": "[REDACTED]"}


@pytest.mark.parametrize("value", [float("inf"), float("nan"), object(), {1: "bad key"}, (1, 2)])
def test_non_json_values_are_rejected_without_creating_run(store, value):
    with pytest.raises(StorageError):
        store.create("run-1", {"value": value})
    assert store.list_runs() == []


def test_collision_and_depth_limit_are_rejected():
    with pytest.raises(StorageError, match="collide"):
        sanitize({"abc": 1, "a\x00bc": 2})
    nested = []
    for _ in range(66):
        nested = [nested]
    with pytest.raises(StorageError, match="nesting"):
        sanitize(nested)


def test_duplicate_create_cannot_overwrite_existing_run(store):
    store.create("run-1", {"target_id": "pi"})
    with pytest.raises(FileExistsError):
        store.create("run-1", {"target_id": "other"})
    assert store.load_state("run-1")["target_id"] == "pi"


@pytest.mark.parametrize("directory", ["root", "runs", "run", "locks", "quarantine"])
def test_existing_insecure_directories_are_rejected(tmp_path, directory):
    root = tmp_path / "private"
    with RunStore(root) as store:
        store.create("run-1", {})
    paths = {"root": root, "runs": root / "runs", "run": root / "runs" / "run-1",
             "locks": root / "locks", "quarantine": root / "quarantine"}
    paths[directory].chmod(0o755)
    with pytest.raises(StorageError):
        RunStore(root)


def test_root_and_ancestor_symlinks_are_denied(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        RunStore(link)
    with pytest.raises(OSError):
        RunStore(link / "private")
    assert not (real / "private").exists()


def test_run_symlink_cannot_escape_store(store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (store.root / "runs" / "run-1").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        store.save_state("run-1", {"value": "no"})
    with pytest.raises(OSError):
        store.list_runs()
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("filename,operation", [
    ("state.json", "state"), ("report.json", "report"), ("audit.jsonl", "audit"),
])
def test_storage_file_symlinks_are_never_followed_or_replaced(store, tmp_path, filename, operation):
    store.create("run-1", {"target_id": "pi"})
    outside = tmp_path / "outside.json"
    outside.write_text("do not touch")
    destination = store.root / "runs" / "run-1" / filename
    destination.unlink(missing_ok=True)
    destination.symlink_to(outside)
    write = {"state": lambda: store.save_state("run-1", {}),
             "report": lambda: store.save_report("run-1", {}),
             "audit": lambda: store.append_event("run-1", {})}[operation]
    read = {"state": lambda: store.load_state("run-1"),
            "report": lambda: store.load_report("run-1"),
            "audit": lambda: store.read_events("run-1")}[operation]
    for call in (write, read):
        with pytest.raises((StorageError, OSError)):
            call()
    assert destination.is_symlink()
    assert outside.read_text() == "do not touch"


def test_hard_linked_and_group_readable_files_are_denied(store, tmp_path):
    store.create("run-1", {"target_id": "pi"})
    state = store.root / "runs" / "run-1" / "state.json"
    os.link(state, tmp_path / "hardlink")
    with pytest.raises(StorageError, match="hard-linked"):
        store.load_state("run-1")
    (tmp_path / "hardlink").unlink()
    state.chmod(0o640)
    with pytest.raises(StorageError, match="owner-only"):
        store.load_state("run-1")


@pytest.mark.parametrize("operation", ["save_state", "save_report"])
def test_atomic_replace_failure_preserves_previous_value_and_removes_temporary(store, monkeypatch, operation):
    store.create("run-1", {"target_id": "pi", "value": "old"})
    store.save_report("run-1", {"value": "old"})
    def fail(*args, **kwargs):
        raise OSError("simulated rename failure")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="simulated"):
        getattr(store, operation)("run-1", {"value": "new"})
    read = store.load_state if operation == "save_state" else store.load_report
    assert read("run-1")["value"] == "old"
    assert not list((store.root / "runs" / "run-1").glob(".pending-*"))


def test_fsync_failure_before_rename_preserves_previous_state(store, monkeypatch):
    store.create("run-1", {"target_id": "pi", "value": "old"})
    real_fsync = os.fsync
    def fail_on_file(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("simulated fsync failure")
        real_fsync(fd)
    monkeypatch.setattr(os, "fsync", fail_on_file)
    with pytest.raises(OSError, match="fsync"):
        store.save_state("run-1", {"value": "new"})
    assert store.load_state("run-1")["value"] == "old"


def test_audit_tail_after_crash_is_ignored_then_removed_before_next_append(store):
    store.create("run-1", {"target_id": "pi"})
    store.append_event("run-1", {"action": "before", "operation_id": "op-1"})
    audit = store.root / "runs" / "run-1" / "audit.jsonl"
    with audit.open("ab") as stream:
        stream.write(b'{"action":"aft')
    assert len(store.read_events("run-1")) == 1
    store.append_event("run-1", {"action": "recovery"})
    assert [event["action"] for event in store.read_events("run-1")] == ["before", "recovery"]
    assert all(isinstance(json.loads(line), dict) for line in audit.read_bytes().splitlines())


@pytest.mark.parametrize("content", [b'{"bad":\n', b'{"x":1,"x":2}\n', b'[]\n', b'{"x":NaN}\n'])
def test_malformed_complete_audit_record_is_not_silently_discarded(store, content):
    store.create("run-1", {"target_id": "pi"})
    audit = store.root / "runs" / "run-1" / "audit.jsonl"
    audit.write_bytes(content)
    audit.chmod(0o600)
    with pytest.raises(StorageError):
        store.read_events("run-1")
    with pytest.raises(StorageError):
        store.append_event("run-1", {})


def test_restart_quarantines_unfinished_operation_and_never_replays(store):
    store.create("run-1", {"target_id": "pi-1", "state": "probe_ready"})
    store.append_event("run-1", {"action": "before", "operation_id": "op-1"})
    store.close()
    with RunStore(store.root) as restored:
        state = restored.load_state("run-1")
        assert state["state"] == "blocked"
        assert state["remote_state"] == "unknown"
        assert state["error_code"] == "remote_state_unknown"
        assert state["interrupted_operations"] == ["op-1"]
        assert restored.is_quarantined("pi-1")
        restored.create("run-2", {"target_id": "pi-1"})
        assert restored.is_quarantined("pi-1")
        assert not any(e["action"] == "after" for e in restored.read_events("run-1"))
    with RunStore(store.root) as restarted_again:
        assert restarted_again.is_quarantined("pi-1")


def test_completed_operation_is_not_quarantined_and_missing_report_is_explicit(store):
    store.create("run-1", {"target_id": "pi", "state": "identified"})
    store.append_event("run-1", {"action": "before", "execution_id": "exec-1"})
    store.append_event("run-1", {"action": "after", "execution_id": "exec-1"})
    assert store.recover_pending("run-1")["state"] == "identified"
    assert not store.is_quarantined("pi")
    with pytest.raises(FileNotFoundError):
        store.load_report("run-1")


@pytest.mark.parametrize("remote_state", ["running", "unknown"])
def test_non_idle_state_with_matched_journal_is_quarantined_instead_of_replayed(store, remote_state):
    store.create("run-1", {"target_id": "pi", "remote_state": remote_state})
    store.append_event("run-1", {"action": "before", "operation_id": "op-1"})
    store.append_event("run-1", {"action": "after", "operation_id": "op-1"})
    assert store.recover_pending("run-1")["remote_state"] == "unknown"
    assert store.is_quarantined("pi")
    event_count = len(store.read_events("run-1"))
    store.recover_pending("run-1")
    assert len(store.read_events("run-1")) == event_count


def test_unknown_terminal_journal_is_never_interpreted_as_idle(store):
    store.create("run-1", {"target_id": "pi", "remote_state": "idle"})
    store.append_event("run-1", {"action": "before", "operation_id": "op-1"})
    store.append_event("run-1", {"action": "after", "operation_id": "op-1", "remote_state_unknown": True})
    assert store.recover_pending("run-1")["state"] == "blocked"
    assert store.is_quarantined("pi")


def test_truncated_after_is_treated_as_unconfirmed_completion(store):
    store.create("run-1", {"target_id": "pi"})
    store.append_event("run-1", {"action": "before", "operation_id": "op-1"})
    with (store.root / "runs" / "run-1" / "audit.jsonl").open("ab") as stream:
        stream.write(b'{"action":"after","operation_id":"op-1"}')
    assert store.recover_pending("run-1")["error_code"] == "remote_state_unknown"
    assert store.is_quarantined("pi")


def test_corrupt_state_prevents_restart_instead_of_resetting_to_idle(store):
    store.create("run-1", {"target_id": "pi"})
    (store.root / "runs" / "run-1" / "state.json").write_text('{"state":')
    with pytest.raises(StorageError, match="malformed"):
        RunStore(store.root)


def test_quarantine_symlink_is_not_interpreted_as_unquarantined(store, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text('{}')
    (store.root / "quarantine" / "pi.json").symlink_to(outside)
    with pytest.raises(OSError):
        store.is_quarantined("pi")


@pytest.mark.parametrize("kind", ["run", "target"])
def test_cross_process_nonblocking_lock_contention(store, kind):
    store.create("run-1", {"target_id": "pi"})
    identifier = "run-1" if kind == "run" else "pi"
    # Spawn ensures the child cannot inherit the parent's flock descriptors.
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    with getattr(store, f"{kind}_lock")(identifier):
        process = context.Process(target=_contend, args=(str(store.root), kind, identifier, send))
        process.start()
        send.close()
        try:
            assert receive.poll(10), "child failed to return a bounded lock result"
            assert receive.recv() == "busy"
        finally:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            receive.close()
        assert process.exitcode == 0
    with getattr(store, f"{kind}_lock")(identifier):
        pass


def test_same_instance_same_thread_locks_are_reentrant_but_other_store_rejected(store):
    store.create("run-1", {"target_id": "pi"})
    with RunStore(store.root) as other:
        with store.run_lock("run-1"):
            store.save_state("run-1", {"target_id": "pi", "saved": True})
            with pytest.raises(StoreBusyError), other.run_lock("run-1"):
                pass
        assert store.load_state("run-1")["saved"] is True


def test_startup_does_not_quarantine_a_live_locked_operation(store):
    store.create("run-1", {"target_id": "pi", "state": "probe_ready"})
    with store.target_lock("pi"), store.run_lock("run-1"):
        store.append_event("run-1", {"action": "before", "operation_id": "op-1"})
        with RunStore(store.root) as other:
            assert not other.is_quarantined("pi")
            with pytest.raises(StoreBusyError), other.target_lock("pi"):
                pass
        store.append_event("run-1", {"action": "after", "operation_id": "op-1"})


def test_closed_store_is_unusable(store):
    store.close()
    with pytest.raises(StorageError, match="closed"):
        store.list_runs()


def test_cancel_marker_is_durable_while_remote_worker_owns_locks(store):
    store.create("run-1", {"target_id": "pi"})
    assert not store.cancel_requested("run-1")
    with store.target_lock("pi"), store.run_lock("run-1"), RunStore(store.root) as other:
        other.request_cancel("run-1")
        assert store.cancel_requested("run-1")
    with RunStore(store.root) as restarted:
        assert restarted.cancel_requested("run-1")
    path = store.root / "runs" / "run-1" / "cancel.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("operation", ["request_cancel", "cancel_requested"])
def test_cancel_requires_registered_run_and_strict_id(store, operation):
    with pytest.raises(FileNotFoundError):
        getattr(store, operation)("not-created")
    with pytest.raises(StorageError):
        getattr(store, operation)("../escape")


def test_cancel_symlink_cannot_read_or_write_outside_run(store, tmp_path):
    store.create("run-1", {"target_id": "pi"})
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    marker = store.root / "runs" / "run-1" / "cancel.json"
    marker.symlink_to(outside)
    with pytest.raises(OSError):
        store.cancel_requested("run-1")
    with pytest.raises(StorageError):
        store.request_cancel("run-1")
    assert outside.read_text() == "unchanged"


def test_malformed_cancel_marker_fails_closed(store):
    store.create("run-1", {"target_id": "pi"})
    store.request_cancel("run-1")
    marker = store.root / "runs" / "run-1" / "cancel.json"
    marker.write_text('{"run_id":"other-run","cancelled":true}')
    with pytest.raises(StorageError, match="cancellation"):
        store.cancel_requested("run-1")


def _completed_pending_state(*, identity=True, with_result=True, with_cache=True):
    result = {"request_id": "op-1", "execution_id": "exec-1", "status": "ok"}
    arguments = {"bus_ref": "bus-1", "address": 104}
    return {
        "target_id": "pi", "remote_state": "idle", "state": "identified",
        "pending_operation": {"operation_id": "op-1", "tool": "read_device_identity" if identity else "inspect_target",
                              "arguments": arguments},
        "evidence": [{"evidence_id": "ev-1", "result": result}] if with_result else [],
        "operations": {"op-1": {"arguments": arguments, "result": result}} if with_cache else {},
    }


@pytest.mark.parametrize("identity", [True, False])
def test_completed_pending_marker_clears_only_after_terminal_result_is_durable(store, identity):
    state = _completed_pending_state(identity=identity)
    store.create("run-1", state)
    store.append_event("run-1", {"action": "before", "operation_id": "op-1"})
    store.append_event("run-1", {"action": "after", "operation_id": "op-1"})
    restored = store.recover_pending("run-1")
    assert restored["pending_operation"] is None
    assert restored["state"] == "identified"
    assert restored["operations"] == state["operations"]
    assert not store.is_quarantined("pi")


@pytest.mark.parametrize("with_result,with_cache", [(False, False), (True, False), (False, True)])
def test_completed_audit_without_result_or_identity_cache_does_not_authorize_replay(store, with_result, with_cache):
    store.create("run-1", _completed_pending_state(with_result=with_result, with_cache=with_cache))
    store.append_event("run-1", {"action": "before", "operation_id": "op-1"})
    store.append_event("run-1", {"action": "after", "operation_id": "op-1"})
    assert store.recover_pending("run-1")["remote_state"] == "unknown"
    assert store.is_quarantined("pi")


def test_interrupted_marker_is_retired_without_overwriting_durable_cached_result(store):
    original = _completed_pending_state()
    store.create("run-1", original)
    store.append_event("run-1", {"action": "before", "operation_id": "op-1"})
    restored = store.recover_pending("run-1")
    assert restored["pending_operation"] is None
    assert restored["interrupted_operations"] == ["op-1"]
    assert restored["operations"] == original["operations"]


def test_target_recovery_detects_crash_after_second_store_skipped_live_run(store):
    store.create("old-run", {"target_id": "pi", "remote_state": "idle"})
    with store.target_lock("pi"), store.run_lock("old-run"):
        store.append_event("old-run", {"action": "before", "operation_id": "op-1"})
        store.save_state("old-run", {"target_id": "pi", "remote_state": "running"})
        other = RunStore(store.root)
        assert not other.is_quarantined("pi")
    # The first process disappeared after the second store's initialization.
    # No constructor will run again before the next target dispatch.
    try:
        other.create("new-run", {"target_id": "pi", "remote_state": "idle"})
        other.recover_target("pi")
        assert other.is_quarantined("pi")
        assert other.load_state("old-run")["remote_state"] == "unknown"
    finally:
        other.close()


def test_target_recovery_busy_run_is_not_skipped(store):
    store.create("run-1", {"target_id": "pi", "remote_state": "idle"})
    with RunStore(store.root) as other, store.run_lock("run-1"):
        with pytest.raises(StoreBusyError):
            other.recover_target("pi")


def test_target_recovery_owns_target_lock_and_validates_all_states(store):
    store.create("run-1", {"target_id": "pi", "remote_state": "idle"})
    with RunStore(store.root) as other, store.target_lock("pi"):
        with pytest.raises(StoreBusyError):
            other.recover_target("pi")
    (store.root / "runs" / "run-1" / "state.json").write_text('{"target_id":')
    with pytest.raises(StorageError, match="malformed"):
        store.recover_target("pi")


def test_target_recovery_does_not_quarantine_unrelated_target(store):
    store.create("run-1", {"target_id": "pi-1", "remote_state": "idle"})
    store.create("run-2", {"target_id": "pi-2", "remote_state": "idle"})
    store.append_event("run-2", {"action": "before", "operation_id": "op-1"})
    store.recover_target("pi-1")
    assert not store.is_quarantined("pi-1")
    assert not store.is_quarantined("pi-2")
    store.recover_target("pi-2")
    assert store.is_quarantined("pi-2")

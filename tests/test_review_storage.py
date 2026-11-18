"""Independent review regressions for cross-run interruption recovery."""

from pathlib import Path

import pytest

from embedsleuth.core import DiagnosticCore
from embedsleuth.storage import RunStore
from embedsleuth.targets import RegisteredTarget, TargetRegistry
from embedsleuth.transport import ProbeResponse


class ReviewPeer:
    def __init__(self):
        self.reads = 0
        self.interrupt_next_read = False

    def execute(self, target, request, **limits):
        observations = {
            "boot_id": "boot-review", "host_key_fingerprint": "sha256:" + "a" * 64,
            "board_model": "Raspberry Pi 3 Model B Rev 1.2", "bus": 1,
            "adapter_present": True, "access_allowed": True,
            "driver_bound": {"104": False}, "combined_transfer_supported": True,
            "dependency_available": True, "adapter_fingerprint": "adapter-review",
        }
        if request["op"] == "read_device_identity":
            self.reads += 1
            if self.interrupt_next_read:
                self.interrupt_next_read = False
                raise KeyboardInterrupt("simulated host interruption after dispatch")
            observations.update(address=104, register=117, identity_value=104)
        return ProbeResponse(observations=observations)


def registry_at(directory: Path):
    trust = directory / "known_hosts"
    trust.write_text("review fixture")
    target = RegisteredTarget(
        target_id="review-pi", host="pi.local", username="pi", known_hosts_path=trust,
        expected_addresses=(104,), conflicting_use_excluded=True,
    )
    return TargetRegistry((target,))


def start_and_inspect(core):
    run_id = core.start_diagnosis("review-pi")["run_id"]
    # A quarantined target may refuse inspections or only refuse device reads.
    core.inspect_target(run_id)
    core.inspect_i2c_environment(run_id)
    return run_id


def test_surviving_store_recovers_other_run_after_original_owner_dies(tmp_path):
    registry, peer = registry_at(tmp_path), ReviewPeer()
    with RunStore(tmp_path / "store") as original:
        first_core = DiagnosticCore(registry, peer, original)
        old_run = first_core.start_diagnosis("review-pi")["run_id"]
        with original.target_lock("review-pi"), original.run_lock(old_run):
            original.append_event(old_run, {
                "action": "before", "operation_id": "interrupted-read", "tool": "read_device_identity",
            })
            state = original.load_state(old_run)
            state["remote_state"] = "running"
            original.save_state(old_run, state)
            # A second live server skips the locked run during startup recovery.
            surviving = RunStore(original.root)
        # Releasing the first owner's locks models OS cleanup after its crash.
        try:
            second_core = DiagnosticCore(registry, peer, surviving)
            new_run = start_and_inspect(second_core)
            denied = second_core.read_device_identity(new_run, "bus-1", 104, "new-read")
            assert denied.status == "blocked"
            assert peer.reads == 0
            assert surviving.is_quarantined("review-pi")
        finally:
            surviving.close()


def test_caught_host_interrupt_cannot_allow_new_run_to_repeat_uncertain_io(tmp_path):
    registry, peer = registry_at(tmp_path), ReviewPeer()
    with RunStore(tmp_path / "store") as store:
        core = DiagnosticCore(registry, peer, store)
        run_id = start_and_inspect(core)
        peer.interrupt_next_read = True
        with pytest.raises(KeyboardInterrupt):
            core.read_device_identity(run_id, "bus-1", 104, "interrupted-read")
        new_run = start_and_inspect(core)
        denied = core.read_device_identity(new_run, "bus-1", 104, "new-read")
        assert denied.status == "blocked"
        assert peer.reads == 1
        assert store.is_quarantined("review-pi")


@pytest.mark.parametrize("alias_host", ["pi.local", "PI.LOCAL."])
def test_alias_registration_cannot_split_one_endpoint_into_independent_locks(tmp_path, alias_host):
    registry = registry_at(tmp_path)
    original = registry.get("review-pi")
    fields = original.model_dump()
    fields.update(target_id="second-name", host=alias_host, username="different_user", expected_bus=2)
    alias = RegisteredTarget(**fields)
    with pytest.raises(ValueError):
        TargetRegistry((original, alias))


def test_distinct_ssh_endpoints_remain_independent_registered_targets(tmp_path):
    registry = registry_at(tmp_path)
    original = registry.get("review-pi")
    fields = original.model_dump()
    fields.update(target_id="second-device", port=2222)
    separate_endpoint = RegisteredTarget(**fields)
    combined = TargetRegistry((original, separate_endpoint))
    assert combined.get("review-pi") is original
    assert combined.get("second-device") is separate_endpoint

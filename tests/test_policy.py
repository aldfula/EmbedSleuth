import pytest
from pydantic import ValidationError

from conftest import changed
from embedsleuth.policy import IdentityReadContext, authorize_identity_read


@pytest.mark.parametrize("address", [0x68, 0x69])
def test_only_complete_read_only_context_permits_identity(profile, ready_context, address):
    decision = authorize_identity_read(profile, ready_context, address)
    assert decision.allowed


@pytest.mark.parametrize("updates,reason", [
    ({"mode": "passive"}, "passive_mode"),
    ({"profile_id": "other"}, "profile_mismatch"),
    ({"target_registered": False}, "target_unregistered"),
    ({"expected_addresses": []}, "address_not_allowed"),
    ({"remote_state": "unknown"}, "remote_state_unknown"),
    ({"remote_state": "running"}, "remote_state_unknown"),
    ({"budget_available": False}, "budget_exhausted"),
    ({"driver_bound": True}, "device_in_use"),
    ({"conflicting_use": True}, "device_in_use"),
    ({"state": "created"}, "preconditions_missing"),
    ({"state": "expired"}, "preconditions_missing"),
    ({"state": "cancelled"}, "preconditions_missing"),
    ({"state": "reported"}, "preconditions_missing"),
    ({"expected_connection": False}, "preconditions_missing"),
    ({"bus_validated": False}, "preconditions_missing"),
    ({"preconditions_fresh": False}, "preconditions_missing"),
    ({"access_allowed": None}, "preconditions_missing"),
    ({"access_allowed": False}, "preconditions_missing"),
    ({"driver_bound": None}, "preconditions_missing"),
    ({"conflicting_use": None}, "preconditions_missing"),
    ({"combined_transfer_supported": None}, "preconditions_missing"),
    ({"combined_transfer_supported": False}, "preconditions_missing"),
    ({"dependency_available": False}, "preconditions_missing"),
])
def test_one_missing_or_unsafe_precondition_blocks_read(profile, ready_context, updates, reason):
    decision = authorize_identity_read(profile, changed(ready_context, **updates), 0x68)
    assert not decision.allowed
    assert decision.reason == reason


@pytest.mark.parametrize("address", [0x67, 0x70, -1, 0x80, True, "104"])
def test_policy_rejects_address_outside_expected_profile(profile, ready_context, address):
    assert not authorize_identity_read(profile, ready_context, address).allowed


def test_declared_address_is_narrower_than_component_candidates(profile, ready_context):
    context = changed(ready_context, expected_addresses=[0x68])
    assert not authorize_identity_read(profile, context, 0x69).allowed


def test_draft_profile_cannot_enable_hardware_access(profile, ready_context):
    draft = changed(profile, review_status="draft", reviewed_on=None)
    assert authorize_identity_read(draft, ready_context, 0x68).reason == "unreviewed_profile"


def test_empty_context_denies_by_default(profile):
    context = IdentityReadContext(profile_id="mpu6050")
    assert not authorize_identity_read(profile, context, 0x68).allowed


@pytest.mark.parametrize("unsafe_value", [1, "true", "false"])
def test_context_does_not_coerce_permissions(unsafe_value):
    with pytest.raises(ValidationError):
        IdentityReadContext(profile_id="mpu6050", access_allowed=unsafe_value)

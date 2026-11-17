import json

import pytest
from pydantic import ValidationError

from embedsleuth.profiles import ComponentProfile, evaluate_identity, load_component_profile


@pytest.mark.parametrize("address", [0x68, 0x69])
def test_identity_is_0x68_at_both_addresses(profile, address):
    assert evaluate_identity(profile, address, 0x68) == "pass"
    assert evaluate_identity(profile, address, 0x69) == "fail"


@pytest.mark.parametrize("value", [0x00, 0xFF, 0xE8])
def test_mismatch_does_not_ignore_reserved_bits(profile, value):
    assert evaluate_identity(profile, 0x68, value) == "fail"


@pytest.mark.parametrize("address,value", [(0x67, 0x68), (True, 0x68), (0x68, True), (0x68, -1), (0x68, 256), (0x68, "104")])
def test_identity_rejects_invalid_observations(profile, address, value):
    with pytest.raises(ValueError):
        evaluate_identity(profile, address, value)


@pytest.mark.parametrize("profile_id", ["../mpu6050", "/tmp/mpu6050.json", "MPU6050", "unknown"])
def test_profile_loader_never_accepts_paths(profile_id):
    with pytest.raises(ValueError, match="unknown bundled"):
        load_component_profile(profile_id)


@pytest.mark.parametrize("change", [
    {"addresses": [104, 104]},
    {"addresses": [True]},
    {"addresses": ["104"]},
    {"addresses": [0]},
    {"reviewed_on": None},
    {"reviewed_on": "2000-01-01"},
    {"arbitrary_shell": "sudo reboot"},
])
def test_profile_rejects_invalid_or_unreviewable_facts(profile, change):
    data = profile.model_dump(mode="json")
    data.update(change)
    with pytest.raises(ValidationError):
        ComponentProfile.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("change", [
    {"mask": 0}, {"mask": 1}, {"length": 2}, {"length": True},
    {"register_address": True}, {"access": "read_write"},
    {"citation": {"source_id": "missing", "section": "4.32", "page": 45}},
])
def test_identity_rule_cannot_expand_access(profile, change):
    data = profile.model_dump(mode="json")
    data["identity"].update(change)
    with pytest.raises(ValidationError):
        ComponentProfile.model_validate_json(json.dumps(data))


def test_profile_is_ic_only_and_has_source_provenance(profile):
    assert profile.scope == "ic"
    assert profile.review_status == "reviewed"
    assert profile.supply.minimum_v == 2.375
    assert profile.supply.maximum_v == 3.46
    assert profile.identity.register_address == 0x75
    assert all(source.sha256 and source.retrieved_on for source in profile.sources)
    assert profile.limitations


def test_source_verification_uses_content_hash(profile):
    source = profile.sources[0]
    assert not source.matches(b"changed document")


def test_supply_range_cannot_be_reversed(profile):
    data = profile.model_dump(mode="json")
    data["supply"].update(minimum_v=5.0, maximum_v=3.3)
    with pytest.raises(ValidationError, match="reversed"):
        ComponentProfile.model_validate_json(json.dumps(data))

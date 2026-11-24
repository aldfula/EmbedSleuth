"""Trusted startup configuration is independent of model tool arguments."""

import json
import os
from pathlib import Path

import pytest

from embedsleuth.config import ConfigurationError, load_configuration


def write_config(tmp_path, **updates):
    data = {"schema_version": "1.0", "state_directory": str(tmp_path / "state"), "targets": []}
    data.update(updates)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


def target(tmp_path, **updates):
    return {"target_id": "lab-pi", "host": "pi.local", "username": "pi",
            "known_hosts_path": str(tmp_path / "known_hosts"), **updates}


def test_configuration_loads_registered_targets_and_defaults_fail_closed(tmp_path):
    config = load_configuration(write_config(tmp_path, targets=[target(tmp_path)]))
    assert config.state_directory == tmp_path / "state"
    assert config.targets[0].conflicting_use_excluded is False
    assert config.targets[0].expected_addresses == (104, 105)


@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_configuration_allows_owner_controlled_reference_files(tmp_path, mode):
    path = write_config(tmp_path)
    path.chmod(mode)
    assert load_configuration(path).targets == ()


@pytest.mark.parametrize("updates", [
    {"state_directory": "relative"}, {"schema_version": "2.0"},
    {"password": "secret-should-not-be-reflected"}, {"targets": None},
    {"state_directory": 1},
])
def test_configuration_rejects_invalid_shape_without_contents_in_error(tmp_path, updates):
    with pytest.raises(ConfigurationError) as caught:
        load_configuration(write_config(tmp_path, **updates))
    assert str(caught.value) == "Invalid or unsafe local MCP configuration"


@pytest.mark.parametrize("updates", [
    {"known_hosts_path": "relative"}, {"key_path": "~/.ssh/id"},
    {"expected_bus": True}, {"conflicting_use_excluded": "true"},
    {"expected_addresses": ["104"]}, {"command": "arbitrary"},
    {"password": "secret"},
])
def test_configuration_rejects_unsafe_target_fields_and_coercion(tmp_path, updates):
    with pytest.raises(ConfigurationError):
        load_configuration(write_config(tmp_path, targets=[target(tmp_path, **updates)]))


@pytest.mark.parametrize("duplicate", ["id", "endpoint"])
def test_configuration_rejects_duplicate_ids_and_normalized_endpoints(tmp_path, duplicate):
    second = target(tmp_path, host="other.local") if duplicate == "id" else target(tmp_path, target_id="other", host="PI.LOCAL.")
    with pytest.raises(ConfigurationError):
        load_configuration(write_config(tmp_path, targets=[target(tmp_path), second]))


@pytest.mark.parametrize("kind", ["missing", "relative", "symlink", "hardlink", "writable", "directory", "fifo", "oversize", "duplicate", "malformed"])
def test_configuration_rejects_unsafe_files(tmp_path, kind):
    path = write_config(tmp_path)
    if kind == "missing":
        path.unlink()
    elif kind == "relative":
        path = Path("relative.json")
    elif kind in {"symlink", "hardlink"}:
        other = tmp_path / "other.json"
        other.symlink_to(path) if kind == "symlink" else os.link(path, other)
        path = other
    elif kind == "writable":
        path.chmod(0o622)
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif kind == "oversize":
        path.write_bytes(b" " * 65537)
    elif kind == "duplicate":
        path.write_text('{"targets":[],"targets":[]}')
    elif kind == "malformed":
        path.write_bytes(b"\xffsecret")
    with pytest.raises(ConfigurationError):
        load_configuration(path)

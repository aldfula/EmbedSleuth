from pathlib import Path

import pytest
from pydantic import ValidationError

from embedsleuth.targets import RegisteredTarget, TargetRegistry


def target(**overrides):
    return RegisteredTarget(**({"target_id": "lab-pi", "host": "192.0.2.1", "username": "pi",
                                "known_hosts_path": Path("/tmp/known_hosts")} | overrides))


@pytest.mark.parametrize("overrides", [
    {"host": "pi; reboot"}, {"host": "-oProxyCommand=x"}, {"username": "pi user"},
    {"port": True}, {"port": 65536}, {"known_hosts_path": Path("relative")},
    {"key_path": Path("relative")}, {"expected_bus": True}, {"expected_bus": -1},
    {"expected_addresses": (True,)}, {"expected_addresses": (104, 104)},
    {"approved": True},
])
def test_target_strict_registration(overrides):
    with pytest.raises(ValidationError):
        target(**overrides)


def test_registry_requires_exact_local_object():
    registered = target()
    registry = TargetRegistry((registered,))
    assert registry.get("lab-pi") is registered
    assert registry.contains_exact(registered)
    assert not registry.contains_exact(registered.model_copy())
    with pytest.raises(ValueError, match="not registered"):
        registry.get("unknown")
    with pytest.raises(ValueError, match="duplicate"):
        TargetRegistry((registered, registered))

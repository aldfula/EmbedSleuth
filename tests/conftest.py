import json

import pytest

from embedsleuth.policy import IdentityReadContext
from embedsleuth.profiles import load_component_profile


@pytest.fixture
def profile():
    return load_component_profile("mpu6050")


@pytest.fixture
def ready_context():
    return IdentityReadContext(
        profile_id="mpu6050", mode="read_only", state="probe_ready",
        target_registered=True, expected_connection=True, expected_addresses=(0x68, 0x69),
        bus_validated=True, preconditions_fresh=True, access_allowed=True,
        driver_bound=False, conflicting_use=False, combined_transfer_supported=True,
        dependency_available=True, budget_available=True, remote_state="idle",
    )


def changed(model, **updates):
    """Revalidate changes rather than bypass validators with model_copy(update=...)."""
    data = model.model_dump(mode="json")
    data.update(updates)
    return type(model).model_validate_json(json.dumps(data))

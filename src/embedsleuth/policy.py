"""Pure, fail-closed precondition policy. No IO or approval bypasses."""

from typing import Literal

from embedsleuth.contracts import Contract, I2CAddress, Identifier, Mode, RunState
from embedsleuth.profiles import ComponentProfile

POLICY_VERSION = "0.1.0"


class IdentityReadContext(Contract):
    """Trusted core observations, never fields accepted from model tool arguments.

    Phase 2 must derive freshness, budget and connection state itself. Supplying
    this object is not authorization to perform IO; Phase 1 only evaluates it.
    """

    profile_id: Identifier
    mode: Mode = "read_only"
    state: RunState = "created"
    target_registered: bool = False
    expected_connection: bool = False
    expected_addresses: tuple[I2CAddress, ...] = ()
    bus_validated: bool = False
    preconditions_fresh: bool = False
    access_allowed: bool | None = None
    driver_bound: bool | None = None
    conflicting_use: bool | None = None
    combined_transfer_supported: bool | None = None
    dependency_available: bool = False
    budget_available: bool = False
    remote_state: Literal["idle", "running", "unknown"] = "unknown"


class PolicyDecision(Contract):
    allowed: bool
    reason: Literal[
        "identity_read_allowed", "passive_mode", "profile_mismatch",
        "unreviewed_profile", "address_not_allowed", "target_unregistered",
        "preconditions_missing", "device_in_use", "remote_state_unknown",
        "budget_exhausted",
    ]


def authorize_identity_read(
    profile: ComponentProfile, context: IdentityReadContext, address: int,
) -> PolicyDecision:
    """Unknown or incomplete preconditions deny a read, regardless of hook state."""
    def deny(reason: str) -> PolicyDecision:
        return PolicyDecision(allowed=False, reason=reason)

    if context.mode == "passive":
        return deny("passive_mode")
    if profile.profile_id != context.profile_id:
        return deny("profile_mismatch")
    if profile.review_status != "reviewed":
        return deny("unreviewed_profile")
    if not context.target_registered:
        return deny("target_unregistered")
    if type(address) is not int or address not in profile.addresses or address not in context.expected_addresses:
        return deny("address_not_allowed")
    if context.remote_state != "idle":
        return deny("remote_state_unknown")
    if not context.budget_available:
        return deny("budget_exhausted")
    if context.driver_bound is True or context.conflicting_use is True:
        return deny("device_in_use")
    if (
        context.state != "probe_ready" or not context.expected_connection
        or not context.bus_validated or not context.preconditions_fresh
        or context.access_allowed is not True or context.driver_bound is not False
        or context.conflicting_use is not False or context.combined_transfer_supported is not True
        or not context.dependency_available
    ):
        return deny("preconditions_missing")
    return PolicyDecision(allowed=True, reason="identity_read_allowed")

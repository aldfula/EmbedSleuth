"""Locally registered SSH targets; never populated by model tool arguments."""

from pathlib import Path
from ipaddress import ip_address
from typing import Annotated

from pydantic import Field, field_validator

from embedsleuth.contracts import Contract, I2CAddress, Identifier


class RegisteredTarget(Contract):
    target_id: Identifier
    host: Annotated[str, Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9:.][A-Za-z0-9:.%-]*$")]
    username: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*[$]?$")]
    port: Annotated[int, Field(ge=1, le=65535)] = 22
    known_hosts_path: Path
    key_path: Path | None = None
    profile_id: Identifier = "mpu6050"
    expected_bus: Annotated[int, Field(ge=0, le=255)] = 1
    expected_addresses: Annotated[tuple[I2CAddress, ...], Field(min_length=1, max_length=2)] = (104, 105)
    # Explicit local operator declaration, not proof that other processes cannot run.
    conflicting_use_excluded: bool = False

    @field_validator("known_hosts_path", "key_path")
    @classmethod
    def absolute_path(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("SSH trust and identity paths must be absolute")
        return value

    @field_validator("expected_addresses")
    @classmethod
    def unique_addresses(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate expected addresses")
        return value


class TargetRegistry:
    """An explicit immutable snapshot supplied by the local application owner."""

    def __init__(self, targets: tuple[RegisteredTarget, ...] = ()) -> None:
        self._targets = {target.target_id: target for target in targets}
        if len(self._targets) != len(targets):
            raise ValueError("duplicate target ID")
        endpoints = set()
        for target in targets:
            host = target.host.casefold().rstrip(".")
            try:
                host = str(ip_address(host))
            except ValueError:
                pass
            endpoint = (host, target.port)
            if endpoint in endpoints:
                raise ValueError("duplicate SSH endpoint; use one stable target ID per device")
            endpoints.add(endpoint)

    def get(self, target_id: str) -> RegisteredTarget:
        try:
            return self._targets[target_id]
        except KeyError:
            raise ValueError("target is not registered") from None

    def contains_exact(self, target: RegisteredTarget) -> bool:
        return self._targets.get(target.target_id) is target

"""Bundled, reviewed component facts. Loading never downloads documents."""

from __future__ import annotations

import hashlib
from datetime import date
from importlib.resources import files
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from embedsleuth.contracts import Assessment, Byte, Contract, I2CAddress, Identifier, Version


class SourceDocument(Contract):
    source_id: Identifier
    document_number: str
    revision: str
    publisher: str
    original_url: Annotated[str, Field(pattern=r"^https://")]
    retrieved_url: Annotated[str, Field(pattern=r"^https://")]
    retrieved_on: date
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    def matches(self, content: bytes) -> bool:
        return hashlib.sha256(content).hexdigest() == self.sha256


class Citation(Contract):
    source_id: Identifier
    section: Annotated[str, Field(min_length=1)]
    page: Annotated[int, Field(ge=1)]


class IdentityRule(Contract):
    register_address: Byte
    length: Annotated[int, Field(ge=1, le=1)] = 1
    expected: Byte
    mask: Byte = 255
    access: Literal["read_only"] = "read_only"
    read_side_effect: Literal["register_pointer_only"] = "register_pointer_only"
    citation: Citation

    @model_validator(mode="after")
    def meaningful_mask(self) -> Self:
        if self.mask == 0 or self.expected & ~self.mask:
            raise ValueError("identity mask must constrain all expected bits")
        return self


class SupplyRange(Contract):
    minimum_v: Annotated[float, Field(gt=0)]
    maximum_v: Annotated[float, Field(gt=0)]
    citation: Citation

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.minimum_v > self.maximum_v:
            raise ValueError("supply range is reversed")
        return self


class SamplingRule(Contract):
    review_status: Literal["draft", "reviewed"] = "draft"
    reviewed_on: date | None = None
    data_register: Literal[59] = 59
    length: Literal[14] = 14
    count: Literal[5] = 5
    power_register: Literal[107] = 107
    wake_clear_mask: Literal[64] = 64
    citations: Annotated[tuple[Citation, ...], Field(min_length=1)]


class ComponentProfile(Contract):
    schema_version: Literal["1.0"] = "1.0"
    profile_id: Identifier
    version: Version
    manufacturer: str
    component: str
    scope: Literal["ic"] = "ic"
    review_status: Literal["draft", "reviewed"]
    reviewed_on: date | None = None
    review_notes: Annotated[str, Field(min_length=1)]
    sources: Annotated[tuple[SourceDocument, ...], Field(min_length=1)]
    addresses: Annotated[tuple[I2CAddress, ...], Field(min_length=1)]
    address_citation: Citation
    identity: IdentityRule
    supply: SupplyRange
    max_i2c_hz: Annotated[int, Field(gt=0)]
    bus_citation: Citation
    sampling: SamplingRule | None = None
    limitations: Annotated[tuple[str, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        source_ids = {source.source_id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("duplicate source ID")
        if len(set(self.addresses)) != len(self.addresses):
            raise ValueError("duplicate I2C address")
        for citation in (
            self.address_citation, self.identity.citation, self.supply.citation, self.bus_citation,
            *(self.sampling.citations if self.sampling else ()),
        ):
            if citation.source_id not in source_ids:
                raise ValueError("citation refers to an unknown document")
        if self.sampling and self.sampling.review_status == "reviewed" and (
            self.sampling.reviewed_on is None or (self.review_status == "reviewed" and (
                self.reviewed_on is None or self.sampling.reviewed_on > self.reviewed_on))
        ):
            raise ValueError("sampling review requires a date within the profile review")
        if self.review_status == "reviewed" and self.reviewed_on is None:
            raise ValueError("reviewed profile requires a review date")
        if self.reviewed_on is not None and any(
            source.retrieved_on > self.reviewed_on for source in self.sources
        ):
            raise ValueError("review cannot predate source retrieval")
        return self


def load_component_profile(profile_id: str) -> ComponentProfile:
    """Only explicit bundled IDs are accepted; user input is never a file path."""
    if profile_id != "mpu6050":
        raise ValueError("unknown bundled component profile")
    content = files("embedsleuth").joinpath("data", "profiles", "mpu6050.json").read_text(encoding="utf-8")
    profile = ComponentProfile.model_validate_json(content)
    if profile.profile_id != profile_id:
        raise ValueError("bundled profile ID mismatch")
    return profile


def evaluate_identity(profile: ComponentProfile, address: int, value: int) -> Assessment:
    """Classify an observed byte; a match does not authenticate a physical IC."""
    if type(address) is not int or address not in profile.addresses:
        raise ValueError("address is outside the component profile")
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError("identity observation must be one byte")
    return "pass" if value & profile.identity.mask == profile.identity.expected else "fail"

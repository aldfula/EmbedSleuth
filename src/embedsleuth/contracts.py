"""Versioned, strict contracts shared by core, MCP adapters and reports.

These are data contracts only. No network, shell, hardware or model calls occur.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")]
ReportText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Version = Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
Byte = Annotated[int, Field(ge=0, le=255)]
I2CAddress = Annotated[int, Field(ge=0x08, le=0x77)]
Mode = Literal["read_only", "passive"]
Assessment = Literal["pass", "fail", "unknown", "not_applicable"]
ToolStatus = Literal["ok", "error", "blocked", "unsupported", "timeout"]
RunState = Literal[
    "created", "connected", "inspected", "probe_ready", "identified", "reported",
    "blocked", "expired", "cancelled",
]
ErrorCode = Literal[
    "host_key_unknown", "host_key_mismatch", "authentication_failed",
    "network_timeout", "dependency_missing", "permission_denied",
    "adapter_not_found", "driver_bound", "transfer_failed", "identity_mismatch",
    "policy_denied", "resource_limit", "remote_state_unknown", "invalid_request",
    "state_changed", "run_expired", "operation_conflict", "target_busy", "storage_error",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class RunRequest(Contract):
    run_id: Identifier


class ProfileRequest(Contract):
    profile_id: Identifier


class StartDiagnosisRequest(Contract):
    target_id: Identifier
    profile_id: Identifier
    mode: Mode = "read_only"


class IdentityReadRequest(RunRequest):
    bus_ref: Identifier
    address: I2CAddress
    operation_id: Identifier


class Hypothesis(Contract):
    """Model-authored explanation, never a source of observations."""

    hypothesis_id: Identifier
    explanation: Annotated[str, Field(min_length=1, max_length=4000)]
    supporting_evidence: tuple[Identifier, ...] = ()
    opposing_evidence: tuple[Identifier, ...] = ()


class FinalizeReportRequest(RunRequest):
    hypotheses: tuple[Hypothesis, ...] = ()


class DiagnosticError(Contract):
    code: ErrorCode
    message: Annotated[str, Field(min_length=1, max_length=2000)]


class ToolResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    tool_version: Version
    policy_version: Version
    profile_version: Version
    run_id: Identifier
    target_id: Identifier
    execution_id: Identifier
    request_id: Identifier
    status: ToolStatus
    assessment: Assessment = "unknown"
    observations: dict[str, JsonValue] = Field(default_factory=dict)
    error: DiagnosticError | None = None
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    collected_at: datetime
    duration_ms: Annotated[int, Field(ge=0)]
    completeness: Literal["complete", "partial"] = "complete"
    truncated: bool = False
    retry_count: Annotated[int, Field(ge=0, le=1)] = 0

    @field_validator("collected_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("collected_at must have an explicit UTC offset")
        return value

    @model_validator(mode="after")
    def consistent_outcome(self) -> Self:
        if (self.status == "ok") != (self.error is None):
            raise ValueError("ok results cannot contain errors; other statuses require one")
        if self.assessment == "pass" and (
            self.status != "ok" or self.completeness != "complete" or self.truncated
        ):
            raise ValueError("failed or incomplete execution cannot establish a pass")
        if self.truncated and self.completeness != "partial":
            raise ValueError("truncated results must be partial")
        return self


class EvidenceRecord(Contract):
    evidence_id: Identifier
    result: ToolResult

    @field_validator("result")
    @classmethod
    def detach_result(cls, result: ToolResult) -> ToolResult:
        # frozen=True is shallow. Never share caller-owned observation dicts.
        return ToolResult.model_validate_json(result.model_dump_json(warnings="error"))

    @model_validator(mode="after")
    def result_contains_evidence_id(self) -> Self:
        if self.evidence_id not in self.result.evidence_ids:
            raise ValueError("result must reference its evidence record")
        return self


class Finding(Contract):
    finding_id: Identifier
    rule_id: Identifier
    assessment: Assessment
    explanation: Annotated[str, Field(min_length=1, max_length=4000)]
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]


class DiagnosticReport(Contract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: Identifier
    target_id: Identifier
    outcome: Literal["complete", "partial", "interrupted"]
    evidence: tuple[EvidenceRecord, ...] = ()
    findings: tuple[Finding, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    limitations: Annotated[tuple[ReportText, ...], Field(min_length=1)]
    next_actions: tuple[ReportText, ...] = ()

    @field_validator("evidence")
    @classmethod
    def detach_evidence(cls, records: tuple[EvidenceRecord, ...]) -> tuple[EvidenceRecord, ...]:
        return tuple(EvidenceRecord.model_validate_json(record.model_dump_json(warnings="error")) for record in records)

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        if self.outcome == "complete" and not self.evidence:
            raise ValueError("complete report requires recorded evidence")
        known: set[str] = set()
        for record in self.evidence:
            if record.evidence_id in known:
                raise ValueError("duplicate evidence ID")
            known.add(record.evidence_id)
            if (record.result.run_id, record.result.target_id) != (self.run_id, self.target_id):
                raise ValueError("evidence belongs to another run or target")
        references = [ref for finding in self.findings for ref in finding.evidence_ids]
        references += [
            ref for hypothesis in self.hypotheses
            for ref in (*hypothesis.supporting_evidence, *hypothesis.opposing_evidence)
        ]
        if not set(references).issubset(known):
            raise ValueError("unknown evidence reference")
        results = {record.evidence_id: record.result for record in self.evidence}
        for finding in self.findings:
            if finding.assessment == "pass" and not any(
                results[ref].status == "ok" and results[ref].completeness == "complete"
                and not results[ref].truncated for ref in finding.evidence_ids
            ):
                raise ValueError("pass finding requires complete successful evidence")
        for record in self.evidence:
            if not set(record.result.evidence_ids).issubset(known):
                raise ValueError("result references unavailable evidence")
        return self

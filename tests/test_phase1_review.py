"""Regressions for evidence aliasing and empty report text found in review."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from embedsleuth.contracts import DiagnosticReport, EvidenceRecord, ToolResult


@pytest.fixture
def observed_result():
    return ToolResult(
        tool_version="0.1.0", policy_version="0.1.0", profile_version="0.1.0",
        run_id="run-review", target_id="lab-pi", execution_id="execution-review",
        request_id="request-review", status="ok", assessment="pass",
        observations={"identity": 104, "details": {"bytes": [104]}},
        evidence_ids=("evidence-review",),
        collected_at=datetime(2026, 11, 18, tzinfo=timezone.utc), duration_ms=1,
    )


def make_report(record, **overrides):
    fields = {
        "run_id": "run-review", "target_id": "lab-pi", "outcome": "complete",
        "evidence": (record,), "limitations": ("Electrical conditions unmeasured.",),
    }
    fields.update(overrides)
    return DiagnosticReport(**fields)


def test_evidence_preserves_observations_after_original_result_is_mutated(observed_result):
    record = EvidenceRecord(evidence_id="evidence-review", result=observed_result)

    observed_result.observations["identity"] = 0
    observed_result.observations["details"]["bytes"].append(0)

    assert record.result.observations == {"identity": 104, "details": {"bytes": [104]}}


def test_report_preserves_observations_after_original_evidence_is_mutated(observed_result):
    record = EvidenceRecord(evidence_id="evidence-review", result=observed_result)
    report = make_report(record)

    record.result.observations["identity"] = 0
    record.result.observations["details"]["bytes"].clear()

    assert report.evidence[0].result.observations == {
        "identity": 104, "details": {"bytes": [104]},
    }


@pytest.mark.parametrize("field", ["limitations", "next_actions"])
@pytest.mark.parametrize("text", ["", " ", "\t\n", "x" * 4001])
def test_report_rejects_blank_or_oversized_text(observed_result, field, text):
    record = EvidenceRecord(evidence_id="evidence-review", result=observed_result)
    with pytest.raises(ValidationError):
        make_report(record, **{field: (text,)})


def test_report_normalizes_surrounding_whitespace(observed_result):
    record = EvidenceRecord(evidence_id="evidence-review", result=observed_result)
    report = make_report(
        record, limitations=("  Voltage not measured.\n",),
        next_actions=("\tCheck wiring.  ",),
    )
    assert report.limitations == ("Voltage not measured.",)
    assert report.next_actions == ("Check wiring.",)


@pytest.mark.parametrize("boundary", ["evidence", "report"])
def test_serialization_rechecks_mutated_observations_before_accepting_evidence(
    observed_result, boundary,
):
    record = EvidenceRecord(evidence_id="evidence-review", result=observed_result)
    if boundary == "evidence":
        observed_result.observations["injected"] = object()
    else:
        record.result.observations["injected"] = object()

    with pytest.raises((ValidationError, PydanticSerializationError)):
        if boundary == "evidence":
            EvidenceRecord(evidence_id="evidence-review", result=observed_result)
        else:
            make_report(record)

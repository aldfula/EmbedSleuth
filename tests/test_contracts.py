import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from conftest import changed
from embedsleuth.contracts import DiagnosticReport, ToolResult
from embedsleuth.tools import tool_input_schemas, validate_arguments


@pytest.fixture
def result():
    return ToolResult(
        tool_version="0.1.0", policy_version="0.1.0", profile_version="0.1.0",
        run_id="run-1", target_id="lab-pi", execution_id="exec-1", request_id="req-1",
        status="ok", assessment="pass", observations={"identity": 104},
        evidence_ids=("ev-1",), collected_at=datetime(2026, 11, 17, tzinfo=timezone.utc),
        duration_ms=5,
    )


def test_default_diagnosis_mode_is_read_only():
    request = validate_arguments("start_diagnosis", {"target_id": "lab-pi", "profile_id": "mpu6050"})
    assert request.mode == "read_only"


@pytest.mark.parametrize("key,value", [
    ("command", "sudo reboot"), ("host", "192.0.2.1"), ("approved", True),
    ("force", True), ("register", 107), ("length", 32),
    ("driver_bound", False), ("preconditions_fresh", True),
])
def test_model_cannot_supply_execution_or_authorization_fields(key, value):
    args = {"run_id": "run-1", "bus_ref": "bus-1", "address": 104, "operation_id": "op-1", key: value}
    with pytest.raises(ValidationError):
        validate_arguments("read_device_identity", args)


@pytest.mark.parametrize("address", [True, "104", 104.0, -1, 0, 128])
def test_wire_address_requires_valid_integer(address):
    with pytest.raises(ValidationError):
        validate_arguments("read_device_identity", {
            "run_id": "run-1", "bus_ref": "bus-1", "address": address, "operation_id": "op-1",
        })


@pytest.mark.parametrize("target", ["pi@example.com", "../pi", "pi;reboot", "/tmp/key", ""])
def test_target_is_identifier_not_connection_string(target):
    with pytest.raises(ValidationError):
        validate_arguments("start_diagnosis", {"target_id": target, "profile_id": "mpu6050"})


def test_unknown_tool_cannot_be_executed():
    with pytest.raises(ValueError, match="unknown diagnostic tool"):
        validate_arguments("run_shell", {"command": "reboot"})


def test_tool_catalog_exports_closed_json_schemas():
    schemas = tool_input_schemas()
    assert len(schemas) == 11
    assert all(schema["additionalProperties"] is False for schema in schemas.values())
    assert schemas["read_device_identity"]["properties"]["address"]["minimum"] == 8


@pytest.mark.parametrize("updates", [
    {"status": "timeout", "assessment": "pass", "error": {"code": "network_timeout", "message": "timeout"}},
    {"status": "error"},
    {"status": "ok", "error": {"code": "permission_denied", "message": "denied"}},
    {"completeness": "partial"},
    {"truncated": True},
    {"duration_ms": -1},
    {"collected_at": "2026-11-17T12:00:00"},
    {"collected_at": "2026-11-17T12:00:00+09:00"},
    {"schema_version": "2.0"},
    {"evidence_ids": []},
])
def test_result_rejects_contradictory_or_invalid_outcomes(result, updates):
    with pytest.raises(ValidationError):
        changed(result, **updates)


def report_payload(result):
    return {
        "run_id": "run-1", "target_id": "lab-pi", "outcome": "complete",
        "evidence": [{"evidence_id": "ev-1", "result": result.model_dump(mode="json")}],
        "findings": [{"finding_id": "finding-1", "rule_id": "identity-match", "assessment": "pass",
                      "explanation": "Matches expected identity; accuracy is unverified.", "evidence_ids": ["ev-1"]}],
        "limitations": ["Electrical conditions were not measured."],
    }


def test_report_round_trip_preserves_provenance(result):
    report = DiagnosticReport.model_validate_json(json.dumps(report_payload(result)))
    assert DiagnosticReport.model_validate_json(report.model_dump_json()) == report


@pytest.mark.parametrize("mutation", ["unknown_ref", "other_run", "other_target", "duplicate", "missing_self", "unavailable_result_ref", "unsupported_hypothesis", "no_limitations"])
def test_report_rejects_fabricated_or_cross_run_provenance(result, mutation):
    payload = report_payload(result)
    if mutation == "unknown_ref": payload["findings"][0]["evidence_ids"] = ["not-observed"]
    if mutation == "other_run": payload["evidence"][0]["result"]["run_id"] = "other-run"
    if mutation == "other_target": payload["evidence"][0]["result"]["target_id"] = "other-pi"
    if mutation == "duplicate": payload["evidence"] *= 2
    if mutation == "missing_self": payload["evidence"][0]["result"]["evidence_ids"] = []
    if mutation == "unavailable_result_ref": payload["evidence"][0]["result"]["evidence_ids"].append("missing")
    if mutation == "unsupported_hypothesis":
        payload["hypotheses"] = [{"hypothesis_id": "h-1", "explanation": "Claim", "supporting_evidence": ["missing"]}]
    if mutation == "no_limitations": payload["limitations"] = []
    with pytest.raises(ValidationError):
        DiagnosticReport.model_validate_json(json.dumps(payload))


def test_partial_permission_failure_is_valid_and_not_sensor_failure(result):
    failed = changed(result, status="blocked", assessment="unknown", completeness="partial",
                     error={"code": "permission_denied", "message": "Cannot access bus"})
    assert failed.assessment == "unknown"
    assert failed.error.code == "permission_denied"


def test_empty_complete_report_is_not_a_diagnosis():
    with pytest.raises(ValidationError, match="requires recorded evidence"):
        DiagnosticReport(run_id="run-1", target_id="lab-pi", outcome="complete",
                         limitations=("No observations",))


def test_pass_finding_cannot_rely_only_on_failed_execution(result):
    failed = changed(result, status="error", assessment="unknown",
                     error={"code": "transfer_failed", "message": "No response"})
    with pytest.raises(ValidationError, match="requires complete successful evidence"):
        DiagnosticReport.model_validate_json(json.dumps(report_payload(failed)))

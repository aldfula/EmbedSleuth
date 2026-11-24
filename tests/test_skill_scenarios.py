"""Replay reviewed skill procedures through real MCP/core and fake device IO.

These tests establish that the documented traces work with the actual tools.
They do NOT run a model or prove that an AI chooses these traces from prompts.
"""

import asyncio
from copy import deepcopy
import json
from pathlib import Path

from mcp import Client
import pytest

from embedsleuth.contracts import DiagnosticReport
from embedsleuth.mcp_server import create_server
from embedsleuth.tools import validate_arguments
from embedsleuth.transport import ProbeResponse
from test_core import make_harness


CASES = json.loads((Path(__file__).resolve().parents[1] / "evals/diagnose-mpu6050/cases.json").read_text())


@pytest.mark.parametrize("case", CASES["offline_replays"], ids=lambda case: case["id"])
def test_reviewed_skill_trace_is_executable_and_preserves_evidence(make_harness, case):
    fixture = deepcopy(case["fixture"])
    changes = fixture.get("target", {})
    if "expected_addresses" in changes:
        changes["expected_addresses"] = tuple(changes["expected_addresses"])
    h = make_harness(target_changes=changes)
    h.transport.overrides.update(fixture.get("observations", {}))
    h.transport.overrides.update({name: ProbeResponse(**value) for name, value in fixture.get("responses", {}).items()})
    variables = {}
    received = []

    def resolve(value):
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if isinstance(value, str) and value.startswith("$"):
            return variables[value]
        return value

    async def scenario():
        async with Client(create_server(h.core), read_timeout_seconds=3) as client:
            for step in case["trace"]:
                if "advance_seconds" in step:
                    h.clock.now += step["advance_seconds"]
                    continue
                name, arguments = step["tool"], resolve(step["arguments"])
                validate_arguments(name, arguments)
                response = await client.call_tool(name, arguments)
                envelope = response.structured_content
                assert json.loads(response.content[0].text) == envelope
                code = envelope["error"]["code"] if envelope["error"] else None
                assert code == step["expect_error"]
                assert response.is_error is (code is not None)
                result = envelope["result"]
                received.append((name, result))
                if name == "start_diagnosis":
                    variables["$run_id"] = result["run_id"]
                if name == "read_device_identity":
                    variables["$identity_evidence"] = result["evidence_ids"][0]
            return DiagnosticReport.model_validate_json(json.dumps(received[-1][1]))

    report = asyncio.run(scenario())
    expected = case["expected"]
    assert report.run_id == variables["$run_id"]
    assert report.outcome == expected["outcome"]
    assert h.transport.count("read_device_identity") == expected["identity_reads"]
    assert h.store.is_quarantined("lab-pi") is expected["quarantined"]
    identity = [finding for finding in report.findings if finding.rule_id == "identity-match"]
    if expected["identity_assessment"] is None:
        assert identity == []
    else:
        assert len(identity) == 1 and identity[0].assessment == expected["identity_assessment"]
    persisted = DiagnosticReport.model_validate_json(json.dumps(h.store.load_report(report.run_id)))
    assert persisted == report
    evidence = {record.evidence_id for record in report.evidence}
    for finding in report.findings:
        assert set(finding.evidence_ids) <= evidence
    for hypothesis in report.hypotheses:
        assert set(hypothesis.supporting_evidence + hypothesis.opposing_evidence) <= evidence
    assert h.store.load_state(report.run_id)["pending_operation"] is None

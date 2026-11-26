"""Deterministic acceptance summaries, separate from model behavior grading.

No network, hardware, configuration updates or agent loop occurs here.
"""

import json
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from embedsleuth.contracts import Contract, DiagnosticReport


class ResourceSample(Contract):
    schema_version: Literal["1.0"]
    available: Literal[True]
    measurement: Literal["linux_getrusage"]
    scope: Literal["probe_and_single_waited_child"]
    probe_peak_rss_kib: Annotated[int, Field(gt=0)]
    child_peak_rss_kib: Annotated[int, Field(ge=0)]
    peak_sum_bound_kib: Annotated[int, Field(gt=0)]
    elapsed_ms: Annotated[int, Field(ge=0)]
    cpu_ms: Annotated[int, Field(ge=0)]
    pid: Annotated[int, Field(gt=0)]
    process_start_ticks: Annotated[str, Field(pattern=r"^[0-9]{1,30}$")] | None

    @model_validator(mode="after")
    def consistent_sum(self) -> Self:
        if self.peak_sum_bound_kib != self.probe_peak_rss_kib + self.child_peak_rss_kib:
            raise ValueError("inconsistent peak bound")
        return self


def summarize(report: DiagnosticReport):
    # Cached results may have more than one reference; count a request once.
    results = {item.result.request_id: item.result for item in report.evidence}
    samples, missing = [], 0
    for result in results.values():
        # Local preflight denials are not remote measurements.
        if result.status == "blocked" and "resource_usage" not in result.observations:
            continue
        try:
            sample = ResourceSample.model_validate_json(json.dumps(result.observations.get("resource_usage")))
        except ValueError:
            missing += 1
        else:
            samples.append(sample)
    peak = max((sample.peak_sum_bound_kib for sample in samples), default=None)
    memory = ("fail" if peak is not None and peak > 65536 else
              "pass" if samples and not missing else "unknown")
    durations = [result.duration_ms for result in results.values()]
    identity = [finding.assessment for finding in report.findings if finding.rule_id == "identity-match"]
    return {
        "schema_version": "1.0", "run_id": report.run_id, "target_id": report.target_id,
        "report_outcome": report.outcome,
        "execution": {"assessment": ("pass" if results and all(
            result.status == "ok" and not result.truncated and result.completeness == "complete"
            for result in results.values()) else "fail" if results else "unknown"),
            "error_codes": sorted({result.error.code for result in results.values() if result.error})},
        "probe_memory": {"assessment": memory, "peak_sum_bound_kib": peak, "budget_kib": 65536,
                         "sample_count": len(samples), "unmeasured_result_count": missing,
                         "scope": "probe_and_single_waited_child",
                         "excluded": ["SSH daemon", "launching shell", "other system processes"],
                         "system_wide_incremental_rss": "not_measured"},
        "tool_time": {"assessment": ("pass" if durations and max(durations) <= 10200 else
                                     "fail" if durations else "unknown"),
                      "max_duration_ms": max(durations, default=None),
                      "timeout_ms": 10000, "cleanup_allowance_ms": 200},
        "identity": {"assessment": "pass" if "pass" in identity else "fail" if "fail" in identity else "unknown"},
        "model_behavior": "not_evaluated",
    }

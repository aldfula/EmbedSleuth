"""Charged-memory bounds never become a proof of whole incremental peak RSS."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


observer = load("cgroup_observer_test", "evals/diagnose-mpu6050/cgroup_observer.py")
runner = load("cgroup_acceptance_test", "scripts/run_cgroup_acceptance.py")


def rows(peak=1024):
    return [{"path": path, "inode": i+1, "current_bytes": 512, "peak_bytes": peak}
            for i, path in enumerate(observer.GROUPS)]


@pytest.mark.parametrize("path,expected", [("/user.slice/user-1000.slice/session-1.scope", True),
    ("/system.slice/ssh.service", True), ("/system.slice/other.service", False),
    ("/user.sliceevil", False), ("/", False)])
def test_accounting_coverage_uses_path_boundaries(path, expected):
    assert observer.covered(path) is expected


@pytest.mark.parametrize("text", ["", "1:name=systemd:/user.slice", "0::/../user.slice", "0::/a\n0::/b"])
def test_missing_or_ambiguous_membership_is_not_coverage(text):
    with pytest.raises(ValueError):
        observer.group_path(text)


def test_bounds_sum_independent_lifetime_peaks_without_subtracting_old_peaks():
    before, after = rows(1000), rows(3000)
    assert observer.bounds(before, after) == 6000


@pytest.mark.parametrize("change", ["replaced", "reset", "missing", "negative", "boolean", "wrong_group"])
def test_changed_or_unavailable_counters_cannot_establish_a_bound(change):
    before, after = rows(), rows()
    if change == "replaced": after[0]["inode"] += 10
    if change == "reset": after[0]["peak_bytes"] -= 1
    if change == "missing": after.pop()
    if change == "negative": after[0]["current_bytes"] = -1
    if change == "boolean": after[0]["peak_bytes"] = True
    if change == "wrong_group": after[0]["path"] = "/system.slice"
    with pytest.raises(ValueError):
        observer.bounds(before, after)


@pytest.mark.parametrize("change", ["valid", "missing_probe", "outside_probe", "unreadable_ssh", "outside_ssh", "large", "changed_boot", "missing_boot"])
def test_budget_pass_requires_coverage_and_still_does_not_prove_rss(change):
    measurement = {"boot_id": "boot-one", "before": rows(), "after": rows(), "limitations": ["charged memory differs from RSS"],
                   "ssh_memberships": {"groups": ["/system.slice/ssh.service"], "unreadable_pids": []}}
    report = {"run_id": "run-test", "outcome": "partial", "evidence": [{"evidence_id": "ev-1", "result": {
        "observations": {"boot_id": "boot-one", "process_cgroup": "0::/user.slice/user-1000.slice/session-1.scope"}}}]}
    if change == "missing_probe": report["evidence"][0]["result"]["observations"].clear()
    if change == "outside_probe": report["evidence"][0]["result"]["observations"]["process_cgroup"] = "0::/other"
    if change == "unreadable_ssh": measurement["ssh_memberships"]["unreadable_pids"] = [123]
    if change == "outside_ssh": measurement["ssh_memberships"]["groups"] = ["/system.slice/custom.service"]
    if change == "large": measurement["after"] = rows(64*1024*1024)
    if change == "changed_boot": report["evidence"][0]["result"]["observations"]["boot_id"] = "boot-two"
    if change == "missing_boot": measurement.pop("boot_id")
    result = runner.grade(measurement, report)
    assert result["charged_memory_64mib_assessment"] == ("pass" if change == "valid" else "unknown")
    assert result["system_incremental_peak_rss_assessment"] == "unknown"


@pytest.mark.parametrize("execution,memory,code", [("pass", "pass", 0), ("fail", "pass", 1),
    ("pass", "unknown", 3), ("unknown", "pass", 3), ("fail", "unknown", 1)])
def test_unknown_memory_or_failed_checks_never_return_success(execution, memory, code):
    assert runner.exit_code({"acceptance": {"deterministic_checks": execution},
                             "memory_checks": {"charged_memory_64mib_assessment": memory}}) == code

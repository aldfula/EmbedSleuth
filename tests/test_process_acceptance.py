"""Process observation must never turn sampling into a full peak guarantee."""

import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("observer", Path(__file__).parents[1] /
                                            "evals/diagnose-mpu6050/process_observer.py")
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


def stat(pid=12, start=99, rss=7):
    fields = ["S"] + ["0"] * 21
    fields[19], fields[21] = str(start), str(rss)
    return f"{pid} (a tricky ) process) " + " ".join(fields)


def test_stat_parser_handles_comm_parentheses_and_page_units():
    assert observer.parse_stat(stat(), 4) == ((12, 99), 28)


@pytest.mark.parametrize("text", ["broken", stat(pid=-1), stat(rss=-1), stat(start=-1)])
def test_invalid_counters_cannot_prove_a_process_absent(text):
    with pytest.raises((ValueError, IndexError)):
        observer.parse_stat(text, 4)


def test_rss_delta_excludes_observer_and_does_not_cancel_growth_with_shrinkage():
    assert observer.positive_delta({(1, 1): 40, (2, 1): 100},
                                   {(1, 1): 50, (2, 1): 10, (3, 2): 30, (9, 1): 100}, 9) == 40


@pytest.mark.parametrize("start,keys,unreadable,expected", [
    ("99", [[12, 99]], [], "still_present"),
    ("99", [[12, 100]], [], "not_present_at_final_sample"),
    ("99", [], [], "not_present_at_final_sample"),
    ("99", [], [12], "unknown"),
    (None, [], [], "unknown"),
])
def test_exit_checks_bind_pid_to_start_time_and_preserve_uncertainty(start, keys, unreadable, expected):
    assert observer.probe_exit_status(12, start, keys, unreadable) == expected


@pytest.mark.parametrize("pid,start", [(0, "1"), (-1, "1"), (True, "1"), (12, "1" * 31)])
def test_bad_identity_cannot_claim_a_process_exited(pid, start):
    assert observer.probe_exit_status(pid, start, [], []) == "unknown"


@pytest.mark.parametrize("peak", [1, 65536, 65537])
def test_sampling_never_establishes_a_peak_or_whole_tree_exit_guarantee(peak):
    runner_spec = importlib.util.spec_from_file_location("process_runner", Path(__file__).parents[1] /
                                                       "scripts/run_process_acceptance.py")
    runner = importlib.util.module_from_spec(runner_spec)
    runner_spec.loader.exec_module(runner)
    result = runner.check_result({"peak_observed_kib": peak, "final_process_keys": [], "unreadable_pids": []},
                                 {"run_id": "test-run", "outcome": "partial", "evidence": []}, 0)
    assert result["system_incremental_peak_assessment"] == "unknown"
    assert result["full_process_tree_exit_assessment"] == "unknown"
    assert result["sampled_64mib_exceeded"] is (peak > 65536)

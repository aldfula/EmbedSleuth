"""Cancellation grading must distinguish RPC cancellation from physical termination."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


observer = load('cancel_observer', 'evals/diagnose-mpu6050/cancellation_observer.py')
runner = load('cancel_runner', 'scripts/run_cancel_acceptance.py')
EVENTS = [{'action': 'before', 'operation_id': 'op-1', 'tool': 'inspect_target'},
          {'action': 'after', 'operation_id': 'op-1'}]


def test_only_exact_production_source_counts_as_a_probe():
    source = b'import json\nprint(1)\n'
    digest = hashlib.sha256(source).hexdigest()
    command = b'python3\0-I\0-B\0-c\0' + source + b'\0'
    assert observer.exact_probe(command, digest)
    assert not observer.exact_probe(command.replace(b'-I', b'-E'), digest)
    assert not observer.exact_probe(command + b'extra\0', digest)
    assert not observer.exact_probe(command, 'f' * 64)


def test_process_identity_parses_parentheses_and_start_ticks():
    fields = ['S', '19'] + ['0'] * 20
    fields[19] = '991'
    assert observer.process_key('21 (python ) something) ' + ' '.join(fields)) == ((21, 991), 19)


@pytest.mark.parametrize('change', ['not_pending', 'not_seen', 'still_present', 'unreadable', 'no_processes',
                                  'no_quarantine', 'pending_intent', 'not_interrupted', 'remote_idle'])
def test_incomplete_conditions_do_not_count_as_cancellation_success(change):
    report = {'run_id': 'run-test', 'outcome': 'interrupted', 'evidence': []}
    state = {'pending_operation': None, 'remote_state': 'unknown'}
    observation = {'probe_seen': True, 'captured_processes': [
        {'present_at_end': False, 'readable_at_end': True}]}
    pending, quarantined = True, True
    if change == 'not_pending': pending = False
    if change == 'not_seen': observation['probe_seen'] = False
    if change == 'still_present': observation['captured_processes'][0]['present_at_end'] = True
    if change == 'unreadable': observation['captured_processes'][0]['readable_at_end'] = False
    if change == 'no_processes': observation['captured_processes'] = []
    if change == 'no_quarantine': quarantined = False
    if change == 'pending_intent': state['pending_operation'] = {'operation_id': 'op-1'}
    if change == 'not_interrupted': report['outcome'] = 'partial'
    if change == 'remote_idle': state['remote_state'] = 'idle'
    assert runner.grade(report, state, quarantined, pending, observation, EVENTS)['assessment'] == 'inconclusive'


def test_pass_still_does_not_claim_remote_execution_or_whole_tree_termination():
    report = {'run_id': 'run-test', 'outcome': 'interrupted', 'evidence': []}
    result = runner.grade(report, {'pending_operation': None, 'remote_state': 'unknown'}, True, True,
                          {'probe_seen': True, 'captured_processes': [
                              {'present_at_end': False, 'readable_at_end': True}]}, EVENTS)
    assert result['assessment'] == 'pass'
    assert result['remote_running_at_cancel_receipt'] == 'unknown'
    assert result['whole_process_tree_exit'] == 'unknown'
    assert result['identity_reads'] == 0


@pytest.mark.parametrize("events", [[], EVENTS[:1], EVENTS[1:]])
def test_open_or_missing_audit_is_not_success(events):
    result = runner.grade({'run_id': 'run-test', 'outcome': 'interrupted', 'evidence': []},
        {'pending_operation': None, 'remote_state': 'unknown'}, True, True,
        {'probe_seen': True, 'captured_processes': [{'present_at_end': False, 'readable_at_end': True}]}, events)
    assert result['assessment'] == 'inconclusive'


@pytest.mark.parametrize("assessment,code", [("pass", 0), ("inconclusive", 3)])
def test_cli_preserves_result_and_reports_inconclusive_as_nonzero(monkeypatch, tmp_path, assessment, code):
    output = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["cancel", "--config", "/unused", "--target", "pi-1",
                                      "--output", str(output), "--confirm-quarantine"])
    monkeypatch.setattr(runner, "run", lambda *_: {"checks": {"assessment": assessment}})
    assert runner.main() == code
    assert json.loads(output.read_text())["checks"]["assessment"] == assessment
    assert output.stat().st_mode & 0o777 == 0o600

from copy import deepcopy
import json

import pytest

from embedsleuth.core import SessionError
from embedsleuth.sampling import approve_plan, decode_samples
from embedsleuth.tools import validate_arguments
from test_core import make_harness, ready, read


def observations(plan_id="sampling-" + "a" * 64):
    return {"address": 104, "plan_id": plan_id, "wakeup_attempted": True,
            "restoration_confirmed": True, "restored_pwr_mgmt_1": 64,
            "configuration": {"gyro_config": 0, "accel_config": 0, "pwr_mgmt_1": 64,
                              "pwr_mgmt_2": 0, "user_ctrl": 0},
            "samples": [[0, 0, 0, 0, 64, 0, 0, 0, 0, 131, 255, 125, 0, 0]] * 5}


def planned(h, mode="sensor_check"):
    run = ready(h, mode=mode)
    assert read(h, run).assessment == "pass"
    plan = h.core.prepare_sensor_sampling(run, "bus-1", 104)
    h.transport.overrides['sample_sensor_data'] = observations(plan['plan_id'])
    return run, plan['plan_id']


def approve(h, run, plan_id):
    approve_plan(h.store, h.registry, run, plan_id, now=h.clock.utcnow())


def test_signed_words_current_ranges_temperature_and_non_health_warnings():
    decoded = decode_samples(observations())
    sample = decoded['samples'][0]
    assert sample['acceleration_g'] == [0, 0, 1]
    assert sample['angular_velocity_dps'] == [1, -1, 0]
    assert sample['temperature_c'] == 36.53
    assert decoded['health_assessment'] == 'unknown'
    assert 'identical_snapshots_do_not_prove_a_fault' in decoded['warnings']


@pytest.mark.parametrize('change', ['bytes', 'bool', 'count', 'range', 'self_test', 'restore', 'wake', 'power', 'restored'])
def test_malformed_or_unrestored_data_never_passes(change):
    data = deepcopy(observations())
    if change == 'bytes': data['samples'][0] = [0] * 13
    if change == 'bool': data['samples'][0][0] = True
    if change == 'count': data['samples'] = []
    if change == 'range': data['configuration']['gyro_config'] = 256
    if change == 'self_test': data['configuration']['accel_config'] = 128
    if change == 'restore': data['restoration_confirmed'] = False
    if change == 'wake': data['wakeup_attempted'] = False
    if change == 'power': data['configuration']['pwr_mgmt_1'] = 72
    if change == 'restored': data['restored_pwr_mgmt_1'] = 0
    with pytest.raises(ValueError): decode_samples(data)


@pytest.mark.parametrize('mode', ['read_only', 'passive'])
def test_old_modes_cannot_prepare_or_execute_writes(make_harness, mode):
    h = make_harness()
    run = ready(h, mode=mode)
    with pytest.raises(SessionError): h.core.prepare_sensor_sampling(run, 'bus-1', 104)
    result = h.core.sample_sensor_data(run, 'absent', 'sample-1')
    assert result.status == 'blocked' and h.transport.count('sample_sensor_data') == 0


def test_requires_local_approval_and_executes_only_once_with_report(make_harness):
    h = make_harness()
    run, plan_id = planned(h)
    assert h.core.get_report(run).outcome == 'partial'
    assert h.core.sample_sensor_data(run, plan_id, 'sampling-1').status == 'blocked'
    assert h.transport.count('sample_sensor_data') == 0
    approve(h, run, plan_id)
    result = h.core.sample_sensor_data(run, plan_id, 'sampling-1')
    assert result.status == 'ok' and result.assessment == 'pass'
    assert result.observations['decoded']['health_assessment'] == 'unknown'
    assert h.core.sample_sensor_data(run, plan_id, 'sampling-1') == result
    assert h.core.sample_sensor_data(run, plan_id, 'sampling-2').status == 'blocked'
    assert h.transport.count('sample_sensor_data') == 1
    assert any(f.rule_id == 'sensor-data-acquisition' for f in h.core.evaluate_findings(run))
    # An earlier denied request is preserved, so this run remains partial.
    assert h.core.finalize_report(run).outcome == 'partial'


def test_successful_sensor_report_has_acquisition_not_health_conclusion(make_harness):
    h = make_harness(); run, plan_id = planned(h); approve(h, run, plan_id)
    result = h.core.sample_sensor_data(run, plan_id, 'sample-1')
    assert result.status == 'ok'
    report = h.core.finalize_report(run)
    assert report.outcome == 'complete'
    assert any('health' in f.explanation for f in report.findings if f.rule_id == 'sensor-data-acquisition')


@pytest.mark.parametrize('changed', ['expired', 'cancelled', 'quarantined', 'trust', 'digest', 'procedure', 'address', 'stale'])
def test_changed_or_unapproved_plan_never_dispatches(make_harness, changed):
    h = make_harness(); run, plan_id = planned(h); approve(h, run, plan_id)
    if changed == 'expired': h.clock.now += 121
    if changed == 'stale': h.clock.now += 31
    if changed == 'cancelled': h.core.cancel_diagnosis(run)
    if changed == 'quarantined': h.store.quarantine_target('lab-pi', 'test')
    if changed == 'trust': h.trust.write_text('changed trust')
    if changed in ('digest', 'procedure', 'address'):
        state = h.store.load_state(run)
        if changed == 'digest': state['sampling_plan']['plan_id'] = 'changed'
        if changed == 'procedure': state['sampling_plan']['plan']['procedure']['clear_mask'] = 255
        if changed == 'address': state['sampling_plan']['plan']['address'] = 105
        h.store.save_state(run, state)
    assert h.core.sample_sensor_data(run, plan_id, 'sample-1').status == 'blocked'
    assert h.transport.count('sample_sensor_data') == 0


@pytest.mark.parametrize('bad', ['restoration', 'binding', 'bytes', 'unknown_flag'])
def test_bad_remote_success_is_quarantined_and_cannot_be_retried(make_harness, bad):
    h = make_harness(); run, plan_id = planned(h); approve(h, run, plan_id)
    data = h.transport.overrides['sample_sensor_data']
    if bad == 'restoration': data['restoration_confirmed'] = False
    if bad == 'binding': data['boot_id'] = 'different'
    if bad == 'bytes': data['samples'] = []
    if bad == 'unknown_flag':
        from embedsleuth.transport import ProbeResponse
        h.transport.overrides['sample_sensor_data'] = ProbeResponse(
            observations=h.store.load_state(run)['environment'] | data, remote_state_unknown=True)
    result = h.core.sample_sensor_data(run, plan_id, 'sample-1')
    assert result.status == 'error' and h.store.is_quarantined('lab-pi')
    assert h.core.sample_sensor_data(run, plan_id, 'sample-1') == result
    assert h.transport.count('sample_sensor_data') == 1
    assert h.core.get_report(run).outcome == 'interrupted'


@pytest.mark.parametrize('injected', ['approved', 'register', 'command', 'count', 'wake'])
def test_mcp_cannot_inject_approval_commands_or_expand_procedure(injected):
    with pytest.raises(ValueError):
        validate_arguments('sample_sensor_data', {'run_id': 'run-1', 'plan_id': 'plan-1',
                                                'operation_id': 'op-1', injected: True})


def test_sampling_budget_is_reserved_before_dispatch(make_harness):
    h = make_harness(tool_timeout=8)
    run, plan_id = planned(h); approve(h, run, plan_id)
    result = h.core.sample_sensor_data(run, plan_id, 'sample-1')
    assert result.status == 'blocked' and h.transport.count('sample_sensor_data') == 0
    assert h.store.load_state(run)['sampling_plan']['consumed'] is True


@pytest.mark.parametrize('accel_setting', range(4))
@pytest.mark.parametrize('gyro_setting', range(4))
def test_conversion_uses_observed_ranges_without_reconfiguring(accel_setting, gyro_setting):
    data = observations()
    data['configuration']['accel_config'] = accel_setting << 3
    data['configuration']['gyro_config'] = gyro_setting << 3
    sample = decode_samples(data)['samples'][0]
    assert sample['acceleration_g'][2] == 16384 / (16384, 8192, 4096, 2048)[accel_setting]
    assert sample['angular_velocity_dps'][0] == 131 / (131, 65.5, 32.8, 16.4)[gyro_setting]


@pytest.mark.parametrize('problem', ['expired', 'cancelled', 'quarantined', 'wrong_plan'])
def test_local_approval_checks_current_plan_state(make_harness, problem):
    h = make_harness(); run, plan_id = planned(h)
    if problem == 'expired': h.clock.now += 121
    if problem == 'cancelled': h.core.cancel_diagnosis(run)
    if problem == 'quarantined': h.store.quarantine_target('lab-pi', 'test')
    if problem == 'wrong_plan': plan_id = 'wrong'
    with pytest.raises(ValueError): approve(h, run, plan_id)
    assert h.store.load_state(run)['sampling_plan']['approved'] is False


def test_expired_approval_after_durable_intent_never_reaches_transport(make_harness, monkeypatch):
    h = make_harness(); run, plan_id = planned(h); approve(h, run, plan_id)
    original = h.store.append_event
    def delayed(run_id, event):
        original(run_id, event)
        if event.get('tool') == 'sample_sensor_data': h.clock.now += 121
    monkeypatch.setattr(h.store, 'append_event', delayed)
    assert h.core.sample_sensor_data(run, plan_id, 'sample-1').status == 'blocked'
    assert h.transport.count('sample_sensor_data') == 0


def test_service_restart_returns_completed_sampling_cache_without_io(make_harness):
    from embedsleuth.core import DiagnosticCore
    h = make_harness(); run, plan_id = planned(h); approve(h, run, plan_id)
    result = h.core.sample_sensor_data(run, plan_id, 'sample-1')
    fresh = DiagnosticCore(h.registry, h.transport, h.store, clock=h.clock, utcnow=h.clock.utcnow)
    assert fresh.sample_sensor_data(run, plan_id, 'sample-1') == result
    assert h.transport.count('sample_sensor_data') == 1


def test_operator_store_open_does_not_recover_unrelated_diagnostics(make_harness, monkeypatch):
    from embedsleuth.storage import RunStore
    h = make_harness(); run, plan_id = planned(h)
    before = h.store.load_state(run)
    monkeypatch.setattr(RunStore, 'recover_pending', lambda *args: pytest.fail('unrelated recovery'))
    with RunStore.open_existing_writer(h.store.root) as operator_store:
        approve_plan(operator_store, h.registry, run, plan_id, now=h.clock.utcnow())
    after = h.store.load_state(run)
    assert after['sampling_plan']['approved'] is True
    before['sampling_plan'] = after['sampling_plan']
    assert after == before

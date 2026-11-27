import asyncio
import subprocess
import sys

from embedsleuth.mcp_server import MCPRuntime
from embedsleuth.sampling import approve_plan
from embedsleuth.tools import tool_input_schemas
from test_core import make_harness
from test_hooks import setup_hooks, hook, invoke
from test_sampling import observations


def test_sensor_plan_approval_sampling_report_and_hooks_share_one_audit(setup_hooks):
    h = setup_hooks

    async def check():
        runtime = MCPRuntime(h.core)
        try:
            start = await invoke(h, runtime, 'start_diagnosis', {
                'target_id': 'lab-pi', 'profile_id': 'mpu6050', 'mode': 'sensor_check'}, 'a')
            args = {'run_id': start['run_id']}
            await invoke(h, runtime, 'inspect_target', args, 'b')
            await invoke(h, runtime, 'inspect_i2c_environment', args, 'c')
            await invoke(h, runtime, 'read_device_identity', args | {
                'bus_ref': 'bus-1', 'address': 104, 'operation_id': 'identity-1'}, 'd')
            plan = await invoke(h, runtime, 'prepare_sensor_sampling', args | {'bus_ref': 'bus-1', 'address': 104}, 'e')
            assert plan['approval_required'] is True
            assert h.store.load_state(start['run_id'])['state'] == 'sampling_planned'
            assert hook(h, 'Stop') == {}  # Permit human approval across turns without finalizing the run.
            assert not any('approve' in name for name in tool_input_schemas())
            approve_plan(h.store, h.registry, start['run_id'], plan['plan_id'], now=h.clock.utcnow())
            h.transport.overrides['sample_sensor_data'] = observations(plan['plan_id'])
            data = await invoke(h, runtime, 'sample_sensor_data', args | {
                'plan_id': plan['plan_id'], 'operation_id': 'sample-1'}, 'f')
            assert data['observations']['restoration_confirmed'] is True
            report = await invoke(h, runtime, 'finalize_report', args, 'g')
            assert report['outcome'] == 'complete'
            assert hook(h, 'Stop') == {}
            assert h.transport.count('sample_sensor_data') == 1
        finally:
            await runtime.close()

    asyncio.run(check())


def test_waiting_plan_never_hides_pending_or_unknown_remote_work(setup_hooks):
    from test_sampling import planned
    h = setup_hooks
    run, plan_id = planned(h)
    assert hook(h, 'PreToolUse', 'inspect_i2c_environment', {'run_id': run}) == {}
    state = h.store.load_state(run)
    state['pending_operation'] = {'tool': 'sample_sensor_data', 'operation_id': 'unfinished'}
    h.store.save_state(run, state)
    assert hook(h, 'Stop').get('decision') == 'block'


def test_operator_cli_refuses_piped_approval_before_opening_storage(tmp_path):
    result = subprocess.run([sys.executable, '-I', '-m', 'embedsleuth.sampling_approval',
                             '--config', str(tmp_path / 'unused.json'), '--run-id', 'run-test',
                             '--plan-id', 'plan-test'], input='approve plan-test\n', text=True,
                            capture_output=True, timeout=5)
    assert result.returncode == 2 and 'Approval stopped' in result.stderr
    assert list(tmp_path.iterdir()) == []

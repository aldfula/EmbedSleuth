import json
import signal

import pytest

from embedsleuth import remote_probe as probe


def request(**updates):
    return {'op': 'sample_sensor_data', 'bus': 1, 'addresses': [104], 'address': 104,
            'expected_boot_id': 'boot', 'expected_adapter_fingerprint': 'adapter',
            'plan_id': 'sampling-' + 'a' * 64} | updates


class Sensor:
    def __init__(self):
        self.power = 64
        self.config = [0, 0, 0, 0]
        self.control = 0
        self.power2 = 0
        self.interrupts = [0, 0]
        self.calls = []
        self.fail_sample = False
        self.unknown = False
        self.fail_restore = False
        self.interrupt = False
        self.changed_power = False

    def transfer(self, command, timeout):
        assert 0 < timeout <= 7.5
        assert command[:3] == ['/usr/sbin/i2ctransfer', '-y', '1']
        assert '-f' not in command
        self.calls.append(command)
        reg = int(command[4], 16)
        if command[3].startswith('w2@'):
            assert reg == 0x6b  # No writes to any other register.
            value = int(command[5], 16)
            assert value in (0, 1, 2, 3, 64, 65, 66, 67)
            if value & 64 and self.fail_restore:
                raise probe.SamplingFailure('transfer_failed', 'restore failed')
            self.power = value
            return b''
        assert command[3] == 'w1@0x68'
        if reg == 0x75: values = [104]
        elif reg == 0x19: values = self.config
        elif reg == 0x37: values = self.interrupts
        elif reg == 0x6a: values = [self.control, self.power, self.power2]
        elif reg == 0x6b: values = [self.power]
        elif reg == 0x3b:
            if self.fail_sample:
                raise probe.SamplingFailure('remote_state_unknown' if self.unknown else 'transfer_failed', 'test error', self.unknown)
            if self.interrupt:
                # Between-transfer cancellation can restore a known completed write.
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            if self.changed_power: self.power = 8
            values = [0] * 14
        else: pytest.fail('unexpected register')
        assert command[-1] == 'r' + str(len(values))
        return ' '.join('0x%02x' % value for value in values).encode()


@pytest.fixture
def sensor(monkeypatch):
    device = Sensor()
    env = {'boot_id': 'boot', 'adapter_fingerprint': 'adapter', 'adapter_present': True,
           'access_allowed': True, 'driver_bound': {'104': False}, 'combined_transfer_supported': True}
    monkeypatch.setattr(probe, 'collect_environment', lambda *args: dict(env))
    monkeypatch.setattr(probe, 'find_i2ctransfer', lambda: '/usr/sbin/i2ctransfer')
    monkeypatch.setattr(probe, 'sensor_transfer', device.transfer)
    monkeypatch.setattr(probe.time, 'sleep', lambda _: None)
    return device


def writes(sensor):
    return [command for command in sensor.calls if command[3].startswith('w2@')]


@pytest.mark.parametrize('clock', [0, 1, 2, 3])
def test_sleeping_sensor_wakes_only_sleep_bit_and_restores(sensor, clock):
    sensor.power = 64 | clock
    result = probe.dispatch(request())
    assert result['status'] == 'ok'
    assert len(result['observations']['samples']) == 5
    assert result['observations']['restoration_confirmed'] is True
    assert sensor.power == 64 | clock
    assert [command[-1] for command in writes(sensor)] == ['0x%02x' % clock, '0x%02x' % (64 | clock)]


def test_awake_sensor_has_no_configuration_writes(sensor):
    sensor.power = 0
    result = probe.dispatch(request())
    assert result['status'] == 'ok' and writes(sensor) == []
    assert result['observations']['wakeup_attempted'] is False


@pytest.mark.parametrize('configuration', ['reset', 'cycle', 'temperature_disabled', 'standby', 'fifo', 'interrupt', 'read_to_clear', 'self_test', 'clock_stop'])
def test_unsupported_configuration_is_not_initialized_or_changed(sensor, configuration):
    if configuration == 'reset': sensor.power |= 0x80
    if configuration == 'cycle': sensor.power |= 0x20
    if configuration == 'temperature_disabled': sensor.power |= 8
    if configuration == 'clock_stop': sensor.power |= 7
    if configuration == 'standby': sensor.power2 = 1
    if configuration == 'fifo': sensor.control = 64
    if configuration == 'interrupt': sensor.interrupts[1] = 1
    if configuration == 'read_to_clear': sensor.interrupts[0] = 16
    if configuration == 'self_test': sensor.config[2] = 128
    result = probe.dispatch(request())
    assert result['status'] == 'blocked' and writes(sensor) == []


def test_known_read_failure_restores_then_reports_error_without_retry(sensor):
    sensor.fail_sample = True
    result = probe.dispatch(request())
    assert result['error_code'] == 'transfer_failed'
    assert sensor.power == 64 and result['observations']['restoration_confirmed'] is True
    assert sum(command[4] == '0x3b' for command in sensor.calls) == 1


def test_unknown_io_never_starts_a_second_transfer_for_restoration(sensor):
    sensor.fail_sample = sensor.unknown = True
    result = probe.dispatch(request())
    assert result['remote_state_unknown'] is True
    assert result['observations']['restoration_confirmed'] is False
    assert len(writes(sensor)) == 1
    assert sensor.calls[-1][4] == '0x3b'


def test_restoration_failure_is_explicitly_unknown(sensor):
    sensor.fail_restore = True
    result = probe.dispatch(request())
    assert result['remote_state_unknown'] and result['observations']['restoration_confirmed'] is False


def test_changed_power_is_never_overwritten_with_stale_original(sensor):
    sensor.changed_power = True
    result = probe.dispatch(request())
    assert result['remote_state_unknown'] and sensor.power == 8
    assert len(writes(sensor)) == 1


def test_graceful_signal_between_transfers_restores_and_preserves_error(sensor):
    sensor.interrupt = True
    result = probe.dispatch(request())
    assert result['error_code'] == 'policy_denied' and result['observations']['restoration_confirmed']
    assert sensor.power == 64


@pytest.mark.parametrize('updates', [{'register': 1}, {'approved': True}, {'plan_id': '../bad'}, {'address': 32}, {'count': 1000000}])
def test_wire_fields_cannot_expand_fixed_sampling_procedure(updates):
    with pytest.raises(ValueError): probe.validate_request(request(**updates))

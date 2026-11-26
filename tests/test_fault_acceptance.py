"""Fault preparation safety and grading use local files only; never sudo."""

import importlib.util
from pathlib import Path
import stat
import sys
import json
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load("fault_guard", "evals/diagnose-mpu6050/fault_guard.py")
runner = load("fault_runner", "scripts/run_fault_acceptance.py")


@pytest.mark.parametrize("case,facts,expected", [
    ("permission", {"bus": 1, "adapter_present": True, "access_allowed": False}, True),
    ("permission", {"bus": 1, "adapter_present": True, "access_allowed": None}, False),
    ("permission", {"bus": True, "adapter_present": True, "access_allowed": False}, False),
    ("permission", {"bus": 1, "adapter_present": False, "access_allowed": False}, False),
    ("driver-bound", {"bus": 1, "adapter_present": True, "driver_bound": {"104": True}}, True),
    ("driver-bound", {"bus": 1, "adapter_present": True, "driver_bound": {"104": "true"}}, False),
    ("adapter-disabled", {"bus": 1, "adapter_present": False}, True),
    ("adapter-disabled", {"bus": 255, "adapter_present": False}, False),
    ("adapter-disabled", {"bus": 1}, False),
    ("other", {"bus": 1}, False),
])
def test_fault_matching_preserves_unknowns_and_actual_registered_bus(case, facts, expected):
    assert runner.expected_fault(case, facts) is expected


def test_restore_comparison_requires_complete_same_boot_adapter_and_flags():
    before = {"bus": 1, "boot_id": "boot", "adapter_fingerprint": "adapter", "adapter_present": True,
              "access_allowed": True, "combined_transfer_supported": True, "dependency_available": True,
              "driver_bound": {"104": False, "105": False}}
    assert runner.restored_environment(before, before.copy())
    assert not runner.restored_environment(before, before | {"boot_id": "other"})
    assert not runner.restored_environment({}, {})
    assert not runner.restored_environment(before, {key: value for key, value in before.items() if key != "driver_bound"})


def test_permission_restore_uses_original_metadata_and_does_not_guess(tmp_path, monkeypatch):
    device = tmp_path / "device"
    device.touch(mode=0o640)
    monkeypatch.setattr(guard, "DEVICE", device)
    monkeypatch.setattr(guard, "acl_bytes", lambda _: None)
    instance = guard.Guard("permission")
    instance.saved = {"identity": list(guard.device_identity(device.stat())), "mode": 0o640, "acl": None}
    instance.changed = True
    device.chmod(0)
    instance.restore()
    assert stat.S_IMODE(device.stat().st_mode) == 0o640 and instance.changed is False


def test_permission_restore_refuses_a_replaced_node(tmp_path, monkeypatch):
    device = tmp_path / "device"
    device.touch(mode=0o600)
    monkeypatch.setattr(guard, "DEVICE", device)
    instance = guard.Guard("permission")
    identity = list(guard.device_identity(device.stat()))
    identity[1] += 1
    instance.saved = {"identity": identity, "mode": 0o640, "acl": None}
    instance.changed = True
    with pytest.raises(ValueError, match="device changed"):
        instance.restore()
    assert stat.S_IMODE(device.stat().st_mode) == 0o600


def test_permission_setup_rejects_regular_files_before_mutation(tmp_path, monkeypatch):
    device = tmp_path / "file"
    device.touch(mode=0o600)
    monkeypatch.setattr(guard, "DEVICE", device)
    instance = guard.Guard("permission")
    with pytest.raises(ValueError, match="unexpected device"):
        instance.prepare()
    assert instance.changed is False and stat.S_IMODE(device.stat().st_mode) == 0o600


def test_preexisting_i2c_client_is_never_deleted(tmp_path, monkeypatch):
    client = tmp_path / "existing-client"
    client.mkdir()
    monkeypatch.setattr(guard, "CLIENT", client)
    instance = guard.Guard("driver-bound")
    with pytest.raises(ValueError, match="existing client"):
        instance.prepare()
    instance.restore()
    assert client.is_dir()


def test_dummy_cleanup_refuses_a_foreign_driver(tmp_path, monkeypatch):
    client, foreign, dummy = tmp_path / "client", tmp_path / "foreign", tmp_path / "dummy"
    for path in (client, foreign, dummy):
        path.mkdir()
    (client / "driver").symlink_to(foreign, target_is_directory=True)
    monkeypatch.setattr(guard, "CLIENT", client)
    monkeypatch.setattr(guard, "DUMMY", dummy)
    instance = guard.Guard("driver-bound")
    instance.changed = True
    with pytest.raises(ValueError, match="client changed"):
        instance.restore()
    assert client.exists()


def test_root_helper_refuses_non_root_before_opening_system_paths(monkeypatch):
    monkeypatch.setattr(guard.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(sys, "argv", ["fault_guard", "permission", "1000", "a" * 32, "boot", "adapter"])
    with pytest.raises(SystemExit, match="interactive sudo"):
        guard.main()


def test_controller_requires_owner_terminal_before_creating_output(tmp_path, monkeypatch):
    output = tmp_path / "never-created.json"
    monkeypatch.setattr(runner.sys, "argv", ["runner", "--config", str(tmp_path / "missing"),
        "--target", "lab-pi", "--case", "all", "--output", str(output),
        "--confirm-changes", "--confirm-exclusive-i2c"])
    monkeypatch.setattr(runner.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit):
        runner.main()
    assert not output.exists()


def test_dummy_cleanup_refuses_a_replaced_client_even_with_same_driver(tmp_path, monkeypatch):
    client, dummy = tmp_path / "client", tmp_path / "dummy"
    client.mkdir()
    dummy.mkdir()
    (client / "driver").symlink_to(dummy, target_is_directory=True)
    monkeypatch.setattr(guard, "CLIENT", client)
    monkeypatch.setattr(guard, "DUMMY", dummy)
    instance = guard.Guard("driver-bound")
    instance.changed = True
    instance.saved["client_inode"] = client.stat().st_ino + 1
    with pytest.raises(ValueError, match="client changed"):
        instance.restore()
    assert client.is_dir()


def test_atomic_status_does_not_follow_an_existing_symlink(tmp_path):
    secret, target = tmp_path / "untouched", tmp_path / "status.json"
    secret.write_text("unchanged")
    target.symlink_to(secret)
    guard.private_json(target, {"phase": "restored"}, guard.os.getuid())
    assert secret.read_text() == "unchanged"
    assert not target.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.fixture
def modern_sysfs(tmp_path, monkeypatch):
    devices = tmp_path / "sys/devices"
    drivers = tmp_path / "sys/bus/platform/drivers"
    driver = drivers / "i2c-bcm2835"
    driver.mkdir(parents=True)
    for name in ("bind", "unbind"):
        (driver / name).touch()
    controller = devices / "platform/soc/3f804000.i2c"
    leaf = controller / "i2c-1/i2c-dev/i2c-1"
    leaf.mkdir(parents=True)
    (leaf / "device").symlink_to(controller / "i2c-1", target_is_directory=True)
    (controller / "driver").symlink_to(driver, target_is_directory=True)
    link = tmp_path / "sys/class/i2c-dev/i2c-1"
    link.parent.mkdir(parents=True)
    link.symlink_to(leaf, target_is_directory=True)
    monkeypatch.setattr(guard, "ADAPTER", link)
    monkeypatch.setattr(guard, "PLATFORM_DRIVERS", drivers)
    monkeypatch.setattr(guard, "SYSTEM_DEVICES", devices)
    return SimpleNamespace(driver=driver, controller=controller, link=link, leaf=leaf)


def test_adapter_prepare_supports_i2c_dev_without_legacy_adapter_class(modern_sysfs):
    instance = guard.Guard("adapter-disabled")
    instance.prepare()
    assert instance.saved == {"controller": str(modern_sysfs.controller), "driver": str(modern_sysfs.driver)}
    assert not modern_sysfs.link.parent.parent.joinpath("i2c-adapter").exists()
    assert instance.changed is False


def test_modern_adapter_unbind_and_same_controller_restore(modern_sysfs, monkeypatch):
    f = modern_sysfs
    write_text = Path.write_text
    writes = []

    def kernel_write(path, text, *args, **kwargs):
        writes.append((path.name, text))
        if path == f.driver / "unbind":
            f.link.unlink()
            (f.controller / "driver").unlink()
        elif path == f.driver / "bind":
            f.link.symlink_to(f.leaf, target_is_directory=True)
            (f.controller / "driver").symlink_to(f.driver, target_is_directory=True)
        return write_text(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", kernel_write)
    instance = guard.Guard("adapter-disabled")
    instance.prepare()
    instance.apply()
    assert not f.link.exists()
    instance.restore()
    assert f.link.exists() and instance.changed is False
    assert writes == [("unbind", "3f804000.i2c"), ("bind", "3f804000.i2c")]


def test_modern_adapter_does_not_relax_driver_allowlist(modern_sysfs):
    f = modern_sysfs
    foreign = f.driver.parent / "other-controller"
    foreign.mkdir()
    (f.controller / "driver").unlink()
    (f.controller / "driver").symlink_to(foreign, target_is_directory=True)
    instance = guard.Guard("adapter-disabled")
    with pytest.raises(ValueError, match="restoration unavailable"):
        instance.prepare()
    assert instance.changed is False
    assert (f.driver / "unbind").read_text() == ""


def test_failed_preparation_preserves_case_and_administrator_status(tmp_path, monkeypatch):
    output = tmp_path / "result.json"
    monkeypatch.setattr(runner.sys, "argv", ["runner", "--config", str(tmp_path / "unused"),
        "--target", "lab-pi", "--case", "adapter-disabled", "--output", str(output),
        "--confirm-changes", "--confirm-exclusive-i2c"])
    monkeypatch.setattr(runner.sys.stdin, "isatty", lambda: True)
    status = {"case": "adapter-disabled", "phase": "restored", "test_setup_failed": True,
              "error": {"stage": "prepare", "kind": "FileNotFoundError", "errno": 2}}

    def unavailable(*_):
        raise runner.FaultStatusError(status)
    monkeypatch.setattr(runner, "one_case", unavailable)
    assert runner.main() == 1
    saved = json.loads(output.read_text())["results"][0]
    assert saved["case"] == "adapter-disabled" and saved["assessment"] == "unavailable"
    assert saved["administrator_status"] == status

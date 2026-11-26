"""Boot preparation safety tests use temporary local files and never sudo."""

import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


guard = load("memory_guard", "evals/diagnose-mpu6050/memory_boot_guard.py")
runner = load("memory_preparation", "scripts/prepare_memory_acceptance.py")


@pytest.mark.parametrize("data", [b"", b" \n", b"a\nb\n", b"a\x00b", b"a" * 16385,
                                  b"root=x cgroup_disable=memory\n", b"root=x cgroup_enable=cpu\n",
                                  b"root=x cgroup_memory=1\n", b"root=x cgroup_enable=memory\n"])
def test_ambiguous_or_already_configured_boot_options_require_review(data):
    with pytest.raises(ValueError):
        guard.proposed(data)


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_only_one_option_is_added_without_changing_existing_arguments(ending):
    data = b"console=tty1 root=PARTUUID=test rootwait" + ending
    assert guard.proposed(data) == data.rstrip(b"\r\n") + b" cgroup_enable=memory" + ending


@pytest.fixture
def boot(tmp_path, monkeypatch):
    cmdline = tmp_path / "cmdline.txt"
    data = b"console=tty1 root=PARTUUID=test rootwait\n"
    cmdline.write_bytes(data)
    backup = tmp_path / "original.bak"
    plan = {"before_sha256": guard.digest(data), "after_sha256": guard.digest(guard.proposed(data)),
            "boot_id": "old-boot", "backup": str(backup)}
    monkeypatch.setattr(guard, "BOOT", tmp_path)
    monkeypatch.setattr(guard, "CMDLINE", cmdline)
    monkeypatch.setattr(guard, "preflight", lambda: plan.copy())
    monkeypatch.setattr(guard, "regular", lambda path: path.lstat())
    monkeypatch.setattr(guard.os, "sync", lambda: None)
    return cmdline, backup, data, plan


def test_backup_is_exact_and_survives_the_verified_edit(boot):
    cmdline, backup, original, plan = boot
    assert guard.apply(plan["before_sha256"], plan["boot_id"]) == plan
    assert backup.read_bytes() == original
    assert cmdline.read_bytes() == guard.proposed(original)
    assert not list(cmdline.parent.glob(".embedsleuth-cmdline-*"))


@pytest.mark.parametrize("hash_value,boot_value", [("wrong", "old-boot"), (None, "new-boot")])
def test_changed_hash_or_boot_prevents_any_write(boot, hash_value, boot_value):
    cmdline, backup, original, plan = boot
    with pytest.raises(ValueError):
        guard.apply(hash_value or plan["before_sha256"], boot_value)
    assert cmdline.read_bytes() == original and not backup.exists()


def test_existing_different_backup_is_not_overwritten(boot):
    cmdline, backup, original, plan = boot
    backup.write_bytes(b"other backup")
    with pytest.raises(ValueError, match="backup conflict"):
        guard.apply(plan["before_sha256"], plan["boot_id"])
    assert backup.read_bytes() == b"other backup" and cmdline.read_bytes() == original


def test_failed_staged_write_preserves_active_boot_file_and_backup(boot, monkeypatch):
    cmdline, backup, original, plan = boot
    write = guard.exclusive_copy

    def fail_staged(path, data):
        if path != backup:
            raise OSError("simulated full disk")
        write(path, data)

    monkeypatch.setattr(guard, "exclusive_copy", fail_staged)
    with pytest.raises(OSError):
        guard.apply(plan["before_sha256"], plan["boot_id"])
    assert backup.read_bytes() == cmdline.read_bytes() == original


def test_symlink_is_rejected_before_following_the_boot_file(tmp_path):
    actual = tmp_path / "actual"
    actual.write_bytes(b"unchanged")
    link = tmp_path / "cmdline"
    link.symlink_to(actual)
    with pytest.raises(ValueError):
        guard.regular(link)
    assert actual.read_bytes() == b"unchanged"


def test_no_owner_terminal_means_no_remote_action_or_output_file(tmp_path, monkeypatch):
    output = tmp_path / "never-created.json"
    monkeypatch.setattr(sys, "argv", ["prepare", "--config", "/missing", "--target", "lab-pi",
        "--output", str(output), "--confirm-boot-change-and-reboot"])
    monkeypatch.setattr(runner.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="own terminal"):
        runner.main()
    assert not output.exists()


@pytest.mark.parametrize("change", ["same_boot", "wrong_file", "wrong_backup", "no_memory"])
def test_reboot_alone_is_not_success_without_matching_backup_and_accounting(change):
    plan = {"boot_id": "old", "before_sha256": "before", "after_sha256": "after"}
    after = {"boot_id": "new", "cmdline_sha256": "after", "backup_sha256": "before",
             "controllers": ["memory"]}
    assert runner.verify(plan, after)
    if change == "same_boot": after["boot_id"] = "old"
    if change == "wrong_file": after["cmdline_sha256"] = "other"
    if change == "wrong_backup": after["backup_sha256"] = "other"
    if change == "no_memory": after["controllers"] = ["cpu"]
    assert not runner.verify(plan, after)

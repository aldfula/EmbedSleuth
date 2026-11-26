"""Deployment setup must never overwrite registration, evidence, or host trust."""

import json
from pathlib import Path
import shlex
import sys
import tomllib

import pytest

from embedsleuth.config import load_configuration
from embedsleuth.storage import RunStore
from embedsleuth import workspace


def arguments(tmp_path):
    return {"directory": tmp_path / "workspace", "config_path": tmp_path / "private/targets.json",
            "init_config": True, "state_directory": tmp_path / "state"}


def test_first_install_contains_complete_skill_and_safe_host_settings(tmp_path):
    args = arguments(tmp_path)
    result = workspace.prepare(**args)
    config = load_configuration(args["config_path"])
    assert config.targets == () and config.state_directory == args["state_directory"]
    assert result["hook_trust_granted"] is False and result["model_turns"] == result["device_checks"] == 0
    root = args["directory"]
    skill = root / ".agents/skills/diagnose-mpu6050"
    assert all((skill / name).is_file() for name in workspace.SKILL_FILES)
    mcp = tomllib.loads((root / ".codex/config.toml").read_text())["mcp_servers"]["embedsleuth"]
    assert mcp["command"] == sys.executable
    assert mcp["args"] == ["-I", "-m", "embedsleuth.mcp_server", "--config", str(args["config_path"])]
    assert set(mcp) == {"command", "args", "startup_timeout_sec", "tool_timeout_sec"}
    hooks = json.loads((root / ".codex/hooks.json").read_text())["hooks"]
    assert set(hooks) == {"PreToolUse", "PostToolUse", "Stop"}
    for event, groups in hooks.items():
        command = shlex.split(groups[0]["hooks"][0]["command"])
        assert command[:4] == [sys.executable, "-I", "-m", "embedsleuth.hooks"]
        assert command[command.index("--config") + 1] == str(args["config_path"])
        assert command[command.index("--event") + 1] == event
    assert root.stat().st_mode & 0o777 == 0o700
    assert args["config_path"].stat().st_mode & 0o777 == 0o600
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in root.rglob("*") if p.is_file())
    assert not (root / ".embedsleuth-hook-state").exists()


def test_new_workspace_reuses_existing_quarantined_store_without_recovery(tmp_path):
    config = tmp_path / "targets.json"
    state = tmp_path / "state"
    with RunStore(state) as store:
        store.quarantine_target("pi-1", "Remote operation completion is uncertain")
    config.write_text(json.dumps({"schema_version": "1.0", "state_directory": str(state), "targets": []}))
    config.chmod(0o600)
    before = {p: p.read_bytes() for p in state.rglob("*") if p.is_file()}
    saved = config.read_bytes()
    result = workspace.prepare(tmp_path / "workspace", config)
    assert result["new_empty_registration"] is False
    assert config.read_bytes() == saved
    assert before == {p: p.read_bytes() for p in state.rglob("*") if p.is_file()}
    with RunStore.open_reader(state) as reader:
        assert reader.is_quarantined("pi-1")


@pytest.mark.parametrize("kind", ["workspace", "config", "state", "symlink", "parent_symlink",
                                  "relative", "traversal", "config_inside_workspace", "state_inside_workspace",
                                  "workspace_inside_state", "missing_state", "config_inside_state", "state_inside_config"])
def test_unsafe_or_existing_destinations_fail_before_writing(tmp_path, kind):
    args = arguments(tmp_path)
    marker = tmp_path / "sentinel"
    marker.write_text("untouched")
    if kind == "workspace": args["directory"].mkdir()
    if kind == "config":
        args["config_path"].parent.mkdir(); args["config_path"].write_text("original")
    if kind == "state": args["state_directory"].mkdir()
    if kind == "symlink": args["directory"].symlink_to(marker)
    if kind == "parent_symlink":
        link = tmp_path / "link";link.symlink_to(tmp_path, target_is_directory=True)
        args["directory"] = link / "workspace"
    if kind == "relative": args["directory"] = Path("relative")
    if kind == "traversal": args["directory"] = tmp_path / ".." / "escape"
    if kind == "config_inside_workspace": args["config_path"] = args["directory"] / "targets.json"
    if kind == "state_inside_workspace": args["state_directory"] = args["directory"] / "state"
    if kind == "workspace_inside_state": args["directory"] = args["state_directory"] / "workspace"
    if kind == "missing_state": args["state_directory"] = None
    if kind == "config_inside_state": args["config_path"] = args["state_directory"] / "targets.json"
    if kind == "state_inside_config": args["state_directory"] = args["config_path"] / "state"
    with pytest.raises(ValueError): workspace.prepare(**args)
    assert marker.read_text() == "untouched"
    if kind == "config": assert args["config_path"].read_text() == "original"
    if kind != "workspace": assert not (tmp_path / "workspace/.codex").exists()


def test_existing_registration_cannot_be_repointed_to_another_store(tmp_path):
    with pytest.raises(ValueError, match="original state"):
        workspace.prepare(tmp_path / "workspace", tmp_path / "config", state_directory=tmp_path / "new-state")
    assert not (tmp_path / "workspace").exists()


def test_installation_failure_removes_only_new_outputs(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    original = workspace.write_new
    def fail(path, content):
        if path.name == "README.md": raise OSError("simulated write failure")
        return original(path, content)
    monkeypatch.setattr(workspace, "write_new", fail)
    with pytest.raises(OSError): workspace.prepare(**args)
    assert not args["directory"].exists() and not args["config_path"].exists() and not args["state_directory"].exists()


def test_python_and_config_paths_with_spaces_and_quotes_are_not_shell_instructions(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    args["config_path"] = tmp_path / "private space's/targets.json"
    executable = str(tmp_path / "venv space's/bin/python")
    monkeypatch.setattr(sys, "executable", executable)
    workspace.prepare(**args)
    host = tomllib.loads((args["directory"] / ".codex/config.toml").read_text())
    assert host["mcp_servers"]["embedsleuth"]["command"] == executable
    hooks = json.loads((args["directory"] / ".codex/hooks.json").read_text())["hooks"]
    for groups in hooks.values():
        command = shlex.split(groups[0]["hooks"][0]["command"])
        assert command[0] == executable and command[command.index("--config")+1] == str(args["config_path"])


def test_other_operating_systems_are_rejected_without_output(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ValueError, match="Linux"): workspace.prepare(**arguments(tmp_path))
    assert not (tmp_path / "workspace").exists()

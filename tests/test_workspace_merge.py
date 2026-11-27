import json
from pathlib import Path
import sys
import tomllib

import pytest

from embedsleuth.workspace_merge import apply_existing
from embedsleuth import workspace_merge as merge


@pytest.fixture
def existing(tmp_path):
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / ".codex").mkdir()
    (directory / "README.md").write_text("My project\n")
    (directory / "main.py").write_text("print('original')\n")
    (directory / ".gitignore").write_text("local.txt\n")
    (directory / ".codex/config.toml").write_text('# Keep comment\nmodel = "custom"\n[mcp_servers.other]\ncommand="other"\n')
    (directory / ".codex/hooks.json").write_text(json.dumps({"custom": True, "hooks": {
        "PreToolUse": [{"matcher": "other", "hooks": [{"type": "command", "command": "other-check"}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": "hello"}]}]}}))
    config = tmp_path / "targets.json"
    config.write_text(json.dumps({"schema_version": "1.0", "state_directory": str(tmp_path / "evidence"), "targets": []}))
    config.chmod(0o600)
    for path in directory.rglob('*'):
        if path.is_file():
            path.chmod(0o644)
    return directory, config


def snapshot(directory):
    return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}


def test_in_place_preserves_existing_project_and_other_host_entries(existing):
    directory, config = existing
    before = snapshot(directory)
    original_config = config.read_bytes()
    result = apply_existing(directory, config)
    assert result['device_checks'] == result['model_turns'] == 0 and not result['hook_trust_granted']
    for name in ('README.md', 'main.py'):
        assert (directory / name).read_bytes() == before[name]
    assert (directory / '.codex/config.toml').read_bytes().startswith(before['.codex/config.toml'])
    host = tomllib.loads((directory / '.codex/config.toml').read_text())
    assert host['model'] == 'custom' and host['mcp_servers']['other']['command'] == 'other'
    assert host['mcp_servers']['embedsleuth']['command'] == sys.executable
    hooks = json.loads((directory / '.codex/hooks.json').read_text())
    assert hooks['custom'] is True and hooks['hooks']['PreToolUse'][0]['matcher'] == 'other'
    assert len(hooks['hooks']['PreToolUse']) == 2 and 'SessionStart' in hooks['hooks']
    assert config.read_bytes() == original_config
    assert not (config.parent / 'evidence').exists()
    assert not (directory / '.embedsleuth-hook-state').exists()
    backup = Path(result['backup'])
    assert (backup / '.codex/config.toml').read_bytes() == before['.codex/config.toml']
    assert not (directory / '.embedsleuth/pending.json').exists()


def test_dry_run_has_no_side_effects_and_repeated_apply_is_noop(existing):
    directory, config = existing
    before = snapshot(directory)
    assert apply_existing(directory, config, dry_run=True)['changed_files']
    assert snapshot(directory) == before and not (directory / '.embedsleuth').exists()
    apply_existing(directory, config)
    after = snapshot(directory)
    assert apply_existing(directory, config)['changed_files'] == []
    assert snapshot(directory) == after


@pytest.mark.parametrize('problem', ['mcp_conflict', 'bad_toml', 'inline_table', 'bad_json', 'duplicate_json', 'hook_conflict', 'skill', 'symlink', 'pending'])
def test_conflicts_reject_before_mutation(existing, problem):
    directory, config = existing
    if problem == 'mcp_conflict':
        (directory / '.codex/config.toml').write_text('[mcp_servers.embedsleuth]\ncommand="someone"\n')
    if problem == 'bad_toml': (directory / '.codex/config.toml').write_text('broken = [')
    if problem == 'inline_table': (directory / '.codex/config.toml').write_text('mcp_servers = { other = {command="other"} }')
    if problem == 'bad_json': (directory / '.codex/hooks.json').write_text('[')
    if problem == 'duplicate_json': (directory / '.codex/hooks.json').write_text('{"hooks":{},"hooks":{}}')
    if problem == 'hook_conflict':
        (directory / '.codex/hooks.json').write_text('{"hooks":{"Stop":[{"hooks":[{"command":"old-embedsleuth"}]}]}}')
    if problem == 'skill':
        skill = directory / '.agents/skills/diagnose-mpu6050'
        skill.mkdir(parents=True); (skill / 'SKILL.md').write_text('custom skill')
    if problem == 'symlink':
        (directory / '.agents').symlink_to(config.parent / 'external')
    if problem == 'pending':
        (directory / '.embedsleuth').mkdir(); (directory / '.embedsleuth/pending.json').write_text('{}')
    before = snapshot(directory)
    with pytest.raises((ValueError, OSError)):
        apply_existing(directory, config)
    assert snapshot(directory) == before


def test_failed_apply_rolls_back_own_writes_without_touching_project(existing, monkeypatch):
    directory, config = existing
    before = snapshot(directory)
    original = merge.replace_checked
    def fail(path, old, new):
        if path.name == 'SKILL.md': raise OSError('simulated disk failure')
        return original(path, old, new)
    monkeypatch.setattr(merge, 'replace_checked', fail)
    with pytest.raises(OSError): apply_existing(directory, config)
    after = snapshot(directory)
    assert {k: after[k] for k in before} == before
    assert not (directory / '.embedsleuth/pending.json').exists()
    assert not (directory / '.agents/skills/diagnose-mpu6050/SKILL.md').exists()


def test_modified_installed_skill_is_never_overwritten(existing):
    directory, config = existing
    apply_existing(directory, config)
    skill = directory / '.agents/skills/diagnose-mpu6050/SKILL.md'
    skill.write_text('local customization')
    before = snapshot(directory)
    with pytest.raises(ValueError): apply_existing(directory, config)
    assert snapshot(directory) == before


def test_first_install_can_create_external_registration_in_existing_project(existing, tmp_path):
    directory, _ = existing
    config, state = tmp_path / 'new-private/targets.json', tmp_path / 'new-evidence'
    before = snapshot(directory)
    kwargs = {'init_config': True, 'state_directory': state}
    assert apply_existing(directory, config, dry_run=True, **kwargs)['new_empty_registration']
    assert not config.exists() and not state.exists() and snapshot(directory) == before
    apply_existing(directory, config, **kwargs)
    assert json.loads(config.read_text())['targets'] == [] and state.is_dir()
    assert config.stat().st_mode & 0o777 == 0o600
    assert apply_existing(directory, config)['changed_files'] == []


def test_failed_first_install_does_not_leave_a_new_registration(existing, tmp_path, monkeypatch):
    directory, _ = existing
    config, state = tmp_path / 'new-private/targets.json', tmp_path / 'new-evidence'
    monkeypatch.setattr(merge, 'replace_checked', lambda *args: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError): apply_existing(directory, config, init_config=True, state_directory=state)
    assert not config.exists() and not state.exists()


def test_legacy_upgrade_accepts_only_exact_known_original_skill(existing, monkeypatch):
    import hashlib
    directory, config = existing
    apply_existing(directory, config)
    (directory / '.embedsleuth/installation.json').unlink()
    skill = directory / '.agents/skills/diagnose-mpu6050/SKILL.md'
    skill.write_text('original published legacy skill')
    marker = {'version': '0.1.0', 'config': str(config), 'state_directory': str(config.parent / 'evidence'),
              'python': sys.executable, 'hook_trust_granted': False}
    (directory / 'INSTALLATION.json').write_text(json.dumps(marker))
    (directory / 'INSTALLATION.json').chmod(0o600)
    hashes = {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (directory / '.agents').rglob('*') if p.is_file()}
    monkeypatch.setattr(merge, 'legacy_owned', lambda: hashes)
    assert '.agents/skills/diagnose-mpu6050/SKILL.md' in apply_existing(directory, config)['changed_files']
    assert 'original published legacy skill' not in skill.read_text()

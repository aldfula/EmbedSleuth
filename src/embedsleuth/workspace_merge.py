"""Conservative in-place setup: merge owned entries, back up, never grant trust."""

import fcntl
from importlib.resources import files
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tomllib
from uuid import uuid4
from types import SimpleNamespace

from embedsleuth import __version__
from embedsleuth.config import load_configuration
from embedsleuth.hooks import codex_hook_definitions
from embedsleuth.workspace import SKILL_FILES, absolute, write_new


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_existing(path):
    absolute(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or (info.st_mode & 0o022 and path.name != ".gitignore")
                or info.st_size > 1024 * 1024):
            raise ValueError("Unsafe workspace file")
        return stream.read(1024 * 1024 + 1)


def decode_json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    value = json.loads(data, object_pairs_hook=pairs)
    if type(value) is not dict:
        raise ValueError("Expected a JSON object")
    return value


def legacy_owned():
    return json.loads(files("embedsleuth").joinpath("data/setup-legacy-0.1.0.json").read_text())["files"]


def plan(directory, config_path, *, new_state=None):
    """Read configuration and produce byte changes without creating anything."""
    if sys.platform != "linux":
        raise ValueError("This release supports Linux development PCs")
    directory, config_path = absolute(directory), absolute(config_path)
    if not directory.is_dir() or config_path.is_relative_to(directory):
        raise ValueError("Use an existing workspace and an external registration")
    if new_state is not None:
        absolute(new_state)
        if (config_path.exists() or new_state.exists() or new_state == config_path
                or new_state in config_path.parents or config_path in new_state.parents):
            raise ValueError("New registration requires separate unused configuration and evidence paths")
        config = SimpleNamespace(state_directory=new_state)
    else:
        config = load_configuration(config_path)
    if config.state_directory.is_relative_to(directory) or directory.is_relative_to(config.state_directory):
        raise ValueError("Keep evidence outside the workspace")
    absolute(config.state_directory)
    metadata_path = directory / ".embedsleuth/installation.json"
    if read_existing(directory / ".embedsleuth/pending.json") is not None:
        raise ValueError("Interrupted setup requires reviewing its backups before reapplying")
    old_metadata = read_existing(metadata_path)
    previous = decode_json(old_metadata) if old_metadata else {}
    owned = previous.get("files", {})
    if old_metadata is None:
        legacy = read_existing(directory / "INSTALLATION.json")
        if legacy is not None:
            marker = decode_json(legacy)
            if (marker.get("version") == "0.1.0" and marker.get("config") == str(config_path)
                    and marker.get("state_directory") == str(config.state_directory)
                    and marker.get("python") == sys.executable and marker.get("hook_trust_granted") is False):
                owned = legacy_owned()
    if type(owned) is not dict:
        raise ValueError("Invalid installation metadata")
    changes = {}

    def change(name, content, *, managed=False):
        before = read_existing(directory / name)
        if before == content:
            return
        if managed and before is not None and owned.get(name) != digest(before):
            raise ValueError("Existing skill or installation metadata conflicts with setup")
        changes[name] = (before, content)

    mcp_path = directory / ".codex/config.toml"
    before = read_existing(mcp_path)
    raw = before or b""
    parsed = tomllib.loads(raw.decode())
    desired = {"command": sys.executable,
               "args": ["-I", "-m", "embedsleuth.mcp_server", "--config", str(config_path)],
               "startup_timeout_sec": 20, "tool_timeout_sec": 20}
    servers = parsed.get("mcp_servers", {})
    if type(servers) is not dict:
        raise ValueError("Conflicting MCP configuration")
    if "embedsleuth" in servers and servers["embedsleuth"] != desired:
        raise ValueError("Existing EmbedSleuth MCP entry conflicts; review it manually")
    if "embedsleuth" not in servers:
        block = "\n[mcp_servers.embedsleuth]\n" + "\n".join(
            f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in desired.items()) + "\n"
        merged = raw + block.encode()
        if tomllib.loads(merged.decode())["mcp_servers"]["embedsleuth"] != desired:
            raise ValueError("Could not merge MCP settings")
        change(".codex/config.toml", merged)

    hooks_before = read_existing(directory / ".codex/hooks.json")
    hooks = decode_json(hooks_before) if hooks_before else {}
    groups = hooks.setdefault("hooks", {})
    if type(groups) is not dict:
        raise ValueError("Invalid hook configuration")
    expected_hooks = codex_hook_definitions(Path(sys.executable), config_path,
                                          directory / ".embedsleuth-hook-state")["hooks"]
    modified = False
    for event, additions in expected_hooks.items():
        entries = groups.setdefault(event, [])
        if type(entries) is not list:
            raise ValueError("Invalid hook event")
        for entry in entries:
            if type(entry) is not dict or type(entry.get("hooks")) is not list:
                raise ValueError("Invalid hook group")
            for hook in entry["hooks"]:
                if type(hook) is not dict:
                    raise ValueError("Invalid hook definition")
                if "embedsleuth" in str(hook.get("command", "")).lower() and entry not in additions:
                    raise ValueError("Existing EmbedSleuth hook conflicts; review it manually")
        for addition in additions:
            if addition not in entries:
                entries.append(addition)
                modified = True
    if modified:
        change(".codex/hooks.json", (json.dumps(hooks, ensure_ascii=False, indent=2) + "\n").encode())

    resources = files("embedsleuth").joinpath("data/skills/diagnose-mpu6050")
    desired_files = {}
    for name in SKILL_FILES:
        relative = ".agents/skills/diagnose-mpu6050/" + name
        content = resources.joinpath(name).read_bytes()
        change(relative, content, managed=True)
        desired_files[relative] = digest(content)
    ignored = (read_existing(directory / ".gitignore") or b"")
    lines = ignored.decode().splitlines()
    extra = [item for item in ("/.codex/config.toml", "/.codex/hooks.json", "/.embedsleuth/",
                               "/.embedsleuth-hook-state/") if item not in lines]
    if extra:
        change(".gitignore", ignored + (b"\n" if ignored and not ignored.endswith(b"\n") else b"")
               + ("\n".join(extra) + "\n").encode())
    metadata = {"version": __version__, "config": str(config_path), "python": sys.executable,
                "files": desired_files, "hook_trust_granted": False}
    change(".embedsleuth/installation.json", (json.dumps(metadata, indent=2) + "\n").encode())
    return changes


def replace_checked(path, before, after):
    if read_existing(path) != before:
        raise ValueError("Workspace changed during setup; review backups")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    absolute(path)
    temporary = path.parent / (".embedsleuth-" + uuid4().hex)
    mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) if before is not None else 0o600
    try:
        write_new(temporary, after)
        temporary.chmod(mode)
        if read_existing(path) != before:
            raise ValueError("Workspace changed during setup; review backups")
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def apply_existing(directory, config_path, *, dry_run=False, init_config=False, state_directory=None):
    if (init_config and state_directory is None) or (not init_config and state_directory is not None):
        raise ValueError("New registration requires --init-config and --state-directory together")
    new_state = state_directory if init_config else None
    changes = plan(directory, config_path, new_state=new_state)
    summary = {"version": __version__, "workspace": str(directory), "in_place": True,
               "dry_run": dry_run, "changed_files": list(changes), "hook_trust_granted": False,
               "device_checks": 0, "model_turns": 0, "new_empty_registration": init_config}
    if dry_run or (not changes and not init_config):
        return summary
    root = directory / ".embedsleuth"
    root.mkdir(mode=0o700, exist_ok=True)
    absolute(root)
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Setup backups require an owner-only directory")
    lock = os.open(root / "setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        changes = plan(directory, config_path, new_state=new_state)
        # Existing host settings can contain secrets. Keep their backups
        # outside the project even before .gitignore has been updated.
        backup_root = config_path.parent / ".embedsleuth-setup-backups"
        absolute(backup_root)
        backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup_info = backup_root.stat()
        if backup_info.st_uid != os.getuid() or backup_info.st_mode & 0o077:
            raise ValueError("Backups require an owner-only external directory")
        backup = backup_root / digest(str(directory).encode())[:24] / uuid4().hex
        for name, (before, _) in changes.items():
            if before is not None:
                write_new(backup / name, before)
        if backup.exists():
            for path in sorted((p for p in backup.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                sync_directory(path)
            sync_directory(backup)
            sync_directory(backup.parent)
            sync_directory(backup_root)
            sync_directory(backup_root.parent)
        journal = root / "pending.json"
        write_new(journal, (json.dumps({"backup": str(backup), "new_config": str(config_path) if init_config else None,
                                      "new_state": str(new_state) if init_config else None, "files": {
            name: {"before": digest(before) if before is not None else None, "after": digest(after)}
            for name, (before, after) in changes.items()}}) + "\n").encode())
        sync_directory(root)
        sync_directory(directory)
        completed = []
        created_state = created_config = False
        new_config_bytes = (json.dumps({"schema_version": "1.0", "state_directory": str(new_state),
                                       "targets": []}, indent=2) + "\n").encode()
        try:
            if init_config:
                new_state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                new_state.mkdir(mode=0o700)
                created_state = True
                write_new(config_path, new_config_bytes)
                created_config = True
            for name, (before, after) in changes.items():
                replace_checked(directory / name, before, after)
                completed.append(name)
        except BaseException:
            recovered = True
            for name in reversed(completed):
                before, after = changes[name]
                try:
                    if before is None:
                        if read_existing(directory / name) != after:
                            raise ValueError("Changed output cannot be rolled back")
                        (directory / name).unlink()
                    else:
                        replace_checked(directory / name, after, before)
                except (OSError, ValueError):
                    recovered = False
            if created_state:
                try:
                    if created_config and read_existing(config_path) != new_config_bytes:
                        raise ValueError("New registration changed; preserve it")
                    new_state.rmdir()  # Preserve any evidence created by another process.
                    if created_config:
                        config_path.unlink()
                except (OSError, ValueError):
                    recovered = False
            if recovered:
                journal.unlink()
                sync_directory(root)
            raise
        journal.unlink()
        sync_directory(root)
        summary["changed_files"] = list(changes)
        summary["backup"] = str(backup)
        return summary
    finally:
        os.close(lock)

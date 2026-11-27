"""Prepare local workspace resources; never grant trust.

No SSH, model, installation, shell execution or diagnostic-state recovery.
New workspaces are exclusive. --in-place conservatively merges existing projects.
Existing registrations and diagnostic evidence are preserved.
"""

import argparse
from importlib.resources import files
import json
import os
from pathlib import Path
import shutil
import sys

from embedsleuth import __version__
from embedsleuth.config import load_configuration
from embedsleuth.hooks import codex_hook_definitions

SKILL_FILES = ("SKILL.md", "agents/openai.yaml", "references/tool-contracts.md", "references/failure-paths.md")


def absolute(path):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Use absolute paths without parent traversal")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Workspace and registration paths must not contain symlinks")
    return path


def write_new(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink()
        raise


def prepare(directory, config_path, *, init_config=False, state_directory=None):
    if sys.platform != "linux":
        raise ValueError("This release supports Linux development PCs")
    directory, config_path = absolute(directory), absolute(config_path)
    if directory.exists():
        raise ValueError("Choose a new workspace directory; existing files are never overwritten")
    if config_path.is_relative_to(directory):
        raise ValueError("Keep the private registration outside the workspace")
    if init_config:
        if config_path.exists() or state_directory is None:
            raise ValueError("New registration requires an unused file and explicit state directory")
        state_directory = absolute(state_directory)
        if state_directory.exists():
            raise ValueError("Existing evidence requires reusing its original registration")
        if state_directory.is_relative_to(directory) or directory.is_relative_to(state_directory):
            raise ValueError("Keep evidence outside the workspace")
        if (state_directory == config_path or state_directory in config_path.parents
                or config_path in state_directory.parents):
            raise ValueError("Keep configuration and evidence in separate paths")
    else:
        if state_directory is not None:
            raise ValueError("Existing registrations retain their original state directory")
        state_directory = absolute(load_configuration(config_path).state_directory)
    hook_state = directory / ".embedsleuth-hook-state"
    if state_directory == hook_state or state_directory in hook_state.parents or hook_state in state_directory.parents:
        raise ValueError("Hook records and evidence must have separate roots")

    # Read all packaged resources before creating any output.
    data = files("embedsleuth").joinpath("data")
    contents = {".agents/skills/diagnose-mpu6050/" + name:
                data.joinpath("skills/diagnose-mpu6050/" + name).read_bytes() for name in SKILL_FILES}
    contents["targets.example.json"] = data.joinpath("targets.example.json").read_bytes()
    python = Path(sys.executable)  # Preserve the venv path; resolving its symlink loses the venv.
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    contents[".codex/config.toml"] = (
        "[mcp_servers.embedsleuth]\n" + f"command = {quote(python)}\n" +
        "args = " + json.dumps(["-I", "-m", "embedsleuth.mcp_server", "--config", str(config_path)], ensure_ascii=False) +
        "\nstartup_timeout_sec = 20\ntool_timeout_sec = 20\n").encode()
    contents[".codex/hooks.json"] = (json.dumps(codex_hook_definitions(python, config_path, hook_state),
                                               ensure_ascii=False, indent=2) + "\n").encode()
    contents[".gitignore"] = b".codex/config.toml\n.codex/hooks.json\n.embedsleuth-hook-state/\nINSTALLATION.json\n"
    contents["INSTALLATION.json"] = (json.dumps({"version": __version__, "config": str(config_path),
        "state_directory": str(state_directory), "python": str(python), "hook_trust_granted": False}, indent=2) + "\n").encode()
    contents["README.md"] = (
        "# EmbedSleuth workspace\n\n"
        f"Private registration: {config_path}\n\n"
        "Fill the targets array using targets.example.json; retain the registration's state_directory. "
        "Use one stable target_id and evidence store per physical device. Do not copy a private key into JSON.\n\n"
        "Open this directory in Codex. Review project trust and all three hooks yourself. "
        "Setup has not granted trust, changed approval policy, contacted a Pi or called a model.\n\n"
        "First request (no device access):\n"
        "```text\n$diagnose-mpu6050로 MPU6050 프로필의 식별 조건만 알려 줘. 장치 검사는 하지 마.\n```\n\n"
        "After SSH trust and registration are ready, replace lab-pi with your registered target_id:\n"
        "```text\n$diagnose-mpu6050로 등록된 lab-pi의 환경만 passive로 조사해 줘. 센서 통신은 하지 마.\n```\n"
    ).encode()
    created_workspace = created_config = created_state = False
    try:
        directory.mkdir(mode=0o700)  # Parent must already exist; never merge into an existing project.
        created_workspace = True
        if init_config:
            state_directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            state_directory.mkdir(mode=0o700)
            created_state = True
            content = json.dumps({"schema_version": "1.0", "state_directory": str(state_directory), "targets": []}, indent=2)
            write_new(config_path, (content + "\n").encode())
            created_config = True
        for name, content in contents.items():
            write_new(directory / name, content)
    except BaseException:
        if created_workspace:
            shutil.rmtree(directory)
        if created_config:
            config_path.unlink()
        if created_state:
            state_directory.rmdir()
        raise
    return {"version": __version__, "workspace": str(directory), "config": str(config_path),
            "new_empty_registration": init_config, "targets_registered": 0 if init_config else "preserved",
            "hook_trust_granted": False, "device_checks": 0, "model_turns": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--init-config", action="store_true")
    parser.add_argument("--state-directory", type=Path)
    parser.add_argument("--in-place", action="store_true", help="Merge into an existing workspace (default: current directory)")
    parser.add_argument("--dry-run", action="store_true", help="List in-place changes without writing files")
    args = parser.parse_args()
    try:
        if args.in_place:
            from embedsleuth.workspace_merge import apply_existing
            directory = (args.directory or Path.cwd()).absolute()
            result = apply_existing(directory, args.config, dry_run=args.dry_run,
                                    init_config=args.init_config, state_directory=args.state_directory)
        else:
            if args.directory is None or args.dry_run:
                raise ValueError("New workspace setup requires --directory; --dry-run requires --in-place")
            result = prepare(args.directory, args.config, init_config=args.init_config, state_directory=args.state_directory)
    except ValueError as exc:
        parser.exit(2, f"Setup stopped: {exc}. No trust granted.\n")
    except OSError:
        parser.exit(2, "Setup stopped: check parent directories and permissions. Existing files are never overwritten.\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

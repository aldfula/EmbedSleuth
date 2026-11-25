"""Check host discovery without model turns, credentials or registered devices.

Uses the installed Codex app-server protocol as a development acceptance probe.
This is not the EmbedSleuth runtime and does not write the user's Codex config.
"""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tempfile
import time


class ProbeError(ValueError):
    pass


def request(process, request_id, method, params):
    process.stdin.write((json.dumps({"id": request_id, "method": method, "params": params}) + "\n").encode())
    deadline = time.monotonic() + 30
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            if not selector.select(timeout=max(0, deadline - time.monotonic())):
                raise ProbeError("Codex integration check timed out")
            line = process.stdout.readline()
            if not line:
                raise ProbeError("Codex app-server exited before responding")
            message = json.loads(line)
            if message.get("id") == request_id:
                if "error" in message:
                    raise ProbeError("Codex rejected the integration check request")
                return message["result"]
    raise ProbeError("Codex integration check timed out")


@contextmanager
def app_server(codex, project, temporary):
    from embedsleuth.hooks import codex_hook_definitions

    probe_home = temporary / "codex-home"
    probe_home.mkdir()
    targets = temporary / "targets.json"
    targets.write_text(json.dumps({"schema_version": "1.0", "state_directory": str(temporary / "state"), "targets": []}))
    targets.chmod(0o600)
    (probe_home / "hooks.json").write_text(json.dumps(codex_hook_definitions(
        Path(sys.executable), targets, temporary / "hook-state")))
    # JSON strings are a subset of TOML basic strings for these local paths.
    quote = lambda value: json.dumps(str(value))
    (probe_home / "config.toml").write_text(
        "[analytics]\nenabled = false\n"
        "[skills.bundled]\nenabled = false\n"
        f"[projects.{quote(project)}]\ntrust_level = \"trusted\"\n"
        "[mcp_servers.embedsleuth]\n"
        f"command = {quote(sys.executable)}\n"
        f"args = {json.dumps(['-I', '-m', 'embedsleuth.mcp_server', '--config', str(targets)])}\n"
        "startup_timeout_sec = 20\ntool_timeout_sec = 20\n"
    )
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(probe_home)
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("OPENAI_BASE_URL", None)
    with (temporary / "host-stderr.log").open("wb") as errors:
        process = subprocess.Popen([codex, "app-server", "--listen", "stdio://"],
            cwd=project, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=errors, bufsize=0)
        try:
            yield process
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            process.stdout.close()


def check(codex, project):
    from embedsleuth.tools import tool_input_schemas

    with tempfile.TemporaryDirectory(prefix="embedsleuth-host-check-") as directory:
        temporary = Path(directory)
        with app_server(codex, project, temporary) as process:
            request(process, 1, "initialize", {
                "clientInfo": {"name": "embedsleuth-host-check", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            })
            process.stdin.write(b'{"method":"initialized"}\n')
            listing = request(process, 2, "skills/list", {"cwds": [str(project)], "forceReload": True})
            entries = [entry for entry in listing["data"] if Path(entry["cwd"]) == project]
            if not entries or entries[0]["errors"]:
                raise ProbeError("Codex reported skill loading errors")
            matches = [skill for skill in entries[0]["skills"] if skill["name"] == "diagnose-mpu6050"]
            if len(matches) != 1 or not matches[0]["enabled"] or matches[0]["scope"] != "repo":
                raise ProbeError("Expected exactly one enabled repository diagnostic skill")
            skill = matches[0]
            if Path(skill["path"]).resolve() != project / "skills/diagnose-mpu6050/SKILL.md":
                raise ProbeError("Codex loaded a different diagnostic skill")
            dependencies = (skill.get("dependencies") or {}).get("tools", [])
            if not any(item["type"] == "mcp" and item["value"] == "embedsleuth" for item in dependencies):
                raise ProbeError("Codex did not load the EmbedSleuth MCP dependency")
            listing = request(process, 3, "mcpServerStatus/list", {"serverName": "embedsleuth", "detail": "toolsAndAuthOnly"})
            servers = [item for item in listing["data"] if item["name"] == "embedsleuth"]
            if len(servers) != 1:
                raise ProbeError("Codex did not discover the EmbedSleuth server")
            tools = servers[0]["tools"]
            advertised = {tool["name"]: tool["inputSchema"] for tool in tools.values()}
            if advertised != tool_input_schemas():
                raise ProbeError("Codex did not receive the nine exact diagnostic tool schemas")
            if list((temporary / "state/runs").iterdir()):
                raise ProbeError("Host discovery unexpectedly created a diagnostic run")
            listing = request(process, 4, "hooks/list", {"cwds": [str(project)]})
            entries = [entry for entry in listing["data"] if Path(entry["cwd"]) == project]
            if len(entries) != 1 or entries[0]["errors"]:
                raise ProbeError("Codex reported hook loading errors")
            hooks = [item for item in entries[0]["hooks"]
                     if Path(item["sourcePath"]) == temporary / "codex-home/hooks.json"]
            if (len(hooks) != 3 or {item["eventName"] for item in hooks} != {"preToolUse", "postToolUse", "stop"}
                    or any(item["handlerType"] != "command" or item["trustStatus"] != "untrusted"
                           or item["isManaged"] or item["timeoutSec"] != 5 for item in hooks)):
                raise ProbeError("Expected three untrusted command hooks; review host version/trust")
            if (temporary / "hook-state").exists():
                raise ProbeError("Host discovery unexpectedly executed diagnostic hooks")
    return {"skill": "diagnose-mpu6050", "scope": "repo", "mcp_server": "embedsleuth",
            "tool_count": len(advertised), "hook_count": len(hooks), "hook_trust": "untrusted",
            "model_turns": 0, "registered_targets": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex", help="Installed Codex CLI executable")
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    codex = shutil.which(args.codex)
    if not codex:
        parser.exit(2, "Codex CLI is not installed\n")
    try:
        result = check(codex, args.project.resolve())
    except (ProbeError, OSError, ValueError, KeyError):
        parser.exit(1, "Codex discovery check failed; check the installed host version and repository skill\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

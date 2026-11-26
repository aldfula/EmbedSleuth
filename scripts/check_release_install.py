"""Run with a clean installed venv's Python -I, from outside the repository.

Exercises installed setup, production STDIO MCP and actual hook commands with
an empty registration. Never contacts a Pi, model, or real operator store.
"""

import asyncio
from importlib.resources import files
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import tomllib

import embedsleuth
from embedsleuth.config import load_configuration
from embedsleuth.tools import tool_input_schemas
from mcp import Client
from mcp.client.stdio import StdioServerParameters


def hook(groups, payload):
    command = shlex.split(groups[0]["hooks"][0]["command"])
    result = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True,
                            check=True, timeout=10)
    assert json.loads(result.stdout) == {}, "Installed hook did not validate the profile/empty Stop"


async def check(root):
    workspace = root / "workspace"
    config = root / "private/targets.json"
    subprocess.run([sys.executable, "-I", "-m", "embedsleuth.workspace", "--directory", str(workspace),
                    "--config", str(config), "--init-config", "--state-directory", str(root / "state")],
                   check=True, capture_output=True, text=True, timeout=10)
    assert load_configuration(config).targets == ()
    source = files("embedsleuth").joinpath("data/skills/diagnose-mpu6050")
    skill = workspace / ".agents/skills/diagnose-mpu6050"
    for name in ("SKILL.md", "agents/openai.yaml", "references/tool-contracts.md", "references/failure-paths.md"):
        assert (skill / name).read_bytes() == source.joinpath(name).read_bytes()
    mcp = tomllib.loads((workspace / ".codex/config.toml").read_text())["mcp_servers"]["embedsleuth"]
    hooks = json.loads((workspace / ".codex/hooks.json").read_text())["hooks"]
    payload = {"session_id": "release-install-smoke", "turn_id": "profile-only", "tool_use_id": "call-1",
               "tool_name": "mcp__embedsleuth__get_component_profile", "tool_input": {"profile_id": "mpu6050"}}
    hook(hooks["PreToolUse"], payload | {"hook_event_name": "PreToolUse"})
    params = StdioServerParameters(command=mcp["command"], args=mcp["args"], cwd=str(workspace))
    async with Client(params, read_timeout_seconds=10) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name: tool.input_schema for tool in tools} == tool_input_schemas()
        response = await client.call_tool("get_component_profile", {"profile_id": "mpu6050"})
        assert not response.is_error and response.structured_content["error"] is None
        raw = {"content": [{"type": "text", "text": response.content[0].text}],
               "isError": response.is_error, "structuredContent": response.structured_content}
    hook(hooks["PostToolUse"], payload | {"hook_event_name": "PostToolUse", "tool_response": raw})
    hook(hooks["Stop"], {"hook_event_name": "Stop", "session_id": payload["session_id"], "turn_id": payload["turn_id"]})
    assert not list((root / "state/runs").iterdir())
    assert not (workspace / ".embedsleuth-hook-state").exists()
    assert all((Path(sys.executable).parent / name).is_file()
               for name in ("embedsleuth-mcp", "embedsleuth-hook", "embedsleuth-setup"))
    return {"installed_version": embedsleuth.__version__, "standalone_setup": "pass", "mcp_tools": len(tools),
            "packaged_skill": "pass", "hook_commands": 3, "registered_targets": 0,
            "device_checks": 0, "model_turns": 0, "hook_trust_granted": False}


def main():
    assert Path(embedsleuth.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), "Use a non-editable clean venv"
    with tempfile.TemporaryDirectory(prefix="embedsleuth-installed-") as directory:
        print(json.dumps(asyncio.run(check(Path(directory))), indent=2))


if __name__ == "__main__":
    main()

"""One explicitly authorized real Codex/skill/hooks/MCP passive device turn.

No API client, sensor transfer, root change, hook-trust bypass or model retry.
The existing production registration and evidence store remain authoritative.
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import tomllib

from embedsleuth.config import load_configuration
from embedsleuth.storage import RunStore, sanitize
from embedsleuth.targets import TargetRegistry
from embedsleuth.tools import tool_input_schemas, validate_arguments

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("model_acceptance", ROOT / "scripts/run_model_acceptance.py")
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


class RealSession(common.HostSession):
    def __init__(self, process, store, target_id, old_runs):
        super().__init__(process)
        self.store_root, self.target_id, self.old_runs = store, target_id, set(old_runs)
        self.bound_run, self.approved = None, []
        self.admission_closed = False

    def approved_request(self, message):
        if self.admission_closed:
            return False
        try:
            params = message["params"]
            meta = params["_meta"]
            tool = meta["tool_name"]
            expected = ["inspect_target", "inspect_i2c_environment"]
            if (message["method"] != "mcpServer/elicitation/request" or params["serverName"] != "embedsleuth"
                    or params.get("environmentId") is not None or params["mode"] != "form"
                    or meta["codex_approval_kind"] != "mcp_tool_call" or len(self.approved) >= 2
                    or tool != expected[len(self.approved)]):
                return False
            request = validate_arguments(tool, meta["tool_params"])
            if request.run_id in self.old_runs or self.bound_run not in (None, request.run_id):
                return False
            with RunStore.open_reader(self.store_root) as store:
                state = store.load_state(request.run_id)
                if (state["target_id"] != self.target_id or state["mode"] != "passive"
                        or state["profile_id"] != "mpu6050" or store.is_quarantined(self.target_id)):
                    return False
        except (ValueError, OSError, KeyError, TypeError):
            return False
        self.bound_run = request.run_id
        self.approved.append(tool)
        self.messages.append({"method": "embedsleuth/realAcceptanceApproval", "params": {
            "tool": tool, "run_id": request.run_id, "scope": "one_registered_passive_tool_call",
            "source": "explicit_user_preauthorization"}})
        return True


def invocation_overrides(user_config):
    if (any(os.environ.get(name) for name in ("OPENAI_BASE_URL", "CHATGPT_BASE_URL"))
            or user_config.get("model_provider", "openai") != "openai" or user_config.get("openai_base_url")
            or user_config.get("chatgpt_base_url")
            or user_config.get("model_providers", {}).get("openai", {}).get("base_url")):
        raise ValueError("destination differs from reviewed OpenAI scope")
    overrides = []
    for name in user_config.get("mcp_servers", {}):
        if re.fullmatch(r"[A-Za-z0-9_-]+", name) is None:
            raise ValueError("unsupported server name")
        if name != "embedsleuth":
            overrides += ["-c", "mcp_servers." + name + ".enabled=false"]
    # This evaluation restricts unrelated capabilities; persistent host settings,
    # approval policy and hook trust are not changed.
    for name in ("shell_tool", "apps", "plugins", "browser_use", "browser_use_external", "computer_use", "in_app_browser",
                 "image_generation", "view_image", "multi_agent", "multi_agent_v2", "skill_search"):
        overrides += ["-c", "features." + name + "=false"]
    overrides += ["-c", 'web_search="disabled"']
    return overrides


def turn_request(thread_id, project, target_id):
    skill = project / ".agents/skills/diagnose-mpu6050/SKILL.md"
    return {"threadId": thread_id, "input": [
        {"type": "text", "text": "$diagnose-mpu6050로 등록 대상 " + target_id +
         "의 Raspberry Pi와 MPU6050 연결 환경을 센서 통신 없이 조사하고, 실제 증거와 미확인 사항을 보고해 줘.",
         "text_elements": []},
        {"type": "skill", "name": "diagnose-mpu6050", "path": str(skill)}],
        "sandboxPolicy": {"type": "readOnly", "networkAccess": False}}


def wait_for_mcp(session, thread_id):
    deadline = time.monotonic() + 25
    checked = 0
    while True:
        for message in session.messages[checked:]:
            if message.get("method") != "mcpServer/startupStatus/updated":
                continue
            status = message["params"]
            if status.get("threadId") != thread_id or status.get("name") != "embedsleuth":
                continue
            if status.get("status") == "ready":
                return
            if status.get("status") in {"failed", "cancelled"}:
                raise ValueError("production MCP startup failed")
        checked = len(session.messages)
        session.receive(deadline)


def production_inventory_ok(listing):
    servers = [row for row in listing["data"] if row["name"] == "embedsleuth"]
    # The host also lists disabled configured servers. They are acceptable
    # only when explicitly disabled and exposing no tools or resource paths.
    others = [row for row in listing["data"] if row["name"] != "embedsleuth"]
    if (listing.get("nextCursor") or len(servers) != 1
            or any(row.get("runtimeStatus") != "disabled" or row.get("tools")
                   or row.get("resources") or row.get("resourceTemplates") for row in others)):
        return False
    server = servers[0]
    return (server.get("runtimeStatus") == "connected" and not server.get("toolsError")
            and {tool["name"]: tool["inputSchema"] for tool in server["tools"].values()} == tool_input_schemas())


def evaluate(session, project, target_id):
    session.stage = "initialize"
    session.model_turn_requested = False
    session.request("initialize", {"clientInfo": {"name": "embedsleuth-real-agent-acceptance", "version": "0.1.0"},
                                  "capabilities": {"experimentalApi": True}})
    session.send({"method": "initialized"})
    references = []
    for name in ("tool-contracts.md", "failure-paths.md"):
        references.append((project / "skills/diagnose-mpu6050/references" / name).read_text())
    instructions = ("This is one authorized acceptance test on a real registered device. Use only EmbedSleuth MCP. "
        "Create exactly one passive diagnosis for the requested target. Inspect target and I2C environment once each. "
        "Never read device identity, use other servers, shell/SSH/network commands, credentials, local configuration, "
        "private evidence files, fixture data, settings changes or subprocess agents. Do not inspect historical runs. "
        "On error preserve the current report without hardware retry. Use normal repository hooks; do not alter trust. "
        "Only the two passive inspections have one-time preauthorization. Skill references are provided verbatim "
        "because unrelated shell capabilities are disabled for this test:\n" + "\n".join(references))
    started = session.request("thread/start", {"cwd": str(project), "ephemeral": True,
                                              "developerInstructions": instructions})
    session.stage = "model_destination"
    if started.get("model") != "gpt-6.1-sol" or started.get("modelProvider") != "openai":
        raise ValueError("model/provider differs from reviewed scope; no model turn started")
    thread_id = started["thread"]["id"]
    session.stage = "mcp_startup"
    wait_for_mcp(session, thread_id)
    session.stage = "mcp_inventory"
    listing = session.request("mcpServerStatus/list", {"threadId": thread_id, "detail": "toolsAndAuthOnly"})
    if not production_inventory_ok(listing):
        raise ValueError("production MCP inventory mismatch")
    session.stage = "trusted_hooks"
    hooks = session.request("hooks/list", {"threadId": thread_id})
    project_hooks = [hook for row in hooks["data"] if Path(row["cwd"]) == project for hook in row["hooks"]
                     if Path(hook["sourcePath"]) == project / ".codex/hooks.json"]
    if (len(project_hooks) != 3 or any(h["trustStatus"] != "trusted" for h in project_hooks)
            or {h["eventName"] for h in project_hooks} != {"preToolUse", "postToolUse", "stop"}):
        raise ValueError("three already-trusted project hooks required; trust is never changed")
    request = turn_request(thread_id, project, target_id)
    session.stage = "model_turn"
    session.model_turn_requested = True
    turn = session.request("turn/start", request)
    deadline = time.monotonic() + 180
    try:
        while True:
            event = session.receive(deadline)
            if event.get("method") == "turn/completed" and event["params"]["turn"]["id"] == turn["turn"]["id"]:
                break
    except BaseException:
        session.admission_closed = True
        session.request("turn/interrupt", {"threadId": thread_id, "turnId": turn["turn"]["id"]}, timeout=10)
        raise
    session.admission_closed = True
    session.stage = "model_turn_completed"
    return {"model": started["model"], "turn_status": event["params"]["turn"]["status"],
            "thread_id": thread_id, "hook_trust": {h["eventName"]: h["trustStatus"] for h in project_hooks},
            "approved_remote_tools": session.approved,
            "items": [m["params"]["item"] for m in session.messages if m.get("method") == "item/completed"],
            "hooks": [m["params"] for m in session.messages if m.get("method") == "hook/completed"],
            "human_review": "required"}


def run(project, config_path, target_id, output, codex):
    config = load_configuration(config_path)
    TargetRegistry(config.targets).get(target_id)
    with RunStore.open_reader(config.state_directory) as store:
        if store.is_quarantined(target_id):
            raise ValueError("quarantined target")
        baseline = store.list_runs()
    # Keep actual project MCP/hooks; reject a different registered config.
    project_config = tomllib.loads((project / ".codex/config.toml").read_text())
    expected = ["-I", "-m", "embedsleuth.mcp_server", "--config", str(config_path)]
    if project_config["mcp_servers"]["embedsleuth"]["args"] != expected:
        raise ValueError("project MCP uses a different registration")
    user_path = Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex"))) / "config.toml"
    user = tomllib.loads(user_path.read_text()) if user_path.exists() else {}
    output.mkdir(mode=0o700)
    overrides = invocation_overrides(user) + invocation_overrides(project_config)
    started = time.monotonic()
    factory = lambda process, _: RealSession(process, config.state_directory, target_id, baseline)
    error = None
    with common.host(codex, project, overrides, output/"host-stderr.log", session_factory=factory) as session:
        try:
            result = evaluate(session, project, target_id)
        except Exception as exc:
            error = exc
            session.admission_closed = True
            result = {"turn_status": "unavailable", "error_kind": type(exc).__name__,
                      "stage": getattr(session, "stage", "host_start"),
                      "model_turn_requested": getattr(session, "model_turn_requested", False),
                      "human_review": "required", "approved_remote_tools": session.approved}
        finally:
            path = output/"events.json"
            path.write_text(json.dumps(sanitize(session.messages), ensure_ascii=False, indent=2)+"\n")
            path.chmod(0o600)
    result["elapsed_seconds"] = round(time.monotonic()-started, 3)
    result["host_version"] = subprocess.check_output([codex,"--version"], text=True).strip()
    with RunStore.open_reader(config.state_directory) as store:
        created = [r for r in store.list_runs() if r not in baseline and store.load_state(r)["target_id"] == target_id]
        result["runs"] = [{"run_id": r, "state": store.load_state(r)["state"],
                           "pending_operation": store.load_state(r)["pending_operation"],
                           "report": store.load_report(r), "audit": store.read_events(r)} for r in created]
    path = output/"result.json"
    path.write_text(json.dumps(sanitize(result), ensure_ascii=False, indent=2)+"\n");path.chmod(0o600)
    if error is not None:
        raise ValueError("Acceptance unavailable; preserved events/results require review; no automatic retry") from None
    return {k: result[k] for k in ("model", "turn_status", "elapsed_seconds", "approved_remote_tools", "human_review")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-real-model-and-passive-reads", required=True, action="store_true")
    args = parser.parse_args()
    codex = shutil.which("codex")
    if codex is None:
        raise SystemExit("Codex required")
    print(json.dumps(run(ROOT, args.config, args.target, args.output, codex), indent=2))


if __name__ == "__main__":
    main()

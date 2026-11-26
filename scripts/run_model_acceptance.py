"""Opt-in Codex model evaluation. Uses host authentication, not an API client.

Starts an ephemeral test context and one real model turn. Fake device backend
is the default. This is a test driver, not a product agent loop. Does not trust
hooks, alter approval policy, copy credentials or assign a different model.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time

from embedsleuth.storage import RunStore, sanitize
from embedsleuth.tools import tool_input_schemas, validate_arguments


class EvaluationError(ValueError):
    pass


class HostSession:
    def __init__(self, process, fixture_store=None):
        self.process, self.buffer, self.messages = process, bytearray(), []
        self.next_id = 0
        self.bytes_received = 0
        self.fixture_store, self.fixture_approvals_left = fixture_store, 3
        self.fixture_approved_tools = set()

    def approved_fixture_read(self, message):
        """Only an explicitly preauthorized, single simulated tool request.

        The caller binds this to the fixture server it starts, never a user or
        payload-provided store. No persistent approval or hook trust is granted.
        """
        if self.fixture_store is None or self.fixture_approvals_left <= 0:
            return False
        try:
            params = message["params"]
            meta = params["_meta"]
            tool = meta["tool_name"]
            if (message["method"] != "mcpServer/elicitation/request" or params["serverName"] != "embedsleuth"
                    or params.get("environmentId") is not None or params["mode"] != "form"
                    or meta["codex_approval_kind"] != "mcp_tool_call"
                    or tool not in {"inspect_target", "inspect_i2c_environment", "read_device_identity"}
                    or tool in self.fixture_approved_tools):
                return False
            request = validate_arguments(tool, meta["tool_params"])
            with RunStore.open_reader(self.fixture_store) as store:
                if store.load_state(request.run_id)["target_id"] != "lab-pi":
                    return False
            if tool == "read_device_identity" and (request.address != 104 or request.bus_ref != "bus-1"):
                return False
        except (OSError, ValueError, KeyError, TypeError):
            return False
        self.fixture_approvals_left -= 1
        self.fixture_approved_tools.add(tool)
        self.messages.append({"method": "embedsleuth/evaluationApproval", "params": {
            "tool": tool, "scope": "one_fake_device_call", "source": "explicit_user_preauthorization"}})
        return True

    def approved_request(self, message):
        return self.approved_fixture_read(message)

    def send(self, value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())

    def receive(self, deadline):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in self.buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise EvaluationError("Model evaluation deadline expired")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise EvaluationError("Codex exited before evaluation completed")
                self.buffer.extend(chunk)
                if len(self.buffer) > 4 * 1024 * 1024:
                    raise EvaluationError("Evaluation message exceeded budget")
        line, _, rest = self.buffer.partition(b"\n")
        self.buffer = bytearray(rest)
        self.bytes_received += len(line)
        if self.bytes_received > 16 * 1024 * 1024:
            raise EvaluationError("Evaluation stream budget exhausted")
        message = json.loads(line)
        if "method" in message:
            self.messages.append(sanitize(message))
            if len(self.messages) > 3000:
                raise EvaluationError("Evaluation event budget exhausted")
            if "id" in message:
                if self.approved_request(message):
                    self.send({"id": message["id"], "result": {"action": "accept", "content": {}}})
                else:
                    self.send({"id": message["id"], "error": {"code": -32601,
                               "message": "Evaluation requires human input; no approval granted"}})
        return message

    def request(self, method, params, *, timeout=30):
        self.next_id += 1
        request_id = self.next_id
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self.receive(deadline)
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise EvaluationError("Codex rejected an evaluation request")
                return message["result"]


@contextmanager
def host(codex, cwd, overrides, errors_path, fixture_store=None, session_factory=HostSession):
    with errors_path.open("xb") as errors:
        errors_path.chmod(0o600)
        process = subprocess.Popen([codex, *overrides, "app-server", "--listen", "stdio://"],
            cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, bufsize=0)
        try:
            yield session_factory(process, fixture_store)
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


def evaluate(session, cwd, prompt, *, timeout, profile_only=False):
    session.request("initialize", {"clientInfo": {"name": "embedsleuth-model-acceptance", "version": "0.1.0"},
                                  "capabilities": {"experimentalApi": True}})
    session.send({"method": "initialized"})
    instructions = ("This is an acceptance test. Do not install software, alter configuration, use SSH/network "
        "or access credentials. Use the provided EmbedSleuth MCP tools for device work. "
        "Use repository skills normally. Do not read evaluator fixtures or grading data. "
        + ("This turn permits only get_component_profile; never create a diagnosis or inspect a target."
           if profile_only else "The provided MCP device is simulated; do not contact real hardware."))
    started = session.request("thread/start", {"cwd": str(cwd), "ephemeral": True,
                                              "developerInstructions": instructions})
    if started.get("model") != "gpt-6.1-sol" or started.get("modelProvider") != "openai":
        raise EvaluationError("Resolved model/provider differs from the approved evaluation scope")
    thread_id = started["thread"]["id"]
    inventory = session.request("mcpServerStatus/list", {"threadId": thread_id, "serverName": "embedsleuth", "detail": "toolsAndAuthOnly"})
    servers = [server for server in inventory["data"] if server["name"] == "embedsleuth"]
    if len(servers) != 1 or {tool["name"]: tool["inputSchema"] for tool in servers[0]["tools"].values()} != tool_input_schemas():
        raise EvaluationError("Evaluation MCP inventory is missing or incompatible")
    turn = session.request("turn/start", {"threadId": thread_id,
        "input": [{"type": "text", "text": prompt, "text_elements": []}]})
    deadline = time.monotonic() + timeout
    turn_id = turn["turn"]["id"]
    while True:
        message = session.receive(deadline)
        if (message.get("method") == "turn/completed"
                and message["params"]["turn"]["id"] == turn_id):
            break
    items = [message["params"]["item"] for message in session.messages if message.get("method") == "item/completed"]
    return {"model": started.get("model"), "turn_status": message["params"]["turn"]["status"],
            "assistant_messages": [item["text"] for item in items if item["type"] == "agentMessage"],
            "mcp_calls": [{key: item.get(key) for key in ("server", "tool", "arguments", "status")}
                          for item in items if item["type"] == "mcpToolCall"],
            "hooks": [message["params"] for message in session.messages if message.get("method") == "hook/completed"],
            "human_review": "required", "events": session.messages}


def run(codex, project, output, case_id, implicit, profile_only, timeout, approve_fixture_reads=False):
    # Outside the repository so operational project hooks/targets cannot leak
    # into the simulated test through parent-directory config discovery.
    with tempfile.TemporaryDirectory(prefix="embedsleuth-model-workspace-") as directory:
        return _run(codex, project, output, case_id, implicit, profile_only, timeout, Path(directory), approve_fixture_reads)


def _run(codex, project, output, case_id, implicit, profile_only, timeout, workspace, approve_fixture_reads):
    if approve_fixture_reads and (profile_only or case_id not in {"identity-104", "driver-bound"}):
        raise EvaluationError("Fixture read preauthorization is limited to two reviewed simulated cases")
    if output.exists():
        raise EvaluationError("Choose a new output directory; existing evidence is never overwritten")
    output.mkdir(mode=0o700, parents=True)
    if profile_only:
        prompt = "$diagnose-mpu6050로 MPU6050 프로필의 식별 조건과 문서 근거만 보여 줘. 장치 검사는 하지 마."
        cwd, overrides = project, []
    else:
        cases = json.loads((project / "evals/diagnose-mpu6050/cases.json").read_text())
        case = next((item for item in cases["offline_replays"] if item["id"] == case_id), None)
        if case is None:
            raise EvaluationError("Unknown evaluation case")
        # A standalone temporary workspace has only the skill, no traces/expected.
        cwd = workspace
        skills = cwd / ".agents/skills"
        skills.mkdir(mode=0o700, parents=True)
        shutil.copytree(project / "skills/diagnose-mpu6050", skills / "diagnose-mpu6050")
        fixture_path = output / "fixture.json"
        fixture_path.write_text(json.dumps(case["fixture"]))
        fixture_path.chmod(0o600)
        # Disable the user's other MCP servers in this test invocation only.
        # No credential/authentication files are read or copied by this driver.
        user_path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
        import tomllib
        user_config = tomllib.loads(user_path.read_text()) if user_path.exists() else {}
        if (user_config.get("model_provider", "openai") != "openai"
                or user_config.get("openai_base_url") or user_config.get("chatgpt_base_url")
                or user_config.get("model_providers", {}).get("openai", {}).get("base_url")):
            raise EvaluationError("Provider destination differs from the approved evaluation scope")
        overrides = []
        for name in user_config.get("mcp_servers", {}):
            # CLI -c splits dotted keys literally; quoted server names would
            # create a different incomplete server instead of disabling it.
            if re.fullmatch(r"[A-Za-z0-9_-]+", name) is None:
                raise EvaluationError("Unsupported MCP name in isolated evaluation configuration")
            overrides += ["-c", "mcp_servers." + name + ".enabled=false"]
        overrides += ["-c", f'mcp_servers.embedsleuth.command={json.dumps(sys.executable)}',
            "-c", "mcp_servers.embedsleuth.enabled=true",
            "-c", "mcp_servers.embedsleuth.args=" + json.dumps(["-I", str(project / "evals/diagnose-mpu6050/server.py"),
                         "--fixture", str(fixture_path), "--directory", str(output / "device")])]
        prompt = case["prompt"]
        if implicit:
            prompt = prompt.replace("$diagnose-mpu6050로 ", "EmbedSleuth로 ", 1)
    started = time.monotonic()
    fixture_store = output / "device/state" if approve_fixture_reads else None
    with host(codex, cwd, overrides, output / "host-stderr.log", fixture_store) as session:
        try:
            result = evaluate(session, cwd, prompt, timeout=timeout, profile_only=profile_only)
        finally:
            # Preserve partial host evidence even on timeout/unsupported input.
            event_path = output / "events.json"
            event_path.write_text(json.dumps(session.messages, ensure_ascii=False, indent=2) + "\n")
            event_path.chmod(0o600)
    result.update({"checked_at": datetime.now(timezone.utc).isoformat(), "elapsed_seconds": round(time.monotonic() - started, 2),
                   "case": "project-profile-only" if profile_only else case_id,
                   "selection": "implicit" if implicit else "explicit", "prompt": prompt,
                   "hardware": "none", "hooks_expected": profile_only, "host_version": subprocess.check_output([codex, "--version"], text=True).strip()})
    (output / "result.json").write_text(json.dumps(sanitize(result), ensure_ascii=False, indent=2) + "\n")
    (output / "result.json").chmod(0o600)
    return {key: result[key] for key in ("case", "model", "turn_status", "elapsed_seconds", "mcp_calls", "human_review")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--case", default="passive")
    parser.add_argument("--implicit", action="store_true")
    parser.add_argument("--project-profile-only", action="store_true")
    parser.add_argument("--approve-fixture-reads", action="store_true",
                        help="Requires separate human preauthorization; only simulated identity-104/driver-bound reads")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    codex = shutil.which(args.codex)
    if codex is None or not 1 <= args.timeout <= 300:
        parser.exit(2, "Codex is required; timeout must be between 1 and 300 seconds\n")
    try:
        result = run(codex, args.project.resolve(), args.output.absolute(), args.case,
                     args.implicit, args.project_profile_only, args.timeout, args.approve_fixture_reads)
    except (ValueError, OSError, KeyError, TypeError):
        parser.exit(1, "Model acceptance did not complete; inspect host availability and preserved evidence\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

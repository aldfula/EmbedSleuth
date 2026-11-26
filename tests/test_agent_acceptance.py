"""Real-agent test guardrails are tested without a model or physical target."""

import importlib.util
from pathlib import Path
import sys

import pytest

from embedsleuth.storage import RunStore
from embedsleuth.tools import tool_input_schemas

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("agent_acceptance", ROOT / "scripts/run_agent_acceptance.py")
runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)


def message(tool="inspect_target", run="run-new"):
    return {"method": "mcpServer/elicitation/request", "params": {"serverName": "embedsleuth", "mode": "form",
        "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_name": tool, "tool_params": {"run_id": run}}}}


@pytest.fixture
def session(tmp_path):
    root = tmp_path / "state"
    with RunStore(root) as store:
        for name in ("run-old", "run-new", "run-other"):
            store.create(name, {"target_id": "pi-1", "mode": "passive", "profile_id": "mpu6050", "remote_state": "idle"})
    return runner.RealSession(None, root, "pi-1", ["run-old"])


def test_only_new_bound_passive_run_and_two_ordered_inspections_are_approved(session):
    assert not session.approved_request(message("inspect_i2c_environment"))
    assert not session.approved_request(message(run="run-old"))
    assert session.approved_request(message())
    assert not session.approved_request(message())
    assert not session.approved_request(message("inspect_i2c_environment", "run-other"))
    assert session.approved_request(message("inspect_i2c_environment"))
    assert not session.approved_request(message("inspect_i2c_environment"))
    assert session.approved == ["inspect_target", "inspect_i2c_environment"]


@pytest.mark.parametrize("change", ["wrong_target", "identity_mode", "quarantined", "closed", "other_server", "other_kind", "identity_tool"])
def test_unreviewed_requests_are_denied(session, change):
    request = message()
    with RunStore(session.store_root) as store:
        state = store.load_state("run-new")
        if change == "wrong_target": state["target_id"] = "another-pi"
        if change == "identity_mode": state["mode"] = "read_only"
        if change == "quarantined": store.quarantine_target("pi-1", "unconfirmed")
        store.save_state("run-new", state)
    if change == "closed": session.admission_closed = True
    if change == "other_server": request["params"]["serverName"] = "another-server"
    if change == "other_kind": request["params"]["_meta"]["codex_approval_kind"] = "trust"
    if change == "identity_tool": request["params"]["_meta"]["tool_name"] = "read_device_identity"
    assert not session.approved_request(request)
    assert session.approved == []


@pytest.mark.parametrize("config", [
    {"model_provider": "other"}, {"openai_base_url": "https://other.example"},
    {"chatgpt_base_url": "https://other.example"},
    {"model_providers": {"openai": {"base_url": "https://other.example"}}},
])
def test_redirected_destinations_are_rejected(config):
    with pytest.raises(ValueError, match="destination"):
        runner.invocation_overrides(config)


@pytest.mark.parametrize("name", ["OPENAI_BASE_URL", "CHATGPT_BASE_URL"])
def test_environment_redirect_is_also_rejected(monkeypatch, name):
    monkeypatch.setenv(name, "https://other.example")
    with pytest.raises(ValueError, match="destination"):
        runner.invocation_overrides({})


def test_invocation_restricts_unrelated_capabilities_without_changing_approval_or_trust():
    args = runner.invocation_overrides({"mcp_servers": {"unrelated": {}, "embedsleuth": {}}})
    joined = " ".join(args)
    assert "mcp_servers.unrelated.enabled=false" in args
    assert "features.shell_tool=false" in args
    assert "features.apps=false" in args
    assert "features.plugins=false" in args
    assert "features.multi_agent=false" in args
    assert "features.view_image=false" in args
    assert not any(word in joined for word in ("approval_policy", "trust_level", "hooks=false"))


def test_skill_and_sandbox_are_explicit_and_private_paths_are_not_exposed():
    params = runner.turn_request("thread-1", ROOT, "pi-1")
    assert params["input"][1]["type"] == "skill"
    policy = params["sandboxPolicy"]
    assert policy == {"type": "readOnly", "networkAccess": False}
    assert ".embedsleuth-local" not in str(params) and ".ssh" not in str(params)


class FakeSession:
    def __init__(self, model="gpt-6.1-sol", trust="trusted"):
        self.model, self.trust, self.calls, self.messages = model, trust, [], []
        self.approved, self.admission_closed = [], False
    def send(self, value): pass
    def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method == "initialize": return {}
        if method == "thread/start":
            self.messages.append({"method": "mcpServer/startupStatus/updated", "params": {
                "threadId": "thread-1", "name": "embedsleuth", "status": "ready"}})
            return {"model": self.model, "modelProvider": "openai", "thread": {"id": "thread-1"}}
        if method == "mcpServerStatus/list": return {"data": [{"name": "embedsleuth", "runtimeStatus": "connected", "tools": {
            name: {"name": name, "inputSchema": schema} for name, schema in tool_input_schemas().items()}}]}
        if method == "hooks/list": return {"data": [{"cwd": str(ROOT), "hooks": [
            {"sourcePath": str(ROOT/".codex/hooks.json"), "trustStatus": self.trust, "eventName": event}
            for event in ("preToolUse", "postToolUse", "stop")]}]}
        if method == "turn/start": return {"turn": {"id": "turn-1"}}
        raise AssertionError(method)
    def receive(self, deadline):
        return {"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}}


@pytest.mark.parametrize("change", ["model", "trust"])
def test_host_preconditions_fail_before_any_model_turn(change):
    session = FakeSession(model="different" if change == "model" else "gpt-6.1-sol",
                          trust="untrusted" if change == "trust" else "trusted")
    with pytest.raises(ValueError): runner.evaluate(session, ROOT, "pi-1")
    assert "turn/start" not in [method for method, _ in session.calls]
    assert session.model_turn_requested is False


def test_at_most_one_turn_is_started_and_admission_is_closed_after_completion():
    session = FakeSession()
    result = runner.evaluate(session, ROOT, "pi-1")
    assert sum(method == "turn/start" for method, _ in session.calls) == 1
    assert result["turn_status"] == "completed" and session.admission_closed


def test_explicit_consent_flag_is_required_before_any_host_start(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["agent", "--config", "/missing", "--target", "pi-1", "--output", str(tmp_path/"out")])
    with pytest.raises(SystemExit): runner.main()
    assert not (tmp_path/"out").exists()


def test_inventory_waits_for_matching_server_readiness_without_model_or_device_calls():
    session = FakeSession()
    updates = iter([("different-thread", "embedsleuth", "ready"),
                    ("thread-1", "other", "ready"), ("thread-1", "embedsleuth", "ready")])
    def receive(deadline):
        thread, name, status = next(updates)
        session.messages.append({"method": "mcpServer/startupStatus/updated", "params": {
            "threadId": thread, "name": name, "status": status}})
    session.receive = receive
    runner.wait_for_mcp(session, "thread-1")
    assert len(session.messages) == 3 and session.calls == []


def test_failed_mcp_startup_aborts_before_turn():
    session = FakeSession()
    session.messages.append({"method": "mcpServer/startupStatus/updated", "params": {
        "threadId": "thread-1", "name": "embedsleuth", "status": "failed"}})
    with pytest.raises(ValueError, match="startup"):
        runner.wait_for_mcp(session, "thread-1")
    assert session.calls == []


@pytest.mark.parametrize("change", ["disabled_empty", "connected_empty", "disabled_tools", "disabled_resources",
    "disabled_templates", "unknown_status", "duplicate", "pagination", "wrong_schema"])
def test_inventory_accepts_only_exact_production_tools_and_inert_disabled_rows(change):
    session = FakeSession()
    listing = session.request("mcpServerStatus/list", {})
    other = {"name": "other", "runtimeStatus": "disabled", "tools": {}, "resources": [], "resourceTemplates": []}
    listing["data"].append(other)
    if change == "connected_empty": other["runtimeStatus"] = "connected"
    if change == "disabled_tools": other["tools"] = {"shell": {}}
    if change == "disabled_resources": other["resources"] = [{"uri": "private://data"}]
    if change == "disabled_templates": other["resourceTemplates"] = [{"uriTemplate": "private://{id}"}]
    if change == "unknown_status": other.pop("runtimeStatus")
    if change == "duplicate": listing["data"].append(listing["data"][0])
    if change == "pagination": listing["nextCursor"] = "more"
    if change == "wrong_schema": listing["data"][0]["tools"].pop("get_report")
    assert runner.production_inventory_ok(listing) is (change == "disabled_empty")

"""Real core/storage/MCP envelopes; fake transport, no model or hardware."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from embedsleuth.hooks import MAX_CALLS, MAX_RUNS, PREFIX, codex_hook_definitions, decode, run_hook
from embedsleuth.mcp_server import MCPRuntime
from embedsleuth.storage import RunStore, StorageError
from test_core import make_harness, ready


@pytest.fixture
def setup_hooks(make_harness, tmp_path):
    h = make_harness()
    config = tmp_path / "targets.json"
    config.write_text(json.dumps({"schema_version": "1.0", "state_directory": str(h.store.root),
                                 "targets": [json.loads(h.target.model_dump_json())]}))
    config.chmod(0o600)
    h.config = config
    h.journal = tmp_path / "hook-state"
    return h


def payload(event, tool="inspect_target", arguments=None, call="call-1", **extra):
    return {"hook_event_name": event, "session_id": "session-secret", "turn_id": "turn-secret",
            "tool_name": PREFIX + tool, "tool_input": arguments or {}, "tool_use_id": call,
            "cwd": "/untrusted/cwd", "transcript_path": "/untrusted/secret", **extra}


def hook(h, event, tool="inspect_target", arguments=None, call="call-1", **extra):
    return run_hook(event, payload(event, tool, arguments, call, **extra), h.config, h.journal)


async def invoke(h, runtime, tool, arguments, call):
    assert hook(h, "PreToolUse", tool, arguments, call) == {}
    result = await runtime.call(tool, arguments)
    raw = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert hook(h, "PostToolUse", tool, arguments, call, tool_response=raw) == {}
    return raw["structuredContent"]["result"]


def test_complete_diagnosis_via_real_mcp_and_hooks(setup_hooks):
    h = setup_hooks

    async def check():
        runtime = MCPRuntime(h.core)
        try:
            start = await invoke(h, runtime, "start_diagnosis", {"target_id": "lab-pi", "profile_id": "mpu6050"}, "a")
            args = {"run_id": start["run_id"]}
            await invoke(h, runtime, "inspect_target", args, "b")
            await invoke(h, runtime, "inspect_i2c_environment", args, "c")
            identity = args | {"bus_ref": "bus-1", "address": 104, "operation_id": "identity-1"}
            await invoke(h, runtime, "read_device_identity", identity, "d")
            await invoke(h, runtime, "evaluate_findings", args, "e")
            await invoke(h, runtime, "finalize_report", args, "f")
            # Cached terminal replay remains legal and does not touch hardware.
            await invoke(h, runtime, "read_device_identity", identity, "g")
            await invoke(h, runtime, "get_report", args, "h")
            assert hook(h, "Stop") == {}
            assert h.transport.count("read_device_identity") == 1
        finally:
            await runtime.close()

    asyncio.run(check())
    raw = "".join(path.read_text() for path in h.journal.glob("*.json"))
    assert not any(secret in raw for secret in ("session-secret", "turn-secret", "pi.local", "/untrusted", "board_model"))


@pytest.mark.parametrize("ending", ["finalize_report", "cancel_diagnosis"])
def test_partial_or_interrupted_reports_allow_stop(setup_hooks, ending):
    h = setup_hooks

    async def check():
        runtime = MCPRuntime(h.core)
        try:
            result = await invoke(h, runtime, "start_diagnosis", {"target_id": "lab-pi", "profile_id": "mpu6050"}, "a")
            report = await invoke(h, runtime, ending, {"run_id": result["run_id"]}, "b")
            assert report["outcome"] in {"partial", "interrupted"}
            assert hook(h, "Stop") == {}
        finally:
            await runtime.close()

    asyncio.run(check())


def test_stop_correction_is_once_per_turn_even_without_host_flag(setup_hooks):
    h = setup_hooks
    run = h.core.start_diagnosis("lab-pi")["run_id"]
    assert hook(h, "PreToolUse", arguments={"run_id": run}) == {}
    first = hook(h, "Stop", stop_hook_active=False)
    assert first["decision"] == "block" and "Do not repeat hardware" in first["reason"]
    assert "decision" not in hook(h, "Stop", stop_hook_active=False)
    assert "decision" not in hook(h, "Stop", stop_hook_active=True)
    other = payload("Stop", turn_id="unrelated-turn")
    assert run_hook("Stop", other, h.config, h.journal) == {}
    assert h.transport.calls == []


def test_active_stop_hook_never_blocks_even_first_time(setup_hooks):
    h = setup_hooks
    run = h.core.start_diagnosis("lab-pi")["run_id"]
    hook(h, "PreToolUse", arguments={"run_id": run})
    assert "decision" not in hook(h, "Stop", stop_hook_active=True)


def test_unrelated_tool_and_stop_do_not_open_config_or_create_store(tmp_path):
    missing = tmp_path / "missing"
    for event in ("PreToolUse", "PostToolUse"):
        data = payload(event, tool_name="mcp__other__inspect_target")
        assert run_hook(event, data, missing, missing) == {}
    assert run_hook("Stop", payload("Stop"), missing, missing) == {}
    assert not missing.exists()


@pytest.mark.parametrize("tool,args", [
    ("shell", {"command": "password=private-secret"}),
    ("inspect_target", {"run_id": "../../private-secret"}),
    ("inspect_target", {"run_id": "nonexistent"}),
    ("start_diagnosis", {"target_id": "unknown", "profile_id": "mpu6050"}),
    ("start_diagnosis", {"target_id": "lab-pi", "profile_id": "unknown"}),
    ("read_device_identity", {"run_id": "valid", "bus_ref": "bus-1", "address": True, "operation_id": "id"}),
    ("inspect_target", {"run_id": "valid", "approved": True}),
])
def test_pre_denies_unknown_tools_runs_and_injected_approval_without_echo(setup_hooks, tool, args):
    h = setup_hooks
    result = hook(h, "PreToolUse", tool, args)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "private-secret" not in json.dumps(result)
    assert h.transport.calls == []


@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
def test_profile_queries_are_independent_of_target_config(setup_hooks, event):
    h = setup_hooks
    from embedsleuth.mcp_server import _response
    from embedsleuth.profiles import load_component_profile
    response = _response(load_component_profile("mpu6050")).model_dump(mode="json", by_alias=True)
    data = payload(event, "get_component_profile", {"profile_id": "mpu6050"}, tool_response=response)
    assert run_hook(event, data, Path("/missing"), Path("/missing")) == {}
    assert not h.journal.exists()


@pytest.mark.parametrize("change", ["observation", "text", "error", "audit", "receipt", "run"])
def test_post_warns_on_forged_or_unrecorded_results_without_hiding_them(setup_hooks, change):
    from embedsleuth.mcp_server import _response
    h = setup_hooks
    run = h.core.start_diagnosis("lab-pi")["run_id"]
    args = {"run_id": run}
    assert hook(h, "PreToolUse", arguments=args) == {}
    result = h.core.inspect_target(run)
    raw = _response(result).model_dump(mode="json", by_alias=True)
    if change == "observation":
        raw["structuredContent"]["result"]["observations"]["board_model"] = "forged"
        raw["content"][0]["text"] = json.dumps(raw["structuredContent"])
    elif change == "text":
        raw["content"][0]["text"] = "{}"
    elif change == "error":
        raw["isError"] = True
    elif change == "audit":
        (h.store.root / "runs" / run / "audit.jsonl").unlink()
    elif change == "receipt":
        args = {"run_id": h.core.start_diagnosis("lab-pi")["run_id"]}
    elif change == "run":
        raw["structuredContent"]["result"]["run_id"] = "another-run"
        raw["content"][0]["text"] = json.dumps(raw["structuredContent"])
    original = deepcopy(raw)
    result = hook(h, "PostToolUse", arguments=args, tool_response=raw)
    assert "systemMessage" in result and "decision" not in result
    assert raw == original and len(h.transport.calls) == 1


def test_core_policy_survives_absent_failed_and_timed_out_hooks(setup_hooks):
    h = setup_hooks
    run = ready(h, mode="passive")
    args = {"run_id": run, "bus_ref": "bus-1", "address": 104, "operation_id": "one"}
    assert hook(h, "PreToolUse", "read_device_identity", args) == {}  # Neutral, not approval.
    h.config.unlink()
    assert hook(h, "PreToolUse", "read_device_identity", args)["hookSpecificOutput"]["permissionDecision"] == "deny"
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run([sys.executable, "-I", "-c", "import time; time.sleep(2)"], timeout=0.05, check=True)
    # Simulate a host proceeding without a working hook. Core still denies.
    for index in range(3):
        result = h.core.call("read_device_identity", args | {"operation_id": f"op-{index}"})
        assert result.status == "blocked" and result.error.code == "policy_denied"
    assert h.transport.count("read_device_identity") == 0


def test_readonly_reader_does_not_recover_or_create_files(setup_hooks):
    h = setup_hooks
    run = ready(h)
    state = h.store.load_state(run)
    state["pending_operation"] = {"operation_id": "pending", "tool": "inspect_target"}
    state["remote_state"] = "running"
    h.store.save_state(run, state)
    before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in h.store.root.rglob("*") if path.is_file()}
    with RunStore.open_reader(h.store.root) as reader:
        assert reader.load_state(run) == state
        assert reader.load_report(run) and reader.read_events(run)
        mutations = [lambda: reader.save_state(run, {}), lambda: reader.save_report(run, {}),
                     lambda: reader.create("new", {}), lambda: reader.request_cancel(run),
                     lambda: reader.append_event(run, {}), lambda: reader.quarantine_target("lab-pi", "unknown"),
                     lambda: reader.recover_pending(run), lambda: reader.recover_target("lab-pi")]
        for mutate in mutations:
            with pytest.raises(StorageError, match="read-only"):
                mutate()
    after = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in h.store.root.rglob("*") if path.is_file()}
    assert before == after
    with pytest.raises(FileNotFoundError):
        RunStore.open_reader(h.store.root.parent / "absent")
    assert not (h.store.root.parent / "absent").exists()


@pytest.mark.parametrize("layout", ["symlink", "permissions", "hardlink"])
def test_readonly_reader_preserves_storage_safety_checks(setup_hooks, tmp_path, layout):
    h = setup_hooks
    run = ready(h)
    path = h.store.root / "runs" / run / "state.json"
    if layout == "symlink":
        link = tmp_path / "alias"
        link.symlink_to(h.store.root)
        with pytest.raises(OSError):
            RunStore.open_reader(link)
    elif layout == "permissions":
        h.store.root.chmod(0o755)
        with pytest.raises(StorageError):
            RunStore.open_reader(h.store.root)
    else:
        (path.parent / "link.json").hardlink_to(path)
        with RunStore.open_reader(h.store.root) as reader, pytest.raises(StorageError):
            reader.load_state(run)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'[]', b'{"x":NaN}', b'bad'])
def test_payload_parser_rejects_ambiguous_json(raw):
    with pytest.raises(ValueError):
        decode(raw)


@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse", "Stop"])
def test_cli_emits_only_safe_json_for_malformed_input(tmp_path, event):
    result = subprocess.run([sys.executable, "-I", "-m", "embedsleuth.hooks", "--event", event,
                             "--config", str(tmp_path / "missing"), "--hook-state", str(tmp_path / "state")],
                            input=b'{"private":"secret-sentinel",', capture_output=True, timeout=5, check=True)
    assert json.loads(result.stdout) and result.stderr == b""
    assert b"secret-sentinel" not in result.stdout


def test_overlapping_store_roots_rejected_before_hook_mutation(setup_hooks):
    h = setup_hooks
    h.journal = h.store.root
    data = {"target_id": "lab-pi", "profile_id": "mpu6050"}
    assert hook(h, "PreToolUse", "start_diagnosis", data)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert h.store.list_runs() == []


def test_stop_checks_overlapping_roots_before_journal_startup_recovery(setup_hooks):
    from embedsleuth.hooks import _context
    h = setup_hooks
    run = ready(h)
    state = h.store.load_state(run)
    state["remote_state"] = "running"
    state["pending_operation"] = {"operation_id": "unfinished", "tool": "inspect_target"}
    h.store.save_state(run, state)
    # Simulate a misplaced old hook journal alongside diagnostic records.
    path = h.store.root / (_context(payload("Stop")) + ".json")
    path.write_text(json.dumps({"version": 1, "calls": {}, "runs": [run], "warned": False}))
    path.chmod(0o600)
    h.journal = h.store.root
    before = {str(path): path.read_bytes() for path in h.store.root.rglob("*") if path.is_file()}
    assert "systemMessage" in hook(h, "Stop")
    after = {str(path): path.read_bytes() for path in h.store.root.rglob("*") if path.is_file()}
    assert before == after and h.store.load_state(run) == state


def test_cleanup_calls_remain_available_after_ordinary_hook_budget(setup_hooks):
    h = setup_hooks
    run = h.core.start_diagnosis("lab-pi")["run_id"]
    args = {"run_id": run}
    for index in range(MAX_CALLS):
        assert hook(h, "PreToolUse", "get_report", args, f"call-{index}") == {}
    denied = hook(h, "PreToolUse", "inspect_target", args, "over-budget")
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert hook(h, "PreToolUse", "cancel_diagnosis", args, "cleanup") == {}


def test_parallel_start_reservations_are_bounded(setup_hooks):
    h = setup_hooks
    args = {"target_id": "lab-pi", "profile_id": "mpu6050"}
    for index in range(MAX_RUNS):
        assert hook(h, "PreToolUse", "start_diagnosis", args, f"start-{index}") == {}
    assert hook(h, "PreToolUse", "start_diagnosis", args, "extra")["hookSpecificOutput"]["permissionDecision"] == "deny"
    run = h.core.start_diagnosis("lab-pi")["run_id"]
    assert hook(h, "PreToolUse", "inspect_target", {"run_id": run}, "remote")["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_report_only_query_does_not_activate_stop(setup_hooks):
    h = setup_hooks
    run = ready(h)
    assert hook(h, "PreToolUse", "get_report", {"run_id": run}) == {}
    assert hook(h, "Stop") == {}  # Even a missing query result is unrelated to an active diagnosis.


def test_duplicate_post_is_advisory_and_never_reexecutes(setup_hooks):
    from embedsleuth.mcp_server import _response
    h = setup_hooks
    run = h.core.start_diagnosis("lab-pi")["run_id"]
    args = {"run_id": run}
    assert hook(h, "PreToolUse", arguments=args) == {}
    raw = _response(h.core.inspect_target(run)).model_dump(mode="json", by_alias=True)
    assert hook(h, "PostToolUse", arguments=args, tool_response=raw) == {}
    assert "systemMessage" in hook(h, "PostToolUse", arguments=args, tool_response=raw)
    assert len(h.transport.calls) == 1


@pytest.mark.parametrize("value", [[], "invalid", True])
def test_malformed_start_result_returns_safe_warning(setup_hooks, value):
    from embedsleuth.mcp_server import _response
    h = setup_hooks
    args = {"target_id": "lab-pi", "profile_id": "mpu6050"}
    assert hook(h, "PreToolUse", "start_diagnosis", args) == {}
    raw = _response(value).model_dump(mode="json", by_alias=True)
    assert "systemMessage" in hook(h, "PostToolUse", "start_diagnosis", args, tool_response=raw)


def test_corrupt_journal_warns_without_crash_or_diagnostic_recovery(setup_hooks):
    from embedsleuth.hooks import _Journal, _context
    h = setup_hooks
    data = payload("Stop")
    with _Journal(h.journal) as journal:
        journal.put(_context(data), {"version": 1, "calls": [], "runs": [], "warned": False})
    assert "systemMessage" in run_hook("Stop", data, h.config, h.journal)
    assert h.store.list_runs() == []


def test_renderer_quotes_paths_and_never_changes_approval_policy(tmp_path):
    import shlex
    root = tmp_path / "spaces ' and $(literal)"
    definitions = codex_hook_definitions(root / "python", root / "targets.json", root / "journal")
    assert set(definitions) == {"hooks"}
    for event, groups in definitions["hooks"].items():
        assert len(groups) == 1 and groups[0]["hooks"][0]["timeout"] == 5
        words = shlex.split(groups[0]["hooks"][0]["command"])
        assert words == [str(root / "python"), "-I", "-m", "embedsleuth.hooks", "--event", event,
                         "--config", str(root / "targets.json"), "--hook-state", str(root / "journal")]
        assert groups[0].get("matcher") == (None if event == "Stop" else "^mcp__embedsleuth__.*$")


def test_busy_journal_denies_without_waiting_or_hardware_access(setup_hooks):
    from embedsleuth.hooks import _Journal, _context
    h = setup_hooks
    data = payload("PreToolUse", "start_diagnosis", {"target_id": "lab-pi", "profile_id": "mpu6050"})
    with _Journal(h.journal) as journal, journal._lock("hook", _context(data)):
        result = run_hook("PreToolUse", data, h.config, h.journal)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert h.transport.calls == []


def test_changed_snapshot_is_reported_without_repair(setup_hooks, monkeypatch):
    from embedsleuth.mcp_server import _response
    h = setup_hooks
    run = h.core.start_diagnosis("lab-pi")["run_id"]
    args = {"run_id": run}
    assert hook(h, "PreToolUse", arguments=args) == {}
    raw = _response(h.core.inspect_target(run)).model_dump(mode="json", by_alias=True)
    original = RunStore.load_state
    reads = []

    def changing(reader, run_id):
        state = original(reader, run_id)
        if reader._read_only:
            reads.append(True)
            if len(reads) >= 2:
                state["remote_calls"] += 1
        return state

    monkeypatch.setattr(RunStore, "load_state", changing)
    assert "systemMessage" in hook(h, "PostToolUse", arguments=args, tool_response=raw)
    assert h.transport.count("inspect_target") == 1


def test_pending_cancellation_is_preserved_as_warning_not_hidden_or_retried(setup_hooks):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from embedsleuth.mcp_server import _response
    h = setup_hooks
    run = h.core.start_diagnosis("lab-pi")["run_id"]
    args = {"run_id": run}
    entered, release = Event(), Event()
    execute = h.transport.execute

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return execute(*args, **kwargs)

    h.transport.execute = delayed
    assert hook(h, "PreToolUse", arguments=args) == {}
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(h.core.inspect_target, run)
        try:
            assert entered.wait(5)
            assert hook(h, "PreToolUse", "cancel_diagnosis", args, "cancel") == {}
            report = h.core.cancel_diagnosis(run)
            raw = _response(report).model_dump(mode="json", by_alias=True)
            assert report.outcome == "interrupted"
            output = hook(h, "PostToolUse", "cancel_diagnosis", args, "cancel", tool_response=raw)
            assert "systemMessage" in output and "decision" not in output
            assert raw["structuredContent"]["result"]["outcome"] == "interrupted"
        finally:
            release.set()
        future.result(timeout=5)
    assert h.transport.count("inspect_target") == 1


from test_skill_scenarios import CASES


@pytest.mark.parametrize("case", CASES["offline_replays"], ids=lambda case: case["id"])
def test_reviewed_failure_and_success_traces_with_hooks(make_harness, tmp_path, case):
    from embedsleuth.transport import ProbeResponse
    fixture = deepcopy(case["fixture"])
    changes = fixture.get("target", {})
    if "expected_addresses" in changes:
        changes["expected_addresses"] = tuple(changes["expected_addresses"])
    h = make_harness(target_changes=changes)
    h.config, h.journal = tmp_path / "targets.json", tmp_path / "journal"
    h.config.write_text(json.dumps({"schema_version": "1.0", "state_directory": str(h.store.root),
                                    "targets": [json.loads(h.target.model_dump_json())]}))
    h.config.chmod(0o600)
    h.transport.overrides.update(fixture.get("observations", {}))
    h.transport.overrides.update({name: ProbeResponse(**value) for name, value in fixture.get("responses", {}).items()})
    variables = {}

    def resolve(value):
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return variables[value] if isinstance(value, str) and value.startswith("$") else value

    async def check():
        runtime = MCPRuntime(h.core)
        try:
            for index, step in enumerate(case["trace"]):
                if "advance_seconds" in step:
                    h.clock.now += step["advance_seconds"]
                    continue
                tool, args = step["tool"], resolve(step["arguments"])
                result = await invoke(h, runtime, tool, args, f"step-{index}")
                if tool == "start_diagnosis":
                    variables["$run_id"] = result["run_id"]
                if tool == "read_device_identity":
                    variables["$identity_evidence"] = result["evidence_ids"][0]
            assert hook(h, "Stop") == {}
        finally:
            await runtime.close()

    asyncio.run(check())
    assert h.transport.count("read_device_identity") == case["expected"]["identity_reads"]

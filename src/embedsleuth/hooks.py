"""Optional Codex command hooks. No SSH, subprocesses, model or core calls.

Captures only hashed call receipts and run IDs in a separate private journal.
Diagnostic storage is opened without mutation or crash recovery. The core is
the safety boundary even when these advisory checks are missing or fail.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys

from embedsleuth.config import load_configuration
from embedsleuth.contracts import DiagnosticError, DiagnosticReport, ToolResult
from embedsleuth.profiles import load_component_profile
from embedsleuth.storage import RunStore
from embedsleuth.targets import TargetRegistry
from embedsleuth.tools import TOOL_REQUEST_MODELS, validate_arguments


PREFIX = "mcp__embedsleuth__"
EVENTS = ("PreToolUse", "PostToolUse", "Stop")
MAX_INPUT = 8 * 1024 * 1024
MAX_CALLS = 64
MAX_CLEANUP_CALLS = 16
MAX_RUNS = 8
TERMINAL = {"reported", "blocked", "expired", "cancelled"}
REMOTE = {"inspect_target", "inspect_i2c_environment", "read_device_identity"}
REPAIR = ("EmbedSleuth could not verify closure of this turn's diagnostic records. "
          "Use finalize_report/get_report for existing evidence, or explain the interruption "
          "and unresolved limitations. Do not repeat hardware checks to satisfy this hook.")
WARNING = ("EmbedSleuth hook verification is incomplete. Preserve the original tool result, "
           "report uncertainty, and review existing evidence; this hook grants no approval.")


def decode(data):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def invalid(_):
        raise ValueError("non-finite JSON number")

    value = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
    if type(value) is not dict:
        raise ValueError("JSON object required")
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


def codex_hook_definitions(python: Path, config: Path, journal: Path):
    """Render Linux command definitions for human review; does not install/trust."""
    if any(not path.is_absolute() or ".." in path.parts for path in (python, config, journal)):
        raise ValueError("absolute hook paths required")
    events = {}
    for event in EVENTS:
        command = shlex.join([str(python), "-I", "-m", "embedsleuth.hooks", "--event", event,
                              "--config", str(config), "--hook-state", str(journal)])
        group = {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
        if event != "Stop":
            group["matcher"] = "^mcp__embedsleuth__.*$"
        events[event] = [group]
    return {"hooks": events}


def denial():
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "EmbedSleuth hook could not validate this diagnostic request. "
                                        "Check registered target, run and tool arguments."}}


def advisory(event):
    result = {"systemMessage": WARNING}
    if event == "PostToolUse":
        result["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": WARNING}
    return result


def _context(payload):
    values = [payload.get("session_id"), payload.get("turn_id")]
    if any(type(value) is not str or not 1 <= len(value) <= 200 for value in values):
        raise ValueError("missing bounded turn context")
    return digest(values)


def _receipt(payload):
    value = payload.get("tool_use_id")
    if type(value) is not str or not 1 <= len(value) <= 200:
        raise ValueError("missing bounded call context")
    return digest(value)


class _Journal(RunStore):
    """Reuse private atomic storage primitives; journal entries are not runs."""

    def entry(self, context):
        try:
            entry = self._decode(self._read_file(self._root_fd, context + ".json", limit=65536))
        except FileNotFoundError:
            return {"version": 1, "calls": {}, "runs": [], "warned": False}
        if (set(entry) != {"version", "calls", "runs", "warned"} or type(entry["version"]) is not int
                or entry["version"] != 1 or type(entry["warned"]) is not bool
                or type(entry["calls"]) is not dict or len(entry["calls"]) > MAX_CALLS + MAX_CLEANUP_CALLS
                or type(entry["runs"]) is not list or len(entry["runs"]) > MAX_RUNS):
            raise ValueError("invalid hook journal")
        for call_id, call in entry["calls"].items():
            if (re.fullmatch("[a-f0-9]{64}", call_id) is None or type(call) is not dict
                    or not {"tool", "arguments_digest", "done"}.issubset(call)
                    or set(call) - {"tool", "arguments_digest", "done", "issue"}
                    or type(call["tool"]) is not str or call["tool"] not in TOOL_REQUEST_MODELS
                    or type(call["done"]) is not bool or type(call.get("issue", False)) is not bool
                    or type(call["arguments_digest"]) is not str
                    or re.fullmatch("[a-f0-9]{64}", call["arguments_digest"]) is None):
                raise ValueError("invalid hook receipt")
        for run_id in entry["runs"]:
            validate_arguments("get_report", {"run_id": run_id})
        return entry

    def put(self, context, entry):
        self._atomic_json(self._root_fd, context + ".json", entry)


class HookChecks:
    def __init__(self, config: Path, journal: Path):
        self.config_path, self.journal_path = config, journal

    def configuration(self):
        config = load_configuration(self.config_path)
        # Disjoint absolute roots prevent hook initialization/recovery from
        # touching diagnostic storage, including a mistakenly shared root.
        paths = (config.state_directory, self.journal_path)
        if any(not path.is_absolute() or ".." in path.parts for path in paths):
            raise ValueError("invalid roots")
        if paths[0] in (paths[1], *paths[1].parents) or paths[1] in paths[0].parents:
            raise ValueError("overlapping roots")
        return config

    @contextmanager
    def snapshot(self, run_id, config):
        with RunStore.open_reader(config.state_directory) as store:
            state = store.load_state(run_id)
            target = TargetRegistry(config.targets).get(state["target_id"])
            if (state["run_id"] != run_id or state["profile_id"] != target.profile_id
                    or state["mode"] not in {"read_only", "passive"}):
                raise ValueError("invalid run binding")
            yield store, state
            if store.load_state(run_id) != state:
                raise ValueError("diagnostic snapshot changed")

    @staticmethod
    def report(store, state):
        report = DiagnosticReport.model_validate_json(json.dumps(store.load_report(state["run_id"])))
        if (report.run_id != state["run_id"] or report.target_id != state["target_id"]
                or [json.loads(item.model_dump_json()) for item in report.evidence] != state["evidence"]):
            raise ValueError("report differs from durable evidence")
        return report

    @staticmethod
    def audit(store, state, result, tool=None):
        events = [event for event in store.read_events(state["run_id"])
                  if event.get("operation_id") == result.request_id]
        # Preflight denials have evidence without a remote dispatch intent.
        if not events and result.status == "blocked":
            return
        if (len(events) != 2 or [event.get("action") for event in events] != ["before", "after"]
                or events[0].get("tool") not in REMOTE
                or (tool is not None and events[0]["tool"] != tool)
                or events[1].get("status") != result.status):
            raise ValueError("incomplete remote audit")

    def preflight(self, tool, arguments, config):
        request = validate_arguments(tool, arguments)
        if tool == "get_component_profile":
            load_component_profile(request.profile_id)
        elif tool == "start_diagnosis":
            target = TargetRegistry(config.targets).get(request.target_id)
            if target.profile_id != request.profile_id:
                raise ValueError("unregistered component")
            load_component_profile(request.profile_id)
        else:
            with self.snapshot(request.run_id, config):
                pass
        # Intentionally no policy decision, approval, environment fabrication
        # or terminal replay restriction: the core handles all of these.

    @staticmethod
    def envelope(raw):
        if type(raw) is not dict:
            raise ValueError("missing MCP result")
        content = raw.get("content")
        if (type(content) is not list or len(content) != 1 or type(content[0]) is not dict
                or content[0].get("type") != "text" or type(content[0].get("text")) is not str):
            raise ValueError("invalid MCP text result")
        body = decode(content[0]["text"])
        if "structuredContent" in raw and raw["structuredContent"] != body:
            raise ValueError("MCP representations differ")
        if set(body) != {"schema_version", "result", "error"} or body["schema_version"] != "1.0":
            raise ValueError("invalid result envelope")
        is_error = raw.get("isError", False)
        if type(is_error) is not bool or is_error != (body["error"] is not None):
            raise ValueError("inconsistent MCP error")
        if body["error"] is not None:
            DiagnosticError.model_validate_json(json.dumps(body["error"]))
        if body["result"] is None and body["error"] is None:
            raise ValueError("empty successful result")
        return body

    def verify_result(self, tool, arguments, raw, config):
        body = self.envelope(raw)
        value = body["result"]
        if value is None:  # An explicit adapter failure is not hardware evidence.
            return None
        if tool not in REMOTE and body["error"] is not None:
            raise ValueError("unexpected result error")
        if tool == "get_component_profile":
            if value != json.loads(load_component_profile(arguments["profile_id"]).model_dump_json()):
                raise ValueError("profile differs")
            return None
        if tool == "start_diagnosis" and type(value) is not dict:
            raise ValueError("invalid start summary")
        run_id = value.get("run_id") if tool == "start_diagnosis" else arguments["run_id"]
        with self.snapshot(run_id, config) as (store, state):
            report = self.report(store, state)
            if tool == "start_diagnosis":
                expected = {key: state[key] for key in ("run_id", "target_id", "profile_id", "mode", "state")}
                if value != expected or value["target_id"] != arguments["target_id"]:
                    raise ValueError("start differs")
            elif tool in REMOTE:
                result = ToolResult.model_validate_json(json.dumps(value))
                expected_error = None if result.error is None else json.loads(result.error.model_dump_json())
                if body["error"] != expected_error:
                    raise ValueError("tool and envelope errors differ")
                records = {item.evidence_id: item.result for item in report.evidence}
                if not all(records.get(ref) == result for ref in result.evidence_ids):
                    raise ValueError("result evidence not persisted")
                self.audit(store, state, result, tool)
            elif tool == "evaluate_findings":
                if value != [json.loads(item.model_dump_json()) for item in report.findings]:
                    raise ValueError("findings differ")
            elif value != json.loads(report.model_dump_json()):
                # Concurrent cancellation can acknowledge a marker before a
                # worker closes its report. Preserve that response and warn.
                raise ValueError("returned report differs from durable snapshot")
        return run_id

    def handle(self, event, payload):
        if event not in EVENTS or payload.get("hook_event_name") != event:
            raise ValueError("unexpected hook event")
        if event == "Stop":
            return self.stop(payload)
        canonical = payload.get("tool_name")
        if type(canonical) is not str:
            raise ValueError("missing tool name")
        if not canonical.startswith(PREFIX):
            return {}  # No files/config read for other tools.
        tool = canonical[len(PREFIX):]
        arguments = payload.get("tool_input")
        if tool not in TOOL_REQUEST_MODELS or type(arguments) is not dict:
            raise ValueError("unknown tool or malformed arguments")
        validate_arguments(tool, arguments)
        if tool == "get_component_profile":
            if event == "PreToolUse":
                load_component_profile(arguments["profile_id"])
            else:
                self.verify_result(tool, arguments, payload.get("tool_response"), None)
            return {}
        config = self.configuration()
        context, receipt = _context(payload), _receipt(payload)
        stamp = {"tool": tool, "arguments_digest": digest(arguments), "done": False}
        with _Journal(self.journal_path) as journal, journal._lock("hook", context):
            entry = journal.entry(context)
            if event == "PreToolUse":
                self.preflight(tool, arguments, config)
                limit = MAX_CALLS + (MAX_CLEANUP_CALLS if tool in {
                    "finalize_report", "get_report", "cancel_diagnosis"} else 0)
                if receipt in entry["calls"] or len(entry["calls"]) >= limit:
                    raise ValueError("duplicate call or hook budget exhausted")
                run_id = arguments.get("run_id")
                pending_starts = sum(call["tool"] == "start_diagnosis" and not call["done"]
                                     for call in entry["calls"].values())
                # Merely viewing an old report does not activate Stop checks.
                if tool in REMOTE and run_id not in entry["runs"]:
                    if len(entry["runs"]) + pending_starts >= MAX_RUNS:
                        raise ValueError("hook run budget exhausted")
                    entry["runs"].append(run_id)
                if tool == "start_diagnosis" and len(entry["runs"]) + pending_starts >= MAX_RUNS:
                    raise ValueError("hook run budget exhausted")
                entry["calls"][receipt] = stamp
                journal.put(context, entry)
                return {}  # Never emit permissionDecision=allow or updatedInput.
            previous = entry["calls"].get(receipt)
            if previous != stamp:
                raise ValueError("missing, duplicate or mismatched call receipt")
            try:
                run_id = self.verify_result(tool, arguments, payload.get("tool_response"), config)
            except (OSError, ValueError, KeyError, TypeError):
                previous["issue"] = True
                journal.put(context, entry)
                raise
            previous["done"] = True
            previous.pop("issue", None)
            if tool == "start_diagnosis" and run_id is not None and run_id not in entry["runs"]:
                entry["runs"].append(run_id)
            journal.put(context, entry)
        return {}

    def stop(self, payload):
        context = _context(payload)
        try:
            # Unrelated Stop must not initialize storage or load target config.
            with _Journal.open_reader(self.journal_path) as journal:
                entry = journal.entry(context)
        except FileNotFoundError:
            return {}
        if not entry["runs"] and not any(
            call["tool"] == "start_diagnosis" for call in entry["calls"].values()
        ):
            return {}
        # Validate disjoint roots before *any* writable journal constructor:
        # startup recovery must never run on a misconfigured diagnostic root.
        config = self.configuration()
        with _Journal(self.journal_path) as journal, journal._lock("hook", context):
            entry = journal.entry(context)
            try:
                incomplete = [call for call in entry["calls"].values() if not call["done"]]
                if any(call["tool"] not in REMOTE or call.get("issue") for call in incomplete):
                    raise ValueError("unverified call")
                for run_id in entry["runs"]:
                    with self.snapshot(run_id, config) as (store, state):
                        report = self.report(store, state)
                        if state["state"] not in TERMINAL or state.get("pending_operation"):
                            raise ValueError("unclosed run")
                        for record in report.evidence:
                            self.audit(store, state, record.result)
                if incomplete:
                    # A host can omit PostToolUse on an error. Once every run
                    # and durable remote audit is closed, another get_report
                    # cannot recreate that missing event. Preserve receipts as
                    # unverified and warn, without requesting a futile repair.
                    return advisory("Stop")
            except (OSError, ValueError, KeyError, TypeError):
                if payload.get("stop_hook_active") is True or entry["warned"]:
                    return advisory("Stop")
                entry["warned"] = True
                journal.put(context, entry)  # Durable one-shot guard before block.
                return {"decision": "block", "reason": REPAIR}
        return {}


def run_hook(event, payload, config, journal):
    """Return only fixed safe diagnostics; never reflect raw errors or input."""
    try:
        if type(payload) is not dict:
            raise ValueError("hook payload must be an object")
        return HookChecks(config, journal).handle(event, payload)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return denial() if event == "PreToolUse" else advisory(event)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", choices=EVENTS, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--hook-state", type=Path, required=True)
    args = parser.parse_args()
    try:
        data = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(data) > MAX_INPUT:
            raise ValueError("hook payload exceeds budget")
        payload = decode(data)
        result = run_hook(args.event, payload, args.config, args.hook_state)
    except (ValueError, OSError, RecursionError):
        result = denial() if args.event == "PreToolUse" else advisory(args.event)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()

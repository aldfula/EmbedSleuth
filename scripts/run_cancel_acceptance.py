"""Explicit opt-in in-flight cancellation of a passive production MCP check.

Leaves any resulting quarantine intact. Extra SSH observes process metadata
only; hardware inspection and cancellation use the unmodified production MCP.
"""
import argparse
import asyncio
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import time

from mcp import Client
from mcp.client.stdio import StdioServerParameters
import paramiko

from embedsleuth.config import load_configuration
from embedsleuth.contracts import DiagnosticReport
from embedsleuth.storage import RunStore
from embedsleuth.targets import TargetRegistry

ROOT = Path(__file__).resolve().parents[1]


def grade(report, state, quarantined, pending_at_cancel, observation, events):
    before = [e for e in events if e.get("action") == "before"]
    started = {e["operation_id"] for e in before}
    closed = {e["operation_id"] for e in events if e.get("action") == "after"}
    audited = bool(started) and started == closed
    captured = observation.get("captured_processes", [])
    exited = bool(captured) and all(x.get("present_at_end") is False
                                   and x.get("readable_at_end") is True for x in captured)
    interrupted = (report["outcome"] == "interrupted" and state["pending_operation"] is None
                   and state["remote_state"] == "unknown" and quarantined is True)
    exercised = pending_at_cancel is True and observation.get("probe_seen") is True
    return {"assessment": "pass" if interrupted and exercised and exited and audited else "inconclusive",
            "run_id": report["run_id"], "report_outcome": report["outcome"],
            "inspection_pending_at_cancel": pending_at_cancel,
            "exact_remote_probe_observed": observation.get("probe_seen") is True,
            "captured_process_exit_confirmed": exited, "target_quarantined": quarantined,
            "pending_operation": state["pending_operation"], "remote_state": state["remote_state"],
            "identity_reads": sum(e.get("tool") == "read_device_identity" for e in before),
            "remote_audit_closed": audited,
            "remote_running_at_cancel_receipt": "unknown",
            "whole_process_tree_exit": "unknown"}


def read_event(stream):
    line = stream.readline(65536)
    if not line.endswith("\n") or len(line) >= 65536:
        raise ValueError("observer response unavailable")
    return json.loads(line)


async def exercise(config_path, target, stdout):
    parameters = StdioServerParameters(command=sys.executable,
        args=["-I", "-m", "embedsleuth.mcp_server", "--config", str(config_path)])
    run_id, pending, cancel_sent = None, None, False
    async with Client(parameters, read_timeout_seconds=20) as client:
        async def call(name, **args):
            r = await client.call_tool(name, args)
            body = r.structured_content
            if (type(body) is not dict or json.loads(r.content[0].text) != body
                    or body.get("schema_version") != "1.0"):
                raise ValueError("invalid MCP response")
            return body
        try:
            await call("get_component_profile", profile_id=target.profile_id)
            body = await call("start_diagnosis", target_id=target.target_id,
                              profile_id=target.profile_id, mode="passive")
            if body["error"]:
                raise ValueError("diagnosis did not start")
            run_id = body["result"]["run_id"]
            pending = asyncio.create_task(call("inspect_target", run_id=run_id))
            event = await asyncio.to_thread(read_event, stdout)
            if event.get("event") != "production_probe_seen":
                raise ValueError("observer did not see the exact production probe")
            pending_at_cancel = not pending.done()
            started = time.monotonic()
            cancel_sent = True
            cancellation = await call("cancel_diagnosis", run_id=run_id)
            cancel_ms = round((time.monotonic()-started)*1000, 3)
            inspection = await pending  # Drain the real worker; never repeat inspection.
            body = await call("get_report", run_id=run_id)
            if body["error"] or body["result"] is None:
                raise ValueError("durable report unavailable")
            report = DiagnosticReport.model_validate_json(json.dumps(body["result"]))
            final = await asyncio.to_thread(read_event, stdout)
            if final.get("event") != "final":
                raise ValueError("final observation unavailable")
            return report.model_dump(mode="json"), pending_at_cancel, final, {
                "cancel_duration_ms": cancel_ms, "cancel_response_error": cancellation["error"],
                "inspection_error": inspection["error"], "probe_observation": event}
        finally:
            if run_id and not cancel_sent:
                await call("cancel_diagnosis", run_id=run_id)
            if pending is not None:
                await pending
            if run_id:
                await call("get_report", run_id=run_id)


def run(config_path, target_id):
    config = load_configuration(config_path)
    target = TargetRegistry(config.targets).get(target_id)
    with RunStore.open_reader(config.state_directory) as store:
        if store.is_quarantined(target_id):
            raise ValueError("target already quarantined")
    observer = (ROOT / "evals/diagnose-mpu6050/cancellation_observer.py").read_text()
    digest = hashlib.sha256(files("embedsleuth").joinpath("remote_probe.py").read_bytes()).hexdigest()
    ssh = paramiko.SSHClient()
    ssh.load_host_keys(str(target.known_hosts_path))
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        ssh.connect(hostname=target.host, port=target.port, username=target.username,
                    key_filename=str(target.key_path) if target.key_path else None,
                    allow_agent=target.key_path is None, look_for_keys=False,
                    timeout=8, auth_timeout=8, banner_timeout=8, channel_timeout=20)
        stdin, stdout, stderr = ssh.exec_command("python3 -I -B -c " + shlex.quote(observer), timeout=20)
        stdin.write(digest + "\n"); stdin.flush(); stdin.close()
        if read_event(stdout) != {"ready": True}:
            raise ValueError("observer not ready")
        report, pending, observation, timing = asyncio.run(exercise(config_path, target, stdout))
        if stdout.channel.recv_exit_status() != 0 or stderr.read(4096):
            raise ValueError("observer failed")
        with RunStore.open_reader(config.state_directory) as store:
            state, saved = store.load_state(report["run_id"]), store.load_report(report["run_id"])
            quarantined = store.is_quarantined(target_id)
            events = store.read_events(report["run_id"])
            if report != saved:
                raise ValueError("durable report mismatch")
        return {"checks": grade(report, state, quarantined, pending, observation, events),
                "timing": timing, "observation": observation}
    finally:
        ssh.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--confirm-quarantine", required=True, action="store_true")
    args = parser.parse_args()
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        def deadline(*_):
            raise TimeoutError("cancellation acceptance deadline")
        old = signal.signal(signal.SIGALRM, deadline)
        signal.alarm(45)
        try:
            result = run(args.config, args.target)
        except Exception as e:
            json.dump({"assessment": "unavailable", "error_kind": type(e).__name__,
                       "next_action": "Review the existing report and preserve quarantine; no hardware retry"}, output)
            output.write("\n")
            raise SystemExit("Cancellation acceptance unavailable; review existing report") from None
        finally:
            signal.alarm(0); signal.signal(signal.SIGALRM, old)
        json.dump(result, output, indent=2); output.write("\n")
    print(json.dumps(result["checks"], indent=2))
    return 0 if result["checks"]["assessment"] == "pass" else 3


if __name__ == "__main__":
    raise SystemExit(main())

"""Opt-in /proc observation alongside passive production MCP acceptance.

Only this development observer uses an additional SSH session. Hardware
inspection remains in the production MCP, with the original target/store.
There is no remote write, device transfer, retry or quarantine reset.
"""

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import sys

import paramiko

from embedsleuth.config import load_configuration
from embedsleuth.storage import RunStore
from embedsleuth.targets import TargetRegistry


ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def check_result(measurement, report, status):
    observer = module("process_observer", ROOT / "evals/diagnose-mpu6050/process_observer.py")
    exits = []
    for item in report["evidence"]:
        metrics = item["result"]["observations"].get("resource_usage", {})
        exits.append({"evidence_id": item["evidence_id"], "assessment": observer.probe_exit_status(
            metrics.get("pid"), metrics.get("process_start_ticks"),
            measurement["final_process_keys"], measurement["unreadable_pids"])})
    return {
        "run_id": report["run_id"], "report_outcome": report["outcome"],
        "observer_exit_status": status, "observation": measurement,
        "probe_exit_checks": exits,
        "system_incremental_peak_assessment": "unknown",
        "sampled_64mib_exceeded": measurement["peak_observed_kib"] > 65536,
        "full_process_tree_exit_assessment": "unknown",
    }


def run(config_path, target_id):
    config = load_configuration(config_path)
    target = TargetRegistry(config.targets).get(target_id)
    with RunStore.open_reader(config.state_directory) as store:
        if store.is_quarantined(target_id):
            raise ValueError("quarantined target")
    source = (ROOT / "evals/diagnose-mpu6050/process_observer.py").read_text()
    client = paramiko.SSHClient()
    client.load_host_keys(str(target.known_hosts_path))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(hostname=target.host, port=target.port, username=target.username,
                       key_filename=str(target.key_path) if target.key_path else None,
                       allow_agent=target.key_path is None, look_for_keys=False,
                       timeout=8, auth_timeout=8, banner_timeout=8, channel_timeout=25)
        stdin, stdout, stderr = client.exec_command("python3 -I -B -c " + shlex.quote(source), timeout=25)
        stdin.close()
        if stdout.readline(256) != '{"ready": true}\n':
            raise ValueError("observer did not become ready")
        hardware = module("hardware_acceptance", ROOT / "scripts/run_hardware_acceptance.py")
        acceptance = asyncio.run(hardware.run(config_path, target_id))
        line = stdout.readline(256 * 1024)
        if not line.endswith("\n") or len(line) >= 256 * 1024:
            raise ValueError("observer output unavailable or oversized")
        measurement = json.loads(line)
        status = stdout.channel.recv_exit_status()
        if status != 0 or stderr.read(4096):
            raise ValueError("observer did not complete cleanly")
        with RunStore.open_reader(config.state_directory) as store:
            report = store.load_report(acceptance["checks"]["run_id"])
        return {"passive_acceptance": acceptance, "process_checks": check_result(measurement, report, status)}
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    # Refuse overwrite before opening either network connection.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        def deadline(*_):
            raise TimeoutError("Process acceptance deadline expired")
        previous_handler = signal.signal(signal.SIGALRM, deadline)
        signal.alarm(45)
        try:
            result = run(args.config, args.target)
        except (OSError, ValueError, KeyError, paramiko.SSHException):
            json.dump({"status": "unavailable", "may_have_started_passive_run": True,
                       "next_action": "Review existing diagnostic evidence; do not retry automatically"}, output)
            output.write("\n")
            raise SystemExit("Process acceptance unavailable; review saved evidence") from None
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)
        json.dump(result, output, indent=2, ensure_ascii=False)
        output.write("\n")
    compact = dict(result["process_checks"])
    compact["observation"] = {k: v for k, v in compact["observation"].items() if k != "final_process_keys"}
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

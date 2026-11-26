"""Production MCP check alongside read-only kernel memory.peak observation.

Uses existing registration and store; refuses quarantine. No counter reset,
sudo, process migration, device-access substitute or automatic hardware retry.
"""

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal

from embedsleuth.config import load_configuration
from embedsleuth.storage import RunStore
from embedsleuth.targets import TargetRegistry

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def grade(measurement, report):
    observer = module("cgroup_observer", "evals/diagnose-mpu6050/cgroup_observer.py")
    bound = observer.bounds(measurement["before"], measurement["after"])
    memberships = []
    for row in report["evidence"]:
        observations = row["result"]["observations"]
        try:
            path = observer.group_path(observations.get("process_cgroup", ""))
            memberships.append({"evidence_id": row["evidence_id"], "path": path,
                                "covered": observer.covered(path),
                                "same_boot": bool(measurement.get("boot_id"))
                                and observations.get("boot_id") == measurement["boot_id"]})
        except (ValueError, TypeError, AttributeError):
            memberships.append({"evidence_id": row["evidence_id"], "path": None, "covered": False,
                                "same_boot": False})
    ssh = measurement["ssh_memberships"]
    coverage = (bool(memberships) and all(x["covered"] and x["same_boot"] for x in memberships)
                and bool(ssh["groups"]) and not ssh["unreadable_pids"]
                and all(observer.covered(path) for path in ssh["groups"]))
    return {"run_id": report["run_id"], "report_outcome": report["outcome"],
            "charged_memory_bound_bytes": bound,
            "charged_memory_64mib_assessment": "pass" if coverage and bound <= 64*1024*1024 else "unknown",
            "observed_membership_coverage": coverage, "probe_memberships": memberships,
            "bound_exceeds_64mib": bound > 64*1024*1024,
            "system_incremental_peak_rss_assessment": "unknown",
            "scope": "SSH service and user.slice lifetime charged-memory peaks, including observer",
            "limitations": measurement["limitations"]}


def exit_code(result):
    verdicts = [result["acceptance"]["deterministic_checks"],
                result["memory_checks"]["charged_memory_64mib_assessment"]]
    return 1 if "fail" in verdicts else 0 if all(v == "pass" for v in verdicts) else 3


def read_json(stream):
    line = stream.readline(65536)
    if not line.endswith("\n") or len(line) >= 65536:
        raise ValueError("unavailable observer result")
    return json.loads(line)


def run(config_path, target_id, address, exclusive):
    config = load_configuration(config_path)
    target = TargetRegistry(config.targets).get(target_id)
    hardware = module("hardware_acceptance", "scripts/run_hardware_acceptance.py")
    hardware.check_request(config, target_id, address, exclusive)
    with RunStore.open_reader(config.state_directory) as store:
        if store.is_quarantined(target_id):
            raise ValueError("quarantined target; operator recovery required first")
    preparation = module("preparation", "scripts/prepare_memory_acceptance.py")
    ssh = preparation.connect(target)
    try:
        source = (ROOT / "evals/diagnose-mpu6050/cgroup_observer.py").read_text()
        stdin, stdout, stderr = ssh.exec_command("/usr/bin/python3 -I -B -c " + shlex.quote(source), timeout=30)
        ready = read_json(stdout)
        if ready.get("ready") is not True:
            raise ValueError("observer not ready")
        acceptance = asyncio.run(hardware.run(config_path, target_id, address, exclusive))
        stdin.write("finish\n"); stdin.flush(); stdin.close()
        observation = read_json(stdout)
        if stdout.channel.recv_exit_status() != 0 or stderr.read(1024):
            raise ValueError("observer did not exit cleanly")
        with RunStore.open_reader(config.state_directory) as store:
            report = store.load_report(acceptance["checks"]["run_id"])
        if ready["before"] != observation["before"] or ready["boot_id"] != observation["boot_id"]:
            raise ValueError("observer binding changed")
        return {"acceptance": acceptance, "memory_checks": grade(observation, report), "observation": observation}
    finally:
        ssh.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--identity-address", type=lambda x: int(x, 0), choices=(104, 105))
    parser.add_argument("--confirm-exclusive-i2c", action="store_true")
    args = parser.parse_args()
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        def deadline(*_):
            raise TimeoutError("memory acceptance deadline")
        old = signal.signal(signal.SIGALRM, deadline)
        signal.alarm(45)
        try:
            result = run(args.config, args.target, args.identity_address, args.confirm_exclusive_i2c)
        except Exception as exc:
            json.dump({"status": "unavailable", "error_kind": type(exc).__name__,
                       "next_action": "Review existing evidence; no automatic hardware retry"}, output)
            output.write("\n")
            raise
        finally:
            signal.alarm(0); signal.signal(signal.SIGALRM, old)
        json.dump(result, output, indent=2); output.write("\n")
    print(json.dumps(result["memory_checks"], indent=2))
    return exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())

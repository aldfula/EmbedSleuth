"""Interactive, explicitly approved fault tests. Run in the owner's terminal.

The administrator helper only prepares/restores a condition. All device
observations are passive production MCP calls. No passwords are captured.
"""

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import time
from uuid import uuid4

import paramiko

from embedsleuth.config import load_configuration
from embedsleuth.storage import RunStore
from embedsleuth.targets import TargetRegistry


ROOT = Path(__file__).resolve().parents[1]
CASES = ("permission", "driver-bound", "adapter-disabled")


class FaultStatusError(ValueError):
    def __init__(self, status):
        super().__init__("Administrator test condition or restoration unavailable")
        self.status = status


def environment(report):
    candidates = [record["result"]["observations"] for record in report["evidence"]
                  if "adapter_present" in record["result"]["observations"]]
    if len(candidates) != 1:
        raise ValueError("missing environment evidence")
    return candidates[0]


def expected_fault(case, facts):
    if type(facts.get("bus")) is not int or facts["bus"] != 1:
        return False
    if case == "permission":
        return facts.get("adapter_present") is True and facts.get("access_allowed") is False
    if case == "driver-bound":
        return facts.get("adapter_present") is True and facts.get("driver_bound", {}).get("104") is True
    if case == "adapter-disabled":
        return facts.get("adapter_present") is False
    return False


def restored_environment(before, after):
    fields = (
        "bus", "boot_id", "adapter_fingerprint", "adapter_present", "access_allowed",
        "combined_transfer_supported", "dependency_available", "driver_bound")
    return all(field in before and field in after and after[field] == before[field] for field in fields)


def passive(config_path, target_id):
    spec = importlib.util.spec_from_file_location("hardware_acceptance", ROOT / "scripts/run_hardware_acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = asyncio.run(module.run(config_path, target_id))
    config = load_configuration(config_path)
    with RunStore.open_reader(config.state_directory) as store:
        report = store.load_report(result["checks"]["run_id"])
    if any("identity_value" in row["result"]["observations"] for row in report["evidence"]):
        raise ValueError("fault acceptance must remain passive")
    return result, report


def read_status(sftp, path, uid, case):
    try:
        info = sftp.lstat(path)
    except FileNotFoundError:
        return None
    if (info.st_uid != uid or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077 or info.st_size > 4096):
        raise ValueError("unsafe test status")
    with sftp.open(path, "r") as stream:
        value = json.loads(stream.read(4097))
    if value.get("case") != case:
        raise ValueError("test status mismatch")
    return value


def release_guard(sftp, path):
    # The only unprivileged remote write: a unique empty release marker.
    try:
        with sftp.open(path, "wx"):
            pass
    except OSError:
        pass  # The lease remains active; restoration must still be confirmed.


def one_case(config_path, target_id, case):
    config = load_configuration(config_path)
    target = TargetRegistry(config.targets).get(target_id)
    if target.expected_bus != 1 or 104 not in target.expected_addresses or target.key_path is None:
        raise ValueError("approved preparation requires bus 1, address 0x68 and an explicit key")
    with RunStore.open_reader(config.state_directory) as store:
        if store.is_quarantined(target_id):
            raise ValueError("quarantined target")
    baseline, before_report = passive(config_path, target_id)
    before = environment(before_report)
    if (before.get("adapter_present") is not True or before.get("access_allowed") is not True
            or before.get("driver_bound", {}).get("104") is not False):
        raise ValueError("baseline prerequisites unavailable")
    source = (ROOT / "evals/diagnose-mpu6050/fault_guard.py").read_text()
    client, process, sftp = paramiko.SSHClient(), None, None
    client.load_host_keys(str(target.known_hosts_path))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    status = release = None
    try:
        client.connect(hostname=target.host, username=target.username, port=target.port,
                       key_filename=str(target.key_path), allow_agent=False, look_for_keys=False,
                       timeout=8, auth_timeout=8, banner_timeout=8, channel_timeout=10)
        stdin, stdout, stderr = client.exec_command("id -u", timeout=8)
        stdin.close()
        uid_text = stdout.read(32).strip()
        if not uid_text.isdigit() or stdout.channel.recv_exit_status() or stderr.read(512):
            raise ValueError("operator identity unavailable")
        uid = int(uid_text)
        if uid <= 0:
            raise ValueError("non-root SSH account required")
        nonce = uuid4().hex
        status = f"/run/user/{uid}/embedsleuth-fault-{nonce}.json"
        release = f"/run/user/{uid}/embedsleuth-fault-{nonce}.release"
        sftp = client.open_sftp()
        sftp.get_channel().settimeout(8)
        remote = shlex.join(["/usr/bin/sudo", "--", "/usr/bin/python3", "-I", "-B", "-c",
                             source, case, str(uid), nonce, before["boot_id"], before["adapter_fingerprint"]])
        command = ["ssh", "-tt", "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
                   "-o", "UserKnownHostsFile=" + str(target.known_hosts_path), "-o", "ConnectTimeout=8",
                   "-i", str(target.key_path), "-p", str(target.port),
                   target.username + "@" + target.host, remote]
        print(f"Preparing {case}. Enter a sudo password only in this terminal if requested.", flush=True)
        # Inherit the owner's terminal: never capture sudo input/output.
        process = subprocess.Popen(command)
        deadline = time.monotonic() + 90
        while True:
            value = read_status(sftp, status, uid, case)
            if value is not None:
                if value.get("phase") != "ready":
                    raise FaultStatusError(value)
                break
            if process.poll() is not None or time.monotonic() >= deadline:
                raise ValueError("test preparation unavailable")
            time.sleep(0.2)
        try:
            fault_result, fault_report = passive(config_path, target_id)
            matched = expected_fault(case, environment(fault_report))
        finally:
            release_guard(sftp, release)
        if process.wait(timeout=65) != 0:
            raise FaultStatusError(read_status(sftp, status, uid, case))
        value = read_status(sftp, status, uid, case)
        if value is None or value.get("phase") != "restored" or value.get("test_setup_failed") is not False:
            raise FaultStatusError(value)
        after_result, after_report = passive(config_path, target_id)
        restored = restored_environment(before, environment(after_report))
        result = {"case": case, "assessment": "pass" if matched and restored else "fail",
                  "baseline_run_id": baseline["checks"]["run_id"],
                  "fault_run_id": fault_result["checks"]["run_id"],
                  "restoration_run_id": after_result["checks"]["run_id"],
                  "fault_observed": matched, "restoration_confirmed": restored, "identity_reads": 0,
                  "scope": "passive recognition of a real prepared condition; no model evaluation"}
        for path in (status, release):
            try:
                sftp.remove(path)
            except FileNotFoundError:
                pass
        return result
    finally:
        if process is not None and process.poll() is None:
            if sftp is not None and release is not None:
                release_guard(sftp, release)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        if sftp is not None:
            sftp.close()
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--case", choices=(*CASES, "all"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-changes", action="store_true")
    parser.add_argument("--confirm-exclusive-i2c", action="store_true")
    args = parser.parse_args()
    if not sys.stdin.isatty() or not args.confirm_changes or not args.confirm_exclusive_i2c:
        parser.error("Run in your own terminal with explicit change and current exclusive-use confirmations")
    cases = CASES if args.case == "all" else (args.case,)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    results = []
    try:
        for case in cases:
            result = one_case(args.config, args.target, case)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            if result["assessment"] != "pass":
                break
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired, paramiko.SSHException, KeyboardInterrupt) as exc:
        result = {"case": case, "assessment": "unavailable", "next_action":
                  "Stop and check administrator restoration/status files; do not retry automatically"}
        if isinstance(exc, FaultStatusError):
            result["administrator_status"] = exc.status
        results.append(result)
    finally:
        with os.fdopen(fd, "w") as output:
            json.dump({"results": results}, output, ensure_ascii=False, indent=2)
            output.write("\n")
    return 0 if len(results) == len(cases) and all(r["assessment"] == "pass" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

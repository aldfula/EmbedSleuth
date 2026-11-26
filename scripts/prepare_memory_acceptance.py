"""Apply the approved memory boot option via the owner's interactive sudo.

No password capture, hardware inspection, quarantine reset or SSH trust change.
"""

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import paramiko

from embedsleuth.config import load_configuration
from embedsleuth.targets import TargetRegistry

ROOT = Path(__file__).resolve().parents[1]


def connect(target):
    client = paramiko.SSHClient()
    client.load_host_keys(str(target.known_hosts_path))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(hostname=target.host, port=target.port, username=target.username,
                       key_filename=str(target.key_path), allow_agent=False, look_for_keys=False,
                       timeout=3, auth_timeout=3, banner_timeout=3, channel_timeout=8)
        return client
    except BaseException:
        client.close()
        raise


def read_json(target, source, *args):
    client = connect(target)
    try:
        stdin, stdout, stderr = client.exec_command(
            shlex.join(["/usr/bin/python3", "-I", "-B", "-c", source, *args]), timeout=10)
        stdin.close()
        data = stdout.read(16385)
        if len(data) > 16384 or stdout.channel.recv_exit_status() or stderr.read(1024):
            raise ValueError("remote preparation check unavailable")
        return json.loads(data)
    finally:
        client.close()


POSTFLIGHT = '''from pathlib import Path
import json,hashlib,sys
p=Path
cmd=p("/boot/firmware/cmdline.txt").read_bytes()
backup=p(sys.argv[1]).read_bytes()
print(json.dumps({"boot_id":p("/proc/sys/kernel/random/boot_id").read_text().strip(),
"cmdline_sha256":hashlib.sha256(cmd).hexdigest(),"backup_sha256":hashlib.sha256(backup).hexdigest(),
"controllers":p("/sys/fs/cgroup/cgroup.controllers").read_text().split(),
"runtime_memory_options":[s for s in p("/proc/cmdline").read_text().split() if s.startswith(("cgroup_enable=","cgroup_disable="))]}))
'''


def verify(plan, after):
    return (after.get("boot_id") != plan["boot_id"] and isinstance(after.get("boot_id"), str)
            and after.get("cmdline_sha256") == plan["after_sha256"]
            and after.get("backup_sha256") == plan["before_sha256"]
            and "memory" in after.get("controllers", []))


def save(stream, result):
    stream.seek(0)
    json.dump(result, stream, indent=2)
    stream.write("\n")
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--confirm-boot-change-and-reboot", required=True, action="store_true")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        raise SystemExit("Run in your own terminal; do not send passwords to chat")
    config = load_configuration(args.config)
    target = TargetRegistry(config.targets).get(args.target)
    if target.key_path is None or target.username == "root":
        raise SystemExit("Explicit SSH key and a non-root registered account required")
    fd = os.open(args.output, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w+") as output:
        result = {"status": "preflight", "target_id": args.target, "quarantine_changed": False}
        save(output, result)
        source = (ROOT / "evals/diagnose-mpu6050/memory_boot_guard.py").read_text()
        try:
            plan = read_json(target, source, "preflight")
            result.update(status="prepared", plan=plan)
            save(output, result)
            remote = shlex.join(["/usr/bin/sudo", "--", "/usr/bin/python3", "-I", "-B", "-c", source,
                                 "apply-and-reboot", plan["before_sha256"], plan["boot_id"], "confirmed"])
            command = ["ssh", "-tt", "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
                       "-o", "UserKnownHostsFile=" + str(target.known_hosts_path), "-o", "ConnectTimeout=8",
                       "-i", str(target.key_path), "-p", str(target.port),
                       target.username + "@" + target.host, remote]
            print("Preparing memory accounting. Enter a sudo password only in this terminal if requested.", flush=True)
            print("Backup and recovery path: " + plan["backup"], flush=True)
            result["status"] = "administrator_action_started"
            save(output, result)
            # Inherit owner's terminal. Password input/output is never captured.
            status = subprocess.run(command, timeout=180).returncode
            result["ssh_exit_status"] = status
            if status not in (0, 255):
                raise ValueError("administrator preparation failed; no automatic retry")
            result["status"] = "waiting_for_reboot"
            save(output, result)
            print("Waiting for Pi to return after reboot...", flush=True)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    after = read_json(target, POSTFLIGHT, plan["backup"])
                except (OSError, paramiko.SSHException):
                    time.sleep(2)
                    continue  # Connection readiness only; never repeats the root action.
                if after["boot_id"] == plan["boot_id"]:
                    time.sleep(2)
                    continue
                result["postflight"] = after
                if not verify(plan, after):
                    raise ValueError("post-reboot memory accounting verification failed")
                result["status"] = "memory_accounting_enabled"
                save(output, result)
                print(json.dumps(result, indent=2))
                return
            raise TimeoutError("Pi did not return with a new boot ID within 90 seconds")
        except BaseException as exc:
            result.update(status="unavailable", error_kind=type(exc).__name__,
                          next_action="Review the saved plan and backup; do not rerun or clear quarantine automatically")
            save(output, result)
            raise


if __name__ == "__main__":
    main()

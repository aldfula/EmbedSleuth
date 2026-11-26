"""Plan/apply a narrow, operator-approved local recovery after verified reboot.

Reads boot identity over the registered SSH connection; never inspects I2C.
This is not an MCP tool and cannot resolve reads, pending IO or other failures.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

import paramiko

from embedsleuth.config import load_configuration
from embedsleuth.core import DiagnosticCore
from embedsleuth.storage import RunStore
from embedsleuth.targets import TargetRegistry


def private_observation(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > 16384):
            raise ValueError("unsafe previous boot observation")
        return json.loads(stream.read(16385))


def observe_boot(target):
    client = paramiko.SSHClient()
    client.load_host_keys(str(target.known_hosts_path))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(hostname=target.host, port=target.port, username=target.username,
                       key_filename=str(target.key_path) if target.key_path else None,
                       allow_agent=target.key_path is None, look_for_keys=False,
                       timeout=5, auth_timeout=5, banner_timeout=5, channel_timeout=8)
        fingerprint = "sha256:" + hashlib.sha256(client.get_transport().get_remote_server_key().asbytes()).hexdigest()
        command = "/usr/bin/python3 -I -B -c 'from pathlib import Path; print(Path(\"/proc/sys/kernel/random/boot_id\").read_text().strip())'"
        stdin, stdout, stderr = client.exec_command(command, timeout=8)
        stdin.close()
        data = stdout.read(128)
        if stdout.channel.recv_exit_status() or stderr.read(1024) or len(data) >= 128:
            raise ValueError("boot observation unavailable")
        return data.decode("ascii").strip(), fingerprint
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--prior-boot-observation", required=True, type=Path)
    parser.add_argument("--confirm-local-recovery", action="store_true")
    args = parser.parse_args()
    config = load_configuration(args.config)
    target = TargetRegistry(config.targets).get(args.target)
    previous = private_observation(args.prior_boot_observation)["boot_id"]
    current, fingerprint = observe_boot(target)
    target_digest = DiagnosticCore._target_digest(target)
    with RunStore.open_reader(config.state_directory) as store:
        state, events = store.load_state(args.run), store.read_events(args.run)
        if (state["target_id"] != args.target or state["target_digest"] != target_digest
                or not store._passive_cancel_candidate(args.run, state, events)
                or current == previous or not store.is_quarantined(args.target)):
            raise ValueError("recovery conditions not confirmed")
        others = [run for run in store.list_runs() if run != args.run
                  and (other := store.load_state(run))["target_id"] == args.target
                  and (other.get("pending_operation") is not None or other.get("remote_state") in ("unknown", "running"))]
        if others:
            raise ValueError("other unresolved operations require review")
    if args.confirm_local_recovery:
        with RunStore(config.state_directory) as store:
            result = store.resolve_passive_cancel_after_reboot(args.run,
                previous_boot_id=previous, current_boot_id=current, host_key_fingerprint=fingerprint,
                target_digest=target_digest, operator_confirmed=True)
        print(json.dumps({"status": "resolved", "resolution": result}, indent=2))
    else:
        print(json.dumps({"status": "ready_for_operator_approval", "target_id": args.target, "run_id": args.run,
            "previous_boot_id": previous, "current_boot_id": current, "host_key_fingerprint": fingerprint,
            "scope": "one closed passive board cancellation; preserve original report/evidence/audit",
            "quarantine_changed": False}, indent=2))


if __name__ == "__main__":
    main()

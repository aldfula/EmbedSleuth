"""Owner-supervised boot preparation, outside the read-only diagnostic MCP."""

import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

BOOT = Path("/boot/firmware")
CMDLINE = BOOT / "cmdline.txt"
PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def proposed(data):
    if (type(data) is not bytes or not 0 < len(data) <= 16384
            or b"\x00" in data or len(data.splitlines()) != 1):
        raise ValueError("expected one bounded kernel command line")
    text = data.decode("ascii")
    tokens = text.split()
    if not tokens:
        raise ValueError("empty kernel command line")
    memory = [x for x in tokens if x.startswith(("cgroup_enable=", "cgroup_disable=", "cgroup_memory="))]
    if memory:
        raise ValueError("existing cgroup boot options require manual review")
    suffix = b"\r\n" if data.endswith(b"\r\n") else b"\n" if data.endswith(b"\n") else b""
    return data.rstrip(b"\r\n") + b" cgroup_enable=memory" + suffix


def regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o022 or info.st_nlink != 1 or info.st_size > 16384):
        raise ValueError("unsafe boot file")
    return info


def read_boot():
    regular(CMDLINE)
    fd = os.open(CMDLINE, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        return stream.read(16385)


def preflight():
    boot_id = (PROC / "sys/kernel/random/boot_id").read_text().strip()
    uuid.UUID(boot_id)
    if "memory" in (CGROUP / "cgroup.controllers").read_text().split():
        raise ValueError("memory controller already available; no boot edit needed")
    mounts = [line.split() for line in (PROC / "mounts").read_text().splitlines()]
    if not any(row[1:3] == [str(BOOT), "vfat"] for row in mounts):
        raise ValueError("reviewed boot partition not mounted")
    if BOOT.is_symlink() or CMDLINE.parent != BOOT:
        raise ValueError("unexpected boot path")
    for line in (BOOT / "config.txt").read_text().splitlines():
        if line.strip().startswith(("cmdline=", "os_prefix=", "include ")):
            raise ValueError("boot selection override requires review")
    supported = False
    for path in (PROC / "config.gz", Path("/boot/config-" + os.uname().release)):
        try:
            text = gzip.decompress(path.read_bytes()).decode() if path.suffix == ".gz" else path.read_text()
            supported = "CONFIG_MEMCG=y" in text.splitlines()
            if supported:
                break
        except OSError:
            pass
    if not supported:
        raise ValueError("memory accounting kernel support unconfirmed")
    original = read_boot()
    changed = proposed(original)
    return {"boot_id": boot_id, "before_sha256": digest(original), "after_sha256": digest(changed),
            "backup": str(CMDLINE) + ".embedsleuth-" + digest(original)[:16] + ".bak",
            "change": "append cgroup_enable=memory", "size_before": len(original), "size_after": len(changed)}


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def exclusive_copy(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def apply(expected_hash, expected_boot):
    plan = preflight()
    if plan["before_sha256"] != expected_hash or plan["boot_id"] != expected_boot:
        raise ValueError("boot state changed since preparation")
    original = read_boot()
    if digest(original) != expected_hash:
        raise ValueError("command line changed")
    backup = Path(plan["backup"])
    if backup.exists() or backup.is_symlink():
        regular(backup)
        if backup.read_bytes() != original:
            raise ValueError("backup conflict")
    else:
        exclusive_copy(backup, original)
    if backup.read_bytes() != original:
        raise ValueError("backup verification failed")
    # Backups survive any later failure; no restoration from a guessed value.
    changed = proposed(original)
    temporary = CMDLINE.with_name(".embedsleuth-cmdline-" + uuid.uuid4().hex)
    try:
        exclusive_copy(temporary, changed)
        if temporary.read_bytes() != changed:
            raise ValueError("staged write verification failed")
        if read_boot() != original:
            raise ValueError("command line changed before replacement")
        os.replace(temporary, CMDLINE)
        sync_directory(BOOT)
        if read_boot() != changed or backup.read_bytes() != original:
            raise ValueError("post-write verification failed; use the backup")
        os.sync()
    finally:
        if temporary.exists():
            temporary.unlink()
    return plan


def main():
    if sys.argv[1:] == ["preflight"]:
        print(json.dumps(preflight()))
        return
    if (len(sys.argv) != 5 or sys.argv[1] != "apply-and-reboot" or os.geteuid() != 0
            or not sys.stdin.isatty() or not os.environ.get("SUDO_UID", "").isdigit()
            or int(os.environ["SUDO_UID"]) <= 0):
        raise SystemExit("Run only through interactive sudo in the owner's terminal")
    if re.fullmatch(r"[a-f0-9]{64}", sys.argv[2]) is None:
        raise SystemExit("Invalid expected command-line digest")
    uuid.UUID(sys.argv[3])
    if sys.argv[4] != "confirmed":
        raise SystemExit("Explicit change confirmation required")
    fd = os.open("/run/lock/embedsleuth-memory-boot.lock",
                 os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError("unsafe preparation lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = apply(sys.argv[2], sys.argv[3])
        print("EmbedSleuth backup verified: " + plan["backup"], flush=True)
        print("Recovery in a Pi terminal: sudo cp -- " + plan["backup"] + " " + str(CMDLINE), flush=True)
        print("Memory accounting boot option verified; rebooting now.", flush=True)
        os.execv("/usr/sbin/reboot", ["/usr/sbin/reboot"])
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()

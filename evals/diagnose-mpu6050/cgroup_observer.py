"""Read-only lifetime charged-memory bounds for SSH + login session groups.

Does not reset counters, change groups, move processes or inspect devices.
memory.peak is not the sum of process RSS and must not be labelled as such.
"""

import json
from pathlib import Path
import select
import sys
import time

ROOT = Path("/sys/fs/cgroup")
GROUPS = ("/system.slice/ssh.service", "/user.slice")


def covered(path):
    return any(path == group or path.startswith(group + "/") for group in GROUPS)


def group_path(text):
    rows = [line[3:] for line in text.splitlines() if line.startswith("0::/")]
    if len(rows) != 1 or ".." in Path(rows[0]).parts:
        raise ValueError("unavailable cgroup v2 membership")
    return rows[0]


def counters(root=ROOT):
    result = []
    for name in GROUPS:
        path = root / name.lstrip("/")
        info = path.stat()
        current = int((path / "memory.current").read_text().strip())
        peak = int((path / "memory.peak").read_text().strip())
        if current < 0 or peak <= 0 or peak < current:
            raise ValueError("unavailable kernel memory counters")
        result.append({"path": name, "inode": info.st_ino, "current_bytes": current, "peak_bytes": peak})
    return result


def bounds(before, after):
    if len(before) != 2 or len(after) != 2:
        raise ValueError("incomplete accounting scope")
    for row in [*before, *after]:
        if (any(type(row[k]) is not int for k in ("inode", "current_bytes", "peak_bytes"))
                or row["inode"] <= 0 or row["current_bytes"] < 0 or row["peak_bytes"] <= 0
                or row["peak_bytes"] < row["current_bytes"]):
            raise ValueError("invalid accounting counters")
    for a, b, name in zip(before, after, GROUPS):
        if (a["path"] != name or b["path"] != name or a["inode"] != b["inode"]
                or a["peak_bytes"] > b["peak_bytes"]):
            raise ValueError("accounting group changed or counters reset")
    return sum(row["peak_bytes"] for row in after)


def ssh_memberships():
    groups = set()
    unreadable = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            if (proc / "comm").read_text().strip() == "sshd":
                groups.add(group_path((proc / "cgroup").read_text()))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            unreadable.append(int(proc.name))
    return {"groups": sorted(groups), "unreadable_pids": unreadable}


def main():
    started = time.monotonic()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    before = counters()
    ssh = ssh_memberships()
    observer_group = group_path(Path("/proc/self/cgroup").read_text())
    print(json.dumps({"ready": True, "before": before, "boot_id": boot}), flush=True)
    if not select.select([sys.stdin], [], [], 25)[0] or sys.stdin.readline(16) != "finish\n":
        raise ValueError("observation window expired")
    after = counters()
    if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != boot:
        raise ValueError("boot changed")
    print(json.dumps({"measurement": "sum_of_lifetime_cgroup_memory_peaks", "before": before,
        "after": after, "bound_bytes": bounds(before, after), "boot_id": boot,
        "observer_group": observer_group, "observer_included": True, "ssh_memberships": ssh,
        "elapsed_ms": round((time.monotonic()-started)*1000),
        "limitations": ["charged memory differs from summed RSS",
                        "includes unrelated work and observer memory since group creation",
                        "independent group peaks are summed; no subtraction of lifetime peaks"]}), flush=True)


if __name__ == "__main__":
    main()

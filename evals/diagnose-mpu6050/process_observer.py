"""Read-only Linux /proc sampler for acceptance, not a diagnostic MCP tool.

No device, command-line, environment, credential or writable file access.
Sampling cannot establish an unobserved peak or kill a process.
"""

import json
import os
from pathlib import Path
import sys
import time


def parse_stat(text, page_kib):
    # comm may contain spaces and parentheses. Fields after its last ')' start
    # at state (field 3); starttime is 22 and RSS is 24.
    end = text.rfind(")")
    if end < 0:
        raise ValueError("invalid process stat")
    pid = int(text[:text.index(" ")])
    fields = text[end + 1:].split()
    start, rss = int(fields[19]), int(fields[21])
    if pid <= 0 or start < 0 or rss < 0:
        raise ValueError("invalid process counters")
    return (pid, start), rss * page_kib


def snapshot(root=Path("/proc")):
    rows, unreadable = {}, []
    page_kib = os.sysconf("SC_PAGE_SIZE") // 1024
    entries = [entry for entry in root.iterdir() if entry.name.isdecimal()]
    if len(entries) > 4096:
        raise ValueError("process budget exceeded")
    for entry in entries:
        try:
            key, rss = parse_stat((entry / "stat").read_text()[:16384], page_kib)
            if key[0] != int(entry.name):
                raise ValueError("process mismatch")
            rows[key] = rss
        except FileNotFoundError:  # Process exited between listing and read.
            continue
        except (OSError, ValueError, IndexError):
            unreadable.append(int(entry.name))
    return rows, unreadable


def positive_delta(baseline, current, excluded_pid):
    return sum(max(0, rss - baseline.get(key, 0)) for key, rss in current.items()
               if key[0] != excluded_pid)


def probe_exit_status(pid, start_ticks, final_keys, unreadable):
    if (type(pid) is not int or pid <= 0 or type(start_ticks) is not str
            or not 1 <= len(start_ticks) <= 30 or not start_ticks.isdecimal()):
        return "unknown"
    if pid in unreadable:
        return "unknown"
    return "still_present" if [pid, int(start_ticks)] in final_keys else "not_present_at_final_sample"


def main():
    baseline, missing = snapshot()
    print(json.dumps({"ready": True}), flush=True)
    peak, count, seen, max_gap = 0, 0, set(), 0.0
    started = previous = time.monotonic()
    # Fixed 15 second window and 20 ms requested interval; no remote inputs.
    while time.monotonic() - started < 15:
        now = time.monotonic()
        max_gap = max(max_gap, now - previous)
        previous = now
        current, errors = snapshot()
        missing.extend(errors)
        peak = max(peak, positive_delta(baseline, current, os.getpid()))
        seen.update(set(current) - set(baseline))
        count += 1
        time.sleep(0.02)
    final, errors = snapshot()
    missing.extend(errors)
    print(json.dumps({
        "schema_version": "1.0", "measurement": "sampled_proc_stat_positive_rss_delta",
        "peak_observed_kib": peak, "sample_count": count,
        "max_sample_gap_ms": round(max_gap * 1000, 3),
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "unreadable_pids": sorted(set(missing)), "new_processes_seen": len(seen),
        "final_process_keys": [list(key) for key in final],
        "observer_excluded": True, "peak_guaranteed": False,
        "limitations": ["sampling can miss short-lived peaks and processes",
                        "unrelated process growth and shared pages may be counted",
                        "observer SSH session is in baseline and excluded",
                        "not a proof of total incremental peak RSS"],
    }), flush=True)


if __name__ == "__main__":
    main()

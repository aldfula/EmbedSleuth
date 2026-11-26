"""Read-only observer of one exact production probe. No process signals or IO.

Only new, same-owner Python command lines are hashed, never returned or saved.
The caller supplies the SHA256 of the reviewed production source, not a command.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def process_key(text):
    end = text.rfind(")")
    fields = text[end + 1:].split()
    pid = int(text[:text.index(" ")])
    parent, start = int(fields[1]), int(fields[19])
    if end < 0 or pid <= 0 or parent < 0 or start < 0:
        raise ValueError("invalid process identity")
    return (pid, start), parent


def exact_probe(command, digest):
    args = command.rstrip(b"\0").split(b"\0")
    return (len(args) == 5 and args[:4] == [b"python3", b"-I", b"-B", b"-c"]
            and hashlib.sha256(args[4]).hexdigest() == digest)


def snapshot(root=Path("/proc")):
    rows, errors = {}, []
    entries = [p for p in root.iterdir() if p.name.isdecimal()]
    if len(entries) > 4096:
        raise ValueError("process count exceeded")
    for p in entries:
        try:
            key, parent = process_key((p / "stat").read_text()[:16384])
            if key[0] != int(p.name):
                raise ValueError("PID changed")
            rows[key] = parent
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError):
            errors.append(int(p.name))
    return rows, errors


def main():
    digest = sys.stdin.readline(128).strip()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid source digest")
    baseline, errors = snapshot()
    print(json.dumps({"ready": True}), flush=True)
    seen, lineage, observed = None, [], False
    start = time.monotonic()
    while time.monotonic() - start < 8:
        rows, missing = snapshot()
        errors.extend(missing)
        if seen is None:
            for key in set(rows) - set(baseline):
                p = Path("/proc") / str(key[0])
                try:
                    if p.stat().st_uid != os.getuid():
                        continue
                    if (p / "comm").read_text().strip() != "python3":
                        continue
                    with (p / "cmdline").open("rb") as f:
                        command = f.read(65537)
                    if len(command) > 65536 or not exact_probe(command, digest):
                        continue
                    current, _ = process_key((p / "stat").read_text())
                    if current != key:
                        continue
                except (OSError, ValueError, IndexError):
                    continue
                seen = key
                by_pid = {k[0]: (k, parent) for k, parent in rows.items()}
                parent = rows[key]
                for _ in range(16):
                    if parent not in by_pid:
                        break
                    ancestor, parent = by_pid[parent]
                    if ancestor in baseline:
                        break
                    lineage.append(ancestor)
                print(json.dumps({"event": "production_probe_seen", "key": list(key),
                                  "observed_ms": round((time.monotonic()-start)*1000, 3)}), flush=True)
                observed = True
                break
        time.sleep(0.002)
    final, missing = snapshot()
    errors.extend(missing)
    keys = ([seen] if seen else []) + lineage
    print(json.dumps({"event": "final", "probe_seen": observed,
        "probe_key": list(seen) if seen else None,
        "captured_processes": [{"key": list(k), "present_at_end": k in final,
                                "readable_at_end": k[0] not in missing} for k in keys],
        "unreadable_pids": sorted(set(errors)), "elapsed_ms": round((time.monotonic()-start)*1000),
        "limitations": ["observation precedes cancellation receipt; probe may finish during that interval",
                        "only captured processes are checked; not a proof of unobserved child exit"]}), flush=True)


if __name__ == "__main__":
    main()

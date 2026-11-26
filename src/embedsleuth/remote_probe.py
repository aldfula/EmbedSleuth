"""Standalone, stdlib-only Pi probe. Source is executed by SSH, never installed.

No caller-supplied command, path, register, transfer length or process flag is
accepted. Inspection does not perform I2C transfers. Only identity does so.
"""

import array
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import selectors
import signal
import re
import time
import subprocess
import sys

MAX_REQUEST = 4096
MAX_FILE = 16384
I2C_FUNCS = 0x0705
I2C_FUNC_I2C = 0x00000001


def response(status="ok", error_code=None, message="", observations=None, remote_state_unknown=False):
    return {"status": status, "error_code": error_code, "message": message,
            "observations": observations or {}, "remote_state_unknown": remote_state_unknown}


def validate_request(request):
    if type(request) is not dict or type(request.get("op")) is not str:
        raise ValueError("invalid request")
    op = request["op"]
    fields = {"inspect_target": {"op"},
              "inspect_i2c_environment": {"op", "bus", "addresses"},
              "read_device_identity": {"op", "bus", "addresses", "address", "expected_boot_id", "expected_adapter_fingerprint"}}
    if op not in fields or set(request) != fields[op]:
        raise ValueError("unsupported operation or fields")
    if op == "inspect_target":
        return request
    if type(request["bus"]) is not int or not 0 <= request["bus"] <= 255:
        raise ValueError("invalid bus")
    addresses = request["addresses"]
    if type(addresses) is not list or not 1 <= len(addresses) <= 2:
        raise ValueError("invalid addresses")
    if any(type(a) is not int or a not in (104, 105) for a in addresses) or len(set(addresses)) != len(addresses):
        raise ValueError("invalid addresses")
    if op == "read_device_identity":
        if type(request["address"]) is not int or request["address"] not in addresses:
            raise ValueError("unexpected identity address")
        for field in ("expected_boot_id", "expected_adapter_fingerprint"):
            value = request[field]
            if type(value) is not str or not value or len(value) > 128:
                raise ValueError("invalid precondition fingerprint")
    return request


def read_fixed_file(path):
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            return None
        return data.decode("utf-8", errors="replace").replace("\x00", "").strip()
    except OSError:
        return None


def boot_id():
    return read_fixed_file("/proc/sys/kernel/random/boot_id")


def inspect_target():
    return response(observations={
        "boot_id": boot_id(),
        "board_model": read_fixed_file("/proc/device-tree/model"),
        "os_release": read_fixed_file("/etc/os-release"),
        "kernel_release": os.uname().release,
        "machine": os.uname().machine,
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        "dependency_available": find_i2ctransfer() is not None,
    })


def find_i2ctransfer():
    # A fixed search path avoids caller-controlled PATH executables.
    for base in ("/usr/sbin", "/usr/bin", "/sbin", "/bin"):
        candidate = Path(base) / "i2ctransfer"
        try:
            info = candidate.stat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022 and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            pass
    return None


def config_summary(path):
    """Return relevant configuration facts, never arbitrary boot-file contents."""
    text = read_fixed_file(path)
    if text is None:
        return {"available": False, "directives": []}
    directives = []
    for source_line in text.splitlines():
        line = source_line.split("#", 1)[0].strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            directives.append("[%s]" % section if section in ("all", "none", "pi3", "pi3+", "tryboot") else "[conditional]")
        elif line.startswith("include "):
            directives.append("include present; not resolved")
        elif line.startswith("dtparam="):
            for item in line[len("dtparam="):].split(","):
                if re.fullmatch(r"i2c(?:_arm|_vc)?(?:_baudrate)?=(?:on|off|[0-9]{1,9})", item):
                    directives.append("dtparam=" + item)
                elif item.startswith("i2c"):
                    directives.append("unparsed I2C parameter")
        elif line.startswith("dtoverlay="):
            overlay = line[len("dtoverlay="):].split(",", 1)[0]
            if re.fullmatch(r"i2c[0-9a-z_-]{0,40}", overlay):
                directives.append("dtoverlay=" + overlay + "; parameters omitted")
    return {"available": True, "directives": directives}


def collect_environment(bus, addresses):
    adapter = Path("/sys/class/i2c-dev") / ("i2c-%d" % bus)
    device = Path("/dev") / ("i2c-%d" % bus)
    present = adapter.exists()
    name = read_fixed_file(str(adapter / "name")) if present else None
    current_boot = boot_id()
    fingerprint = None
    if present and current_boot and name:
        fingerprint = hashlib.sha256((current_boot + "\n" + str(adapter.resolve()) + "\n" + name).encode()).hexdigest()
    access = False
    combined = None
    try:
        info = device.stat()
        access = stat.S_ISCHR(info.st_mode) and os.access(device, os.R_OK | os.W_OK)
        if access:
            fd = os.open(device, os.O_RDWR | os.O_CLOEXEC)
            try:
                funcs = array.array("L", [0])
                fcntl.ioctl(fd, I2C_FUNCS, funcs, True)
                combined = bool(funcs[0] & I2C_FUNC_I2C)
            finally:
                os.close(fd)
    except OSError:
        access = False
    bound = {str(address): (Path("/sys/bus/i2c/devices") / ("%d-%04x" % (bus, address)) / "driver").exists()
             for address in addresses}
    return {"bus": bus, "boot_id": current_boot, "adapter_fingerprint": fingerprint,
            "adapter_present": present, "adapter_name": name, "access_allowed": access,
            "combined_transfer_supported": combined, "driver_bound": bound,
            "dependency_available": find_i2ctransfer() is not None,
            "config_sources": {path: config_summary(path) for path in
                               ("/boot/firmware/config.txt", "/boot/config.txt")},
            "configuration_complete": False}


def inspect_i2c_environment(request):
    return response(observations=collect_environment(request["bus"], request["addresses"]))


def communicate_bounded(process, timeout):
    """Read child pipes incrementally so malformed output cannot exhaust Pi RAM."""
    deadline = time.monotonic() + timeout
    output = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": 1024, "stderr": 4096}
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("fixed identity transaction", timeout)
            for key, _ in selector.select(min(remaining, 0.05)):
                chunk = os.read(key.fd, 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                destination = output[key.data]
                if len(destination) + len(chunk) > limits[key.data]:
                    raise ValueError("transfer output limit")
                destination.extend(chunk)
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
    return bytes(output["stdout"]), bytes(output["stderr"])


def stop_transfer(process):
    # Popen starts a new session, so this only targets this fixed probe's group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(timeout=0.2)
    except (OSError, subprocess.TimeoutExpired):
        pass


def read_device_identity(request):
    environment = collect_environment(request["bus"], request["addresses"])
    if (not environment["boot_id"] or environment["boot_id"] != request["expected_boot_id"] or
            not environment["adapter_fingerprint"] or environment["adapter_fingerprint"] != request["expected_adapter_fingerprint"]):
        return response("blocked", "policy_denied", "Target or adapter preconditions changed", environment)
    if not environment["adapter_present"]:
        return response("blocked", "adapter_not_found", "Expected I2C adapter absent", environment)
    if not environment["access_allowed"]:
        return response("blocked", "permission_denied", "I2C access unavailable", environment)
    if environment["driver_bound"][str(request["address"])]:
        return response("blocked", "driver_bound", "Kernel driver owns target device", environment)
    if environment["combined_transfer_supported"] is not True:
        return response("unsupported", "dependency_missing", "Combined I2C transfer unavailable", environment)
    executable = find_i2ctransfer()
    if executable is None:
        return response("unsupported", "dependency_missing", "Existing trusted i2ctransfer unavailable", environment)
    command = [executable, "-y", str(request["bus"]), "w1@0x%02x" % request["address"], "0x75", "r1"]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}, start_new_session=True)
    try:
        output, errors = communicate_bounded(process, timeout=3.0)
    except subprocess.TimeoutExpired:
        stop_transfer(process)
        return response("timeout", "remote_state_unknown", "I2C operation timed out; do not repeat automatically", environment, True)
    except ValueError:
        stop_transfer(process)
        return response("error", "resource_limit", "Transfer output exceeded bounds", environment, True)
    except Exception:
        stop_transfer(process)
        return response("error", "remote_state_unknown", "Transfer completion could not be confirmed", environment, True)
    finally:
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
    if process.returncode:
        # Raw stderr is deliberately excluded: it is untrusted and may contain secrets.
        return response("error", "transfer_failed", "I2C identity transaction failed", environment)
    token = output.strip()
    if len(token) != 4 or not token.startswith(b"0x") or any(c not in b"0123456789abcdefABCDEF" for c in token[2:]):
        return response("error", "transfer_failed", "Unexpected identity response format", environment)
    try:
        value = int(token[2:], 16)
    except ValueError:
        return response("error", "transfer_failed", "Unexpected identity byte", environment)
    environment.update({"address": request["address"], "identity_value": value, "register": 117})
    return response(observations=environment)


def dispatch(request):
    validate_request(request)
    if request["op"] == "inspect_target":
        return inspect_target()
    if request["op"] == "inspect_i2c_environment":
        return inspect_i2c_environment(request)
    return read_device_identity(request)


def resource_usage(started):
    """Linux probe + one waited transfer child's conservative RSS bound.

    RUSAGE_CHILDREN is the largest waited child's peak, not a tree total.
    This probe launches at most one child. SSH/shell/other services are outside
    this measurement; do not present it as system-wide incremental memory.
    """
    if sys.platform != "linux":
        return {"available": False}
    try:
        import resource

        own = resource.getrusage(resource.RUSAGE_SELF)
        child = resource.getrusage(resource.RUSAGE_CHILDREN)
        own_rss, child_rss = int(own.ru_maxrss), int(child.ru_maxrss)
        if own_rss <= 0 or child_rss < 0:
            return {"available": False}
        stat_text = read_fixed_file("/proc/self/stat")
        start_ticks = stat_text.rsplit(")", 1)[1].split()[19] if stat_text else None
        return {
            "schema_version": "1.0", "available": True,
            "measurement": "linux_getrusage", "scope": "probe_and_single_waited_child",
            "probe_peak_rss_kib": own_rss, "child_peak_rss_kib": child_rss,
            "peak_sum_bound_kib": own_rss + child_rss,
            "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
            "cpu_ms": max(0, int((own.ru_utime + own.ru_stime + child.ru_utime + child.ru_stime) * 1000)),
            "pid": os.getpid(), "process_start_ticks": start_ticks,
        }
    except (ImportError, OSError, ValueError, IndexError, AttributeError):
        return {"available": False}


def main():
    started = time.monotonic()
    try:
        wire = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(wire) > MAX_REQUEST:
            raise ValueError("request too large")
        result = dispatch(json.loads(wire))
    except (ValueError, TypeError, UnicodeError):
        result = response("blocked", "invalid_request", "Invalid probe request")
    except Exception:
        result = response("error", "transfer_failed", "Remote inspection failed", remote_state_unknown=True)
    # Metrics are observations only; they never authorize a transfer or alter
    # execution status. Failure to measure is explicitly unknown, not zero.
    result["observations"]["resource_usage"] = resource_usage(started)
    # Membership is evidence for development memory accounting, not IO policy.
    result["observations"]["process_cgroup"] = read_fixed_file("/proc/self/cgroup")
    sys.stdout.write(json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()

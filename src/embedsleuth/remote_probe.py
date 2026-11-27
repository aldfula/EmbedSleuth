"""Standalone, stdlib-only Pi probe. Source is executed by SSH, never installed.

No caller-supplied command, path, register, transfer length or process flag is
accepted. Inspection does not perform I2C transfers. Sensor sampling is a
separate fixed procedure, admitted only after local approval in the PC core.
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
    fields["sample_sensor_data"] = fields["read_device_identity"] | {"plan_id"}
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
    if op in ("read_device_identity", "sample_sensor_data"):
        if type(request["address"]) is not int or request["address"] not in addresses:
            raise ValueError("unexpected identity address")
        for field in ("expected_boot_id", "expected_adapter_fingerprint"):
            value = request[field]
            if type(value) is not str or not value or len(value) > 128:
                raise ValueError("invalid precondition fingerprint")
    if op == "sample_sensor_data" and (type(request["plan_id"]) is not str
            or re.fullmatch(r"sampling-[0-9a-f]{64}", request["plan_id"]) is None):
        raise ValueError("invalid sampling plan reference")
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


class SamplingFailure(Exception):
    def __init__(self, code, message, unknown=False):
        self.code, self.message, self.unknown = code, message, unknown
        super().__init__(message)


def sensor_transfer(command, timeout):
    """One fixed transaction. Timeout never establishes kernel IO completion."""
    if timeout <= 0:
        raise SamplingFailure("resource_limit", "Sampling time budget exhausted")
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}, start_new_session=True)
    try:
        output, _ = communicate_bounded(process, timeout=min(timeout, 0.45))
    except BaseException:
        stop_transfer(process)
        raise SamplingFailure("remote_state_unknown", "Transfer completion is unknown; no further bus access", True) from None
    finally:
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
    if process.returncode:
        raise SamplingFailure("transfer_failed", "Fixed sampling transaction failed")
    return output


def sample_sensor_data(request):
    """At most five snapshots; only SLEEP can be changed, then restored.

    The trusted PC core, not this wire request, owns local approval validation.
    Unknown transfer completion forbids even a restoration attempt: a second
    transfer must not race a possibly stuck first one. Report and quarantine.
    """
    environment = collect_environment(request["bus"], request["addresses"])
    if (environment.get("boot_id") != request["expected_boot_id"]
            or environment.get("adapter_fingerprint") != request["expected_adapter_fingerprint"]
            or not environment.get("adapter_present") or environment.get("access_allowed") is not True
            or environment.get("driver_bound", {}).get(str(request["address"])) is not False
            or environment.get("combined_transfer_supported") is not True):
        return response("blocked", "policy_denied", "Sampling prerequisites changed", environment)
    executable = find_i2ctransfer()
    if executable is None:
        return response("unsupported", "dependency_missing", "Trusted i2ctransfer unavailable", environment)
    address = "0x%02x" % request["address"]
    prefix = [executable, "-y", str(request["bus"])]
    started = time.monotonic()
    work_deadline, restoration_deadline = started + 5.5, started + 7.5
    environment.update({"address": request["address"], "plan_id": request["plan_id"], "samples": [],
                        "wakeup_attempted": False, "restoration_confirmed": True})
    original = None
    failure = None
    uncertain = False
    handlers = {}

    def read(register, length, deadline):
        # Only literals below call this helper; no model-supplied register.
        output = sensor_transfer(prefix + ["w1@" + address, "0x%02x" % register, "r%d" % length],
                                 deadline - time.monotonic())
        tokens = output.split()
        if len(tokens) != length or any(re.fullmatch(b"0x[0-9a-fA-F]{2}", token) is None for token in tokens):
            raise SamplingFailure("transfer_failed", "Malformed fixed sampling response")
        return [int(token[2:], 16) for token in tokens]

    def write_power(value, deadline):
        output = sensor_transfer(prefix + ["w2@" + address, "0x6b", "0x%02x" % value],
                                 deadline - time.monotonic())
        if output.strip():
            raise SamplingFailure("transfer_failed", "Unexpected power-write response")

    def interrupted(signum, frame):
        raise SamplingFailure("policy_denied", "Sampling interrupted; reviewing restoration")

    try:
        # Signals between transactions enter restoration. Signals during IO are
        # uncertain and cannot justify starting another bus transaction.
        for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            handlers[signum] = signal.signal(signum, interrupted)
        if read(0x75, 1, work_deadline) != [104]:
            raise SamplingFailure("identity_mismatch", "Identity changed before sampling")
        divider, configuration, gyro, accel = read(0x19, 4, work_deadline)
        interrupt_pin, interrupt_enable = read(0x37, 2, work_deadline)
        control, power, power2 = read(0x6a, 3, work_deadline)
        environment["configuration"] = {"sample_rate_divider": divider, "config": configuration,
                                         "gyro_config": gyro, "accel_config": accel,
                                         "user_ctrl": control, "pwr_mgmt_1": power, "pwr_mgmt_2": power2}
        if (gyro & 0xe7 or accel & 0xe7 or configuration & 0xc0 or configuration & 7 == 7
                or interrupt_pin & 0x10 or interrupt_enable != 0 or control != 0 or power2 != 0
                or power & 0xb8 or power & 7 > 3):
            raise SamplingFailure("policy_denied", "Unsupported self-test, interrupt, clock, standby, FIFO/DMP or power configuration")
        original = power
        active = power & ~0x40
        if power != active:
            # Mark before dispatch so a failed/unknown write cannot be hidden.
            environment["wakeup_attempted"] = True
            environment["restoration_confirmed"] = False
            write_power(active, work_deadline)
            if read(0x6b, 1, work_deadline) != [active]:
                raise SamplingFailure("state_changed", "Wake state did not match the approved bit change")
            time.sleep(0.1)
        for index in range(5):
            if index:
                time.sleep(0.05)
            environment["samples"].append(read(0x3b, 14, work_deadline))
        # Recheck relevant configuration instead of silently decoding a moving range.
        if (read(0x19, 4, work_deadline) != [divider, configuration, gyro, accel]
                or read(0x6a, 3, work_deadline) != [control, active, power2]):
            raise SamplingFailure("state_changed", "Sensor configuration changed during sampling")
    except SamplingFailure as exc:
        failure, uncertain = exc, exc.unknown
    except BaseException:
        failure = SamplingFailure("remote_state_unknown", "Sampling completion could not be established", True)
        uncertain = True
    finally:
        if environment["wakeup_attempted"] and not uncertain:
            try:
                # Finish one bounded restoration despite repeated graceful signals.
                for signum in handlers:
                    signal.signal(signum, signal.SIG_IGN)
                current = collect_environment(request["bus"], request["addresses"])
                if (current.get("boot_id") != request["expected_boot_id"]
                        or current.get("adapter_fingerprint") != request["expected_adapter_fingerprint"]
                        or current.get("adapter_present") is not True
                        or current.get("access_allowed") is not True
                        or current.get("combined_transfer_supported") is not True
                        or current.get("driver_bound", {}).get(str(request["address"])) is not False
                        or read(0x19, 4, restoration_deadline) != [divider, configuration, gyro, accel]):
                    raise SamplingFailure("state_changed", "Restoration prerequisites changed")
                current_control, current_power, current_power2 = read(0x6a, 3, restoration_deadline)
                if (current_control, current_power2) != (control, power2):
                    raise SamplingFailure("state_changed", "Another sensor configuration cannot be overwritten")
                if current_power not in (original, original & ~0x40):
                    raise SamplingFailure("state_changed", "Another power setting cannot be overwritten")
                if current_power != original:
                    write_power(original, restoration_deadline)
                restored = read(0x6b, 1, restoration_deadline)[0]
                environment["restored_pwr_mgmt_1"] = restored
                environment["restoration_confirmed"] = restored == original
            except BaseException:
                environment["restoration_confirmed"] = False
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
    if uncertain or environment["restoration_confirmed"] is not True:
        return response("error", "remote_state_unknown", "Remote completion or sensor restoration is unconfirmed",
                        environment, True)
    if failure:
        return response("blocked" if failure.code in ("policy_denied", "state_changed") else "error",
                        failure.code, failure.message, environment)
    return response(observations=environment)


def dispatch(request):
    validate_request(request)
    if request["op"] == "inspect_target":
        return inspect_target()
    if request["op"] == "inspect_i2c_environment":
        return inspect_i2c_environment(request)
    if request["op"] == "sample_sensor_data":
        return sample_sensor_data(request)
    return read_device_identity(request)


def resource_usage(started):
    """Linux probe + one waited transfer child's conservative RSS bound.

    RUSAGE_CHILDREN is the largest waited child's peak, not a tree total.
    This probe runs at most one child at a time. SSH/shell/other services are outside
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

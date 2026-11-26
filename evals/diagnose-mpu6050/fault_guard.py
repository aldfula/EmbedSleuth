"""Explicitly approved, interactive administrator test preparation only.

Never imported by the product. Fixed bus/address, 60 s lease, restoration on
normal exit/signals. SIGKILL or power loss still needs operator recovery.
"""

import argparse
import base64
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import time


DEVICE = Path("/dev/i2c-1")
BUS = Path("/sys/bus/i2c/devices/i2c-1")
CLIENT = Path("/sys/bus/i2c/devices/1-0068")
DUMMY = Path("/sys/bus/i2c/drivers/dummy")
ADAPTER = Path("/sys/class/i2c-dev/i2c-1")
PLATFORM_DRIVERS = Path("/sys/bus/platform/drivers")
SYSTEM_DEVICES = Path("/sys/devices")
ACL = "system.posix_acl_access"


def acl_bytes(path):
    try:
        return os.getxattr(path, ACL)
    except OSError as exc:
        if exc.errno != errno.ENODATA:
            raise
        return None


def device_identity(info):
    return info.st_dev, info.st_ino, info.st_rdev, info.st_uid, info.st_gid


class Guard:
    def __init__(self, case):
        self.case, self.changed, self.saved = case, False, {}

    def prepare(self):
        if self.case == "permission":
            info = DEVICE.lstat()
            if not stat.S_ISCHR(info.st_mode) or (os.major(info.st_rdev), os.minor(info.st_rdev)) != (89, 1):
                raise ValueError("unexpected device")
            acl = acl_bytes(DEVICE)
            self.saved = {"identity": list(device_identity(info)), "mode": stat.S_IMODE(info.st_mode),
                          "acl": None if acl is None else base64.b64encode(acl).decode()}
        elif self.case == "driver-bound":
            if CLIENT.exists() or CLIENT.is_symlink() or not DUMMY.is_dir():
                raise ValueError("existing client or dummy driver unavailable")
            if not (BUS / "new_device").is_file() or not (BUS / "delete_device").is_file():
                raise ValueError("client lifecycle unavailable")
        elif self.case == "adapter-disabled":
            # i2c-dev's device link resolves to the adapter. Its parent is the
            # platform controller; newer kernels need not expose i2c-adapter.
            adapter_device = (ADAPTER / "device").resolve(strict=True)
            controller = adapter_device.parent
            driver = (controller / "driver").resolve(strict=True)
            if (adapter_device.name != "i2c-1" or driver != PLATFORM_DRIVERS / "i2c-bcm2835"
                    or not re.fullmatch(r"[0-9a-f]+\.i2c", controller.name)
                    or not controller.is_relative_to(SYSTEM_DEVICES)
                    or not all((driver / name).is_file() for name in ("bind", "unbind"))):
                raise ValueError("controller restoration unavailable")
            self.saved = {"controller": str(controller), "driver": str(driver)}
        else:
            raise ValueError("unsupported fault case")

    def apply(self):
        if self.case == "permission":
            self.changed = True  # Also restore if chmod acknowledgement is lost.
            DEVICE.chmod(0)
        elif self.case == "driver-bound":
            (BUS / "new_device").write_text("dummy 0x68\n")
            self.changed = True
            if (CLIENT / "driver").resolve(strict=True) != DUMMY:
                raise ValueError("unexpected client driver")
            self.saved["client_inode"] = CLIENT.stat().st_ino
        else:
            # A failed write can have reached the kernel. Restoration checks
            # the current binding rather than repeating the unbind operation.
            self.changed = True
            (Path(self.saved["driver"]) / "unbind").write_text(Path(self.saved["controller"]).name)
            if ADAPTER.exists():
                raise ValueError("adapter remained present")

    def restore(self):
        if not self.changed:
            return
        if self.case == "permission":
            if list(device_identity(DEVICE.lstat())) != self.saved["identity"]:
                raise ValueError("device changed; operator recovery required")
            DEVICE.chmod(self.saved["mode"])
            acl = self.saved["acl"]
            if acl is not None:
                os.setxattr(DEVICE, ACL, base64.b64decode(acl))
            elif acl_bytes(DEVICE) is not None:
                os.removexattr(DEVICE, ACL)
            if stat.S_IMODE(DEVICE.stat().st_mode) != self.saved["mode"]:
                raise ValueError("mode restoration failed")
            if acl_bytes(DEVICE) != (None if acl is None else base64.b64decode(acl)):
                raise ValueError("ACL restoration failed")
        elif self.case == "driver-bound":
            if not CLIENT.exists():
                self.changed = False
                return
            if ((CLIENT / "driver").resolve(strict=True) != DUMMY
                    or ("client_inode" in self.saved and CLIENT.stat().st_ino != self.saved["client_inode"])):
                raise ValueError("client changed; deletion withheld")
            (BUS / "delete_device").write_text("0x68\n")
            if CLIENT.exists():
                raise ValueError("client restoration failed")
        else:
            controller, driver = Path(self.saved["controller"]), Path(self.saved["driver"])
            binding = controller / "driver"
            if binding.exists():
                if binding.resolve() != driver:
                    raise ValueError("controller binding changed")
            else:
                (driver / "bind").write_text(controller.name)
            if not ADAPTER.exists() or (ADAPTER / "device").resolve().parent != controller:
                raise ValueError("adapter restoration failed")
        self.changed = False


def private_json(path, value, uid):
    temporary = path.with_suffix(".writing")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), uid, -1)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=("permission", "driver-bound", "adapter-disabled"))
    parser.add_argument("uid", type=int)
    parser.add_argument("nonce")
    parser.add_argument("expected_boot_id")
    parser.add_argument("expected_adapter_fingerprint")
    args = parser.parse_args()
    if (os.geteuid() != 0 or args.uid <= 0 or str(args.uid) != os.environ.get("SUDO_UID")
            or not re.fullmatch("[0-9a-f]{32}", args.nonce)):
        raise SystemExit("Explicit interactive sudo invocation required")
    current_boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    adapter = Path("/sys/class/i2c-dev/i2c-1")
    adapter_name = (adapter / "name").read_text().strip()
    fingerprint = hashlib.sha256((current_boot + "\n" + str(adapter.resolve(strict=True))
                                  + "\n" + adapter_name).encode()).hexdigest()
    if current_boot != args.expected_boot_id or fingerprint != args.expected_adapter_fingerprint:
        raise SystemExit("Device binding changed; refusing administrator preparation")
    runtime = Path("/run/user") / str(args.uid)
    info = runtime.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != args.uid or stat.S_IMODE(info.st_mode) & 0o077:
        raise SystemExit("Private operator runtime directory unavailable")
    status = runtime / ("embedsleuth-fault-" + args.nonce + ".json")
    release = runtime / ("embedsleuth-fault-" + args.nonce + ".release")
    recovery = Path("/run") / ("embedsleuth-fault-" + args.nonce + ".recovery.json")
    if any(path.exists() or path.is_symlink() for path in (status, release, recovery)):
        raise SystemExit("Existing test state; refusing reuse")
    lock = os.open("/run/lock/embedsleuth-fault-acceptance.lock",
                   os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    lock_info = os.fstat(lock)
    if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != 0
            or lock_info.st_nlink != 1 or stat.S_IMODE(lock_info.st_mode) & 0o077):
        raise SystemExit("Unsafe administrator lock")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def interrupted(*_):
        raise KeyboardInterrupt
    for event in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGALRM):
        signal.signal(event, interrupted)
    guard = Guard(args.case)
    restored, failure, stage, error = False, False, "prepare", None
    try:
        guard.prepare()
        private_json(recovery, {"case": args.case, "saved": guard.saved}, 0)
        # Lease includes apply time and does not depend on the PC driver.
        deadline = time.monotonic() + 60
        signal.alarm(60)
        stage = "apply"
        guard.apply()
        stage = "active"
        private_json(status, {"case": args.case, "phase": "ready", "lease_seconds": 60}, args.uid)
        print("EmbedSleuth test condition active; automatic restoration within 60 seconds.", flush=True)
        while time.monotonic() < deadline:
            if release.exists():
                break
            time.sleep(0.1)
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        failure = True
        error = {"stage": stage, "kind": type(exc).__name__, "errno": getattr(exc, "errno", None)}
    finally:
        signal.alarm(0)
        # Preserve the recovery file on failure. Do not hide restoration errors.
        try:
            guard.restore()
            restored = True
        except (OSError, ValueError) as exc:
            failure = True
            error = {"stage": "restore", "kind": type(exc).__name__, "errno": getattr(exc, "errno", None)}
        private_json(status, {"case": args.case, "phase": "restored" if restored else "recovery_required",
                              "test_setup_failed": failure, "error": error,
                              "recovery_file": str(recovery)}, args.uid)
        if restored:
            recovery.unlink(missing_ok=True)
        os.close(lock)
    message = ("EmbedSleuth test preparation failed; original state unchanged or restored." if failure else
               "EmbedSleuth restoration confirmed.") if restored else "STOP: operator recovery required."
    print(message, flush=True)
    return 1 if failure or not restored else 0


if __name__ == "__main__":
    raise SystemExit(main())

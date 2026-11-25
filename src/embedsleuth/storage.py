"""Owner-only, durable local state and audit storage for Linux hosts.

Only JSON-compatible values cross this boundary. Run/target IDs are logical
identifiers, never paths. Callers hold target_lock then run_lock around remote
operations, append a ``before`` event durably before dispatch, and an ``after``
event after receiving a terminal result. Unfinished operations are never replayed.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Iterator
from uuid import uuid4


class StorageError(ValueError):
    """Unsafe storage layout or malformed persisted data."""


class StoreBusyError(StorageError):
    """Another process or thread owns the requested operation lock."""


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
_SECRET_KEY = re.compile(
    r"(?:password|passwd|passphrase|secret|token|authorization|credential|private.?key|api.?key|access.?key|(?:^|[_-])key(?:$|[_-]))",
    re.IGNORECASE,
)
_PEM = re.compile(r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?(?:-----END [^-\r\n]*PRIVATE KEY-----|\Z)", re.S)
_INLINE_SECRET = re.compile(
    r"(?i)\b(password|passwd|passphrase|token|api[_-]?key|secret)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_API_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_PUBLIC_HOST_KEY_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_MAX_JSON_BYTES = 8 * 1024 * 1024
_MAX_AUDIT_BYTES = 32 * 1024 * 1024


def _identifier(value: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise StorageError("invalid storage identifier")
    return value


def _clean_text(value: str) -> str:
    value = _CONTROL.sub("", _ANSI.sub("", value))
    value = _PEM.sub("[REDACTED PRIVATE KEY]", value)
    value = _INLINE_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    return _API_TOKEN.sub("[REDACTED]", value)


def sanitize(value: Any, *, _depth: int = 0) -> Any:
    """Copy JSON values, redact sensitive fields and strip terminal controls.

    This is defense in depth, not arbitrary secret detection. Callers must never
    put raw credentials or unfiltered SSH command output into observations.
    """
    if _depth > 64:
        raise StorageError("stored JSON exceeds nesting limit")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise StorageError("non-finite JSON number")
        return value
    if type(value) is str:
        return _clean_text(value)
    if type(value) is list:
        return [sanitize(item, _depth=_depth + 1) for item in value]
    if type(value) is dict:
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise StorageError("JSON object keys must be strings")
            clean_key = _clean_text(key)
            if clean_key in cleaned:
                raise StorageError("sanitization would collide JSON keys")
            public_fingerprint = (
                key == "host_key_fingerprint" and type(item) is str
                and _PUBLIC_HOST_KEY_FINGERPRINT.fullmatch(item) is not None
            )
            sensitive = (_SECRET_KEY.search(clean_key) or clean_key.lower() == "key") and not public_fingerprint
            cleaned[clean_key] = "[REDACTED]" if sensitive else sanitize(item, _depth=_depth + 1)
        return cleaned
    raise StorageError("storage accepts only JSON-compatible values")


def _encode(value: dict[str, Any]) -> bytes:
    if type(value) is not dict:
        raise StorageError("stored record must be a JSON object")
    encoded = json.dumps(sanitize(value), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > _MAX_JSON_BYTES:
        raise StorageError("stored JSON exceeds size limit")
    return encoded


def _check_owner(info: os.stat_result, *, directory: bool) -> None:
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind_ok or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise StorageError("storage must be owner-only and owned by the current user")
    if not directory and info.st_nlink != 1:
        raise StorageError("hard-linked storage files are forbidden")


def _write_all(fd: int, data: bytes) -> None:
    while data:
        count = os.write(fd, data)
        if count <= 0:
            raise OSError("short storage write")
        data = data[count:]


class RunStore:
    """Durable local store rooted at an explicitly selected private directory.

    The final root directory is created with mode 0700 if missing. Its parent
    must exist. Existing root permissions are checked, never silently changed.
    File operations use directory descriptors and O_NOFOLLOW throughout.
    """

    def __init__(self, root: str | os.PathLike[str]):
        path = Path(root).absolute()
        if ".." in path.parts or path == Path("/"):
            raise StorageError("invalid storage root")
        self.root = path
        self._read_only = False
        self._local = threading.local()
        self._root_fd = -1
        parent_fd = self._open_absolute_directory(path.parent)
        try:
            try:
                os.mkdir(path.name, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError:
                pass
            self._root_fd = os.open(path.name, _OPEN_DIRECTORY, dir_fd=parent_fd)
            _check_owner(os.fstat(self._root_fd), directory=True)
            for name in ("runs", "locks", "quarantine"):
                child = self._directory(self._root_fd, name, create=True)
                os.close(child)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(parent_fd)
        try:
            for run_id in self.list_runs():
                try:
                    self.recover_pending(run_id)
                except StoreBusyError:
                    # A live server still owns this run; target locking prevents
                    # another server from dispatching concurrent remote work.
                    continue
        except BaseException:
            self.close()
            raise

    @classmethod
    def open_reader(cls, root: str | os.PathLike[str]) -> RunStore:
        """Open existing storage without creating, locking or recovering runs.

        Snapshots can race a live writer. Readers must detect inconsistent
        generations and report uncertainty rather than repairing the store.
        """
        path = Path(root)
        if not path.is_absolute() or ".." in path.parts or path == Path("/"):
            raise StorageError("invalid storage root")
        reader = cls.__new__(cls)
        reader.root = path
        reader._local = threading.local()
        reader._read_only = True
        reader._root_fd = -1
        try:
            reader._root_fd = cls._open_absolute_directory(path)
            _check_owner(os.fstat(reader._root_fd), directory=True)
        except BaseException:
            reader.close()
            raise
        return reader

    @staticmethod
    def _open_absolute_directory(path: Path) -> int:
        fd = os.open("/", _OPEN_DIRECTORY)
        try:
            for part in path.parts[1:]:
                next_fd = os.open(part, _OPEN_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def close(self) -> None:
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __enter__(self) -> RunStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        if getattr(self, "_root_fd", -1) >= 0:
            self.close()

    @staticmethod
    def _directory(parent_fd: int, name: str, *, create: bool = False) -> int:
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError:
                pass
        fd = os.open(name, _OPEN_DIRECTORY, dir_fd=parent_fd)
        try:
            _check_owner(os.fstat(fd), directory=True)
        except BaseException:
            os.close(fd)
            raise
        return fd

    @contextmanager
    def _area(self, name: str) -> Iterator[int]:
        if self._root_fd < 0:
            raise StorageError("store is closed")
        _check_owner(os.fstat(self._root_fd), directory=True)
        fd = self._directory(self._root_fd, name)
        try:
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def _run(self, run_id: str) -> Iterator[int]:
        _identifier(run_id)
        with self._area("runs") as runs_fd:
            fd = self._directory(runs_fd, run_id)
            try:
                yield fd
            finally:
                os.close(fd)

    @contextmanager
    def _lock(self, kind: str, identifier: str) -> Iterator[None]:
        if self._read_only:
            raise StorageError("read-only store")
        _identifier(identifier)
        name = f"{kind}-{identifier}.lock"
        held = getattr(self._local, "held", None)
        if held is None:
            held = self._local.held = {}
        if name in held:
            held[name] += 1
            try:
                yield
            finally:
                held[name] -= 1
            return
        with self._area("locks") as locks_fd:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=locks_fd)
            try:
                _check_owner(os.fstat(fd), directory=False)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise StoreBusyError(f"{kind} is busy") from exc
                held[name] = 1
                try:
                    yield
                finally:
                    del held[name]
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def run_lock(self, run_id: str):
        return self._lock("run", run_id)

    def target_lock(self, target_id: str):
        return self._lock("target", target_id)

    @staticmethod
    def _read_file(directory_fd: int, filename: str, *, limit: int) -> bytes:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            _check_owner(os.fstat(fd), directory=False)
            pieces = []
            length = 0
            while True:
                chunk = os.read(fd, min(65536, limit + 1 - length))
                if not chunk:
                    return b"".join(pieces)
                pieces.append(chunk)
                length += len(chunk)
                if length > limit:
                    raise StorageError("stored file exceeds size limit")
        finally:
            os.close(fd)

    @staticmethod
    def _decode(data: bytes) -> dict[str, Any]:
        try:
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise StorageError("duplicate stored JSON key")
                    result[key] = value
                return result
            value = json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(StorageError("invalid JSON number")))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise StorageError("malformed stored JSON") from exc
        if type(value) is not dict:
            raise StorageError("stored record must be a JSON object")
        return sanitize(value)

    @staticmethod
    def _atomic_json(directory_fd: int, filename: str, value: dict[str, Any]) -> None:
        data = _encode(value)
        # Reject an existing symlink rather than replacing it silently.
        try:
            _check_owner(os.stat(filename, dir_fd=directory_fd, follow_symlinks=False), directory=False)
        except FileNotFoundError:
            pass
        temporary = f".pending-{uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory_fd)
        try:
            _write_all(fd, data)
            os.fsync(fd)
            os.replace(temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    def create(self, run_id: str, state: dict[str, Any]) -> None:
        _identifier(run_id)
        _encode(state)
        with self.run_lock(run_id), self._area("runs") as runs_fd:
            os.mkdir(run_id, 0o700, dir_fd=runs_fd)
            os.fsync(runs_fd)
            with self._run(run_id) as run_fd:
                self._atomic_json(run_fd, "state.json", state)

    def load_state(self, run_id: str) -> dict[str, Any]:
        with self._run(run_id) as run_fd:
            return self._decode(self._read_file(run_fd, "state.json", limit=_MAX_JSON_BYTES))

    def save_state(self, run_id: str, state: dict[str, Any]) -> None:
        with self.run_lock(run_id), self._run(run_id) as run_fd:
            self._atomic_json(run_fd, "state.json", state)

    def save_report(self, run_id: str, report: dict[str, Any]) -> None:
        with self.run_lock(run_id), self._run(run_id) as run_fd:
            self._atomic_json(run_fd, "report.json", report)

    def load_report(self, run_id: str) -> dict[str, Any]:
        with self._run(run_id) as run_fd:
            return self._decode(self._read_file(run_fd, "report.json", limit=_MAX_JSON_BYTES))

    def list_runs(self) -> list[str]:
        with self._area("runs") as runs_fd:
            names = os.listdir(runs_fd)
            for name in names:
                _identifier(name)
                fd = self._directory(runs_fd, name)
                os.close(fd)
            return sorted(names)

    def read_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._run(run_id) as run_fd:
            try:
                data = self._read_file(run_fd, "audit.jsonl", limit=_MAX_AUDIT_BYTES)
            except FileNotFoundError:
                return []
        # A crash can leave one partial tail. Only newline-terminated records
        # prove that append_event finished writing its JSON representation.
        return [self._decode(line) for line in data.split(b"\n")[:-1]]

    def append_event(self, run_id: str, event: dict[str, Any]) -> None:
        data = _encode(event)
        with self.run_lock(run_id), self._run(run_id) as run_fd:
            fd = os.open("audit.jsonl", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=run_fd)
            try:
                _check_owner(os.fstat(fd), directory=False)
                if os.fstat(fd).st_size > _MAX_AUDIT_BYTES:
                    raise StorageError("audit exceeds size limit")
                existing = self._read_file(run_fd, "audit.jsonl", limit=_MAX_AUDIT_BYTES)
                complete_length = existing.rfind(b"\n") + 1
                for line in existing[:complete_length].split(b"\n")[:-1]:
                    self._decode(line)
                if len(existing) != complete_length:
                    os.ftruncate(fd, complete_length)
                if complete_length + len(data) > _MAX_AUDIT_BYTES:
                    raise StorageError("audit exceeds size limit")
                os.lseek(fd, 0, os.SEEK_END)
                _write_all(fd, data)
                os.fsync(fd)
                os.fsync(run_fd)
            finally:
                os.close(fd)

    def quarantine_target(self, target_id: str, reason: str) -> None:
        _identifier(target_id)
        if type(reason) is not str or not reason:
            raise StorageError("quarantine requires a reason")
        # Quarantine is independent of remote-operation locking: an interrupted
        # run must remain blocked even if a different live run owns the target.
        with self._lock("quarantine", target_id), self._area("quarantine") as quarantine_fd:
            self._atomic_json(quarantine_fd, f"{target_id}.json", {
                "target_id": target_id, "reason": reason, "error_code": "remote_state_unknown",
            })

    def is_quarantined(self, target_id: str) -> bool:
        _identifier(target_id)
        with self._area("quarantine") as quarantine_fd:
            try:
                record = self._decode(self._read_file(quarantine_fd, f"{target_id}.json", limit=_MAX_JSON_BYTES))
            except FileNotFoundError:
                return False
            if record.get("target_id") != target_id or record.get("error_code") != "remote_state_unknown":
                raise StorageError("invalid quarantine record")
            return True

    def request_cancel(self, run_id: str) -> None:
        """Persist cancellation without waiting for a remote worker's run lock."""
        _identifier(run_id)
        with self._lock("cancel", run_id), self._run(run_id) as run_fd:
            self._atomic_json(run_fd, "cancel.json", {"run_id": run_id, "cancelled": True})

    def cancel_requested(self, run_id: str) -> bool:
        _identifier(run_id)
        with self._run(run_id) as run_fd:
            try:
                record = self._decode(self._read_file(run_fd, "cancel.json", limit=4096))
            except FileNotFoundError:
                return False
            if record != {"run_id": run_id, "cancelled": True}:
                raise StorageError("invalid cancellation marker")
            return True

    def recover_target(self, target_id: str) -> None:
        """Recover all target runs under its cross-process dispatch lock.

        A store may have started while another process still owned a run, then
        survived that process crashing. Startup recovery alone cannot detect
        that later crash. Call this before dispatching any new target operation.
        MVP uses an O(number of runs) scan; corrupt states fail closed, and busy
        runs propagate StoreBusyError rather than being silently skipped.
        """
        _identifier(target_id)
        with self.target_lock(target_id):
            for run_id in self.list_runs():
                state = self.load_state(run_id)
                registered_target = _identifier(state.get("target_id"))
                if registered_target == target_id:
                    self.recover_pending(run_id)

    def recover_pending(self, run_id: str) -> dict[str, Any]:
        """Conservatively block interrupted remote operations without replay."""
        with self.run_lock(run_id):
            state = self.load_state(run_id)
            pending: set[str] = set()
            completed: set[str] = set()
            journal_unknown = False
            for event in self.read_events(run_id):
                operation_id = event.get("operation_id", event.get("execution_id"))
                if event.get("action") == "before":
                    operation_id = _identifier(operation_id)
                    pending.add(operation_id)
                    completed.discard(operation_id)
                elif event.get("action") == "after":
                    operation_id = _identifier(operation_id)
                    pending.discard(operation_id)
                    completed.add(operation_id)
                    journal_unknown |= event.get("remote_state_unknown") is True
            marker = state.get("pending_operation")
            if marker is not None:
                if type(marker) is not dict:
                    raise StorageError("invalid pending operation marker")
                operation_id = _identifier(marker.get("operation_id"))
                results = [
                    record.get("result") for record in state.get("evidence", [])
                    if type(record) is dict and type(record.get("result")) is dict
                    and record["result"].get("request_id") == operation_id
                ]
                durable = operation_id in completed and bool(results)
                if marker.get("tool") == "read_device_identity":
                    operation = state.get("operations", {}).get(operation_id)
                    durable = durable and type(operation) is dict and (
                        operation.get("arguments") == marker.get("arguments")
                        and operation.get("result") in results
                    )
                if durable:
                    # Crash after the terminal journal fsync but before clearing
                    # this marker. Do not let a later denial overwrite its cache.
                    state["pending_operation"] = None
                    self.save_state(run_id, state)
                else:
                    pending.add(operation_id)
            # A terminal audit event cannot turn persisted "running" into
            # "idle". Inconsistent state is conservative evidence of an
            # interrupted commit, not permission to retry an operation.
            if not pending and state.get("remote_state") not in ("running", "unknown") and not journal_unknown:
                return state
            target_id = _identifier(state.get("target_id"))
            quarantined = self.is_quarantined(target_id)
            if not quarantined:
                self.quarantine_target(target_id, "Interrupted remote operation; completion is unconfirmed")
            if (quarantined and state.get("state") == "blocked" and state.get("remote_state") == "unknown"
                    and state.get("error_code") == "remote_state_unknown"
                    and state.get("interrupted_operations") == sorted(pending)):
                return state
            state.update(state="blocked", remote_state="unknown", error_code="remote_state_unknown",
                         interrupted_operations=sorted(pending))
            if marker is not None:
                # The old intent is now an interrupted audit fact. It must not
                # be mistaken for a newly active operation by a later denial.
                state["pending_operation"] = None
            self.save_state(run_id, state)
            self.append_event(run_id, {"action": "recovery", "operation_ids": sorted(pending),
                                       "error_code": "remote_state_unknown"})
            return state

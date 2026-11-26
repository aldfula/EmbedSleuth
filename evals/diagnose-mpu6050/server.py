"""Evaluation-only MCP backend: real core, static fixture, ZERO SSH/hardware.

The evaluator supplies only fixture data to the server. Expected traces and
report grading criteria are never sent to the model. Not shipped as runtime.
"""

import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import threading

from embedsleuth.core import DiagnosticCore
from embedsleuth.mcp_server import serve
from embedsleuth.storage import RunStore
from embedsleuth.targets import RegisteredTarget, TargetRegistry
from embedsleuth.transport import ProbeResponse


class FixtureTransport:
    def __init__(self, fixture, record):
        self.fixture, self.record = fixture, record

    def execute(self, target, request, **limits):
        self.record({"kind": "fake_remote", "operation": request["op"], "address": request.get("address")})
        override = self.fixture.get("responses", {}).get(request["op"])
        if override is not None:
            return ProbeResponse(**deepcopy(override))
        observations = {"boot_id": "evaluation-boot", "host_key_fingerprint": "sha256:" + "a" * 64}
        if request["op"] == "inspect_target":
            observations["board_model"] = "Raspberry Pi 3 Model B Rev 1.2"
        else:
            observations.update({"bus": 1, "adapter_present": True, "access_allowed": True,
                "driver_bound": {"104": False, "105": False}, "combined_transfer_supported": True,
                "dependency_available": True, "adapter_fingerprint": "b" * 64})
            if request["op"] == "read_device_identity":
                observations.update({"address": request["address"], "register": 117, "identity_value": 104})
        observations.update(deepcopy(self.fixture.get("observations", {}).get(request["op"], {})))
        return ProbeResponse(observations=observations)


class RecordedCore(DiagnosticCore):
    def __init__(self, *args, record, **kwargs):
        super().__init__(*args, **kwargs)
        self.record = record

    def call(self, tool_name, arguments):
        self.record({"kind": "tool", "tool": tool_name, "arguments": arguments})
        return super().call(tool_name, arguments)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    directory = args.directory
    directory.mkdir(mode=0o700, exist_ok=True)
    trust = directory / "fixture-trust"
    trust.write_text("evaluation only: not an SSH trust file\n")
    trust.chmod(0o600)
    values = {"target_id": "lab-pi", "host": "192.0.2.10", "username": "fixture",
              "known_hosts_path": str(trust), "conflicting_use_excluded": True}
    values.update(fixture.get("target", {}))
    target = RegisteredTarget.model_validate_json(json.dumps(values))
    registry, lock = TargetRegistry((target,)), threading.Lock()

    def record(value):
        data = (json.dumps(value, allow_nan=False) + "\n").encode()
        with lock:
            fd = os.open(directory / "calls.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "ab") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                raise

    with RunStore(directory / "state") as store:
        core = RecordedCore(registry, FixtureTransport(fixture, record), store, record=record)
        asyncio.run(serve(core))


if __name__ == "__main__":
    main()

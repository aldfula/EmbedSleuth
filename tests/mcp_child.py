"""Offline subprocess fixture; this fake transport is never shipped in the wheel."""

import asyncio
from pathlib import Path
import sys
import time

from embedsleuth.config import load_configuration
from embedsleuth.core import DiagnosticCore
from embedsleuth.mcp_server import serve
from embedsleuth.storage import RunStore
from embedsleuth.targets import TargetRegistry
from embedsleuth.transport import ProbeResponse


class Transport:
    def __init__(self, started):
        self.started = started

    def execute(self, target, request, *, cancelled, **limits):
        # Exercise SDK stdout isolation from a sync worker as well as asyncio.
        print("fixture diagnostic log", flush=True)
        self.started.write_text("started")
        deadline = time.monotonic() + 5
        while not cancelled():
            if time.monotonic() >= deadline:
                raise RuntimeError("offline fixture cancellation was not delivered")
            time.sleep(0.01)
        return ProbeResponse(status="error", error_code="remote_state_unknown",
                             message="Cancelled with unconfirmed remote completion", remote_state_unknown=True)


def main():
    config = load_configuration(Path(sys.argv[1]))
    with RunStore(config.state_directory) as store:
        core = DiagnosticCore(TargetRegistry(config.targets), Transport(Path(sys.argv[2])), store)
        try:
            asyncio.run(serve(core))
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass


if __name__ == "__main__":
    main()

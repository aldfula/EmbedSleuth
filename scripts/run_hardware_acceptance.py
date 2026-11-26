"""Opt-in real hardware acceptance through production STDIO MCP. Passive by default.

This is a development check, not an AI host or an alternative SSH path. Reads
exactly one registered target and keeps its existing state/quarantine store.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from embedsleuth.acceptance import summarize
from embedsleuth.config import load_configuration
from embedsleuth.contracts import DiagnosticReport
from embedsleuth.storage import RunStore
from embedsleuth.targets import TargetRegistry


class AcceptanceError(ValueError):
    pass


def check_request(config, target_id, address, exclusive):
    target = TargetRegistry(config.targets).get(target_id)
    if address is not None and (
        type(address) is not int or address not in (104, 105) or address not in target.expected_addresses
        or exclusive is not True or target.conflicting_use_excluded is not True
    ):
        raise AcceptanceError("Identity acceptance requires a registered address and current exclusive-use confirmation")
    return target


async def exercise(client, target, *, address=None):
    """No hardware retry. On a failed inspection, go directly to reporting."""
    async def call(name, **arguments):
        response = await client.call_tool(name, arguments)
        body = response.structured_content
        if (type(body) is not dict or json.loads(response.content[0].text) != body
                or body.get("schema_version") != "1.0"):
            raise AcceptanceError("Malformed MCP acceptance response")
        if body["result"] is None:
            raise AcceptanceError("MCP acceptance could not obtain a diagnostic result")
        return body["result"], body["error"]

    await call("get_component_profile", profile_id=target.profile_id)
    run, error = await call("start_diagnosis", target_id=target.target_id, profile_id=target.profile_id,
                            mode="passive" if address is None else "read_only")
    if error:
        raise AcceptanceError("MCP acceptance could not start the registered diagnosis")
    run_id = run["run_id"]
    board, error = await call("inspect_target", run_id=run_id)
    if error is None and board["status"] == "ok" and not board["truncated"]:
        environment, error = await call("inspect_i2c_environment", run_id=run_id)
        if address is not None and error is None and environment["status"] == "ok" and not environment["truncated"]:
            # All real preconditions still go through the core and Pi policy.
            await call("read_device_identity", run_id=run_id,
                       bus_ref=f"bus-{target.expected_bus}", address=address, operation_id="acceptance-identity")
    await call("evaluate_findings", run_id=run_id)
    report, _ = await call("finalize_report", run_id=run_id)
    return DiagnosticReport.model_validate_json(json.dumps(report))


async def run(config_path, target_id, address=None, exclusive=False):
    config = load_configuration(config_path)
    target = check_request(config, target_id, address, exclusive)
    # Inspect existing quarantine first without creating/recovering a store.
    with RunStore.open_reader(config.state_directory) as store:
        if store.is_quarantined(target.target_id):
            raise AcceptanceError("Registered target is quarantined; do not change target ID or store")
    parameters = StdioServerParameters(command=sys.executable,
        args=["-I", "-m", "embedsleuth.mcp_server", "--config", str(config_path)])
    async with Client(parameters, read_timeout_seconds=20) as client:
        report = await exercise(client, target, address=address)
    with RunStore.open_reader(config.state_directory) as store:
        saved = DiagnosticReport.model_validate_json(json.dumps(store.load_report(report.run_id)))
        state = store.load_state(report.run_id)
        if saved != report or state["pending_operation"] is not None:
            raise AcceptanceError("Acceptance report is not a closed durable snapshot")
    checks = summarize(report)
    assessments = [checks[name]["assessment"] for name in ("execution", "probe_memory", "tool_time")]
    if address is not None:
        assessments.append(checks["identity"]["assessment"])
    verdict = "fail" if "fail" in assessments else "unknown" if "unknown" in assessments else "pass"
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "mode": "passive" if address is None else "read_only",
            "deterministic_checks": verdict, "mcp_session": "closed", "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--identity-address", type=lambda value: int(value, 0), choices=(104, 105))
    parser.add_argument("--confirm-exclusive-i2c", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.config, args.target, args.identity_address, args.confirm_exclusive_i2c))
    except (ValueError, OSError, KeyError, TypeError):
        parser.exit(1, "Hardware acceptance did not complete; review registered configuration and saved evidence\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return {"pass": 0, "fail": 1, "unknown": 3}[result["deterministic_checks"]]


if __name__ == "__main__":
    sys.exit(main())

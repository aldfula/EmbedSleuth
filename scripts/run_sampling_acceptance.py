"""Interactive real sensor acceptance through a fresh production STDIO MCP.

Run this in the operator's own terminal. Approval is delegated to the production
interactive approval command; this client cannot create or simulate approval.
No alternative SSH path, fault injection, automatic retry or model call exists.
"""

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from embedsleuth.config import load_configuration
from embedsleuth.contracts import DiagnosticReport
from embedsleuth.storage import RunStore
from embedsleuth.targets import TargetRegistry


class AcceptanceError(ValueError):
    pass


async def call(client, name, **arguments):
    response = await client.call_tool(name, arguments)
    body = response.structured_content
    if (type(body) is not dict or body.get("schema_version") != "1.0"
            or not response.content or json.loads(response.content[0].text) != body):
        raise AcceptanceError("Malformed MCP result; no hardware retry")
    return body


def require(body):
    result = body.get("result")
    if (body.get("error") or result is None
            or (isinstance(result, dict) and (result.get("truncated") is True
                or result.get("status") in {"error", "blocked", "unsupported", "timeout"}))):
        raise AcceptanceError("MCP operation stopped; inspect the durable report")
    return result


async def exercise(client, target, address, approve):
    """One run and one sampling call, including on failure. Never grant approval."""
    tools = await client.list_tools()
    if not {"prepare_sensor_sampling", "sample_sensor_data"} <= {tool.name for tool in tools.tools}:
        raise AcceptanceError("Connect a v0.2 MCP server before sensor acceptance")
    require(await call(client, "get_component_profile", profile_id=target.profile_id))
    started = require(await call(client, "start_diagnosis", target_id=target.target_id,
                                 profile_id=target.profile_id, mode="sensor_check"))
    run_id = started["run_id"]
    sampling = None
    sampling_attempted = False
    stopped = False
    try:
        require(await call(client, "inspect_target", run_id=run_id))
        require(await call(client, "inspect_i2c_environment", run_id=run_id))
        identity = require(await call(client, "read_device_identity", run_id=run_id,
                                      bus_ref=f"bus-{target.expected_bus}", address=address,
                                      operation_id="sampling-acceptance-identity"))
        if identity.get("assessment") != "pass":
            raise AcceptanceError("Identity did not match; sampling was not attempted")
        plan = require(await call(client, "prepare_sensor_sampling", run_id=run_id,
                                  bus_ref=f"bus-{target.expected_bus}", address=address))
        # This must be the operator's real TTY, not an input supplied by an AI.
        if not await approve(run_id, plan["plan_id"]):
            raise AcceptanceError("Local operator approval was not granted")
        # Approval does not refresh environment evidence or extend the run budget.
        require(await call(client, "inspect_i2c_environment", run_id=run_id))
        sampling_attempted = True
        sampling = require(await call(client, "sample_sensor_data", run_id=run_id,
                                      plan_id=plan["plan_id"], operation_id="sampling-acceptance-data"))
    except AcceptanceError:
        stopped = True
    finally:
        # Closure is local evidence processing; never another hardware check.
        require(await call(client, "evaluate_findings", run_id=run_id))
        report = require(await call(client, "finalize_report", run_id=run_id))
        saved = require(await call(client, "get_report", run_id=run_id))
        if report != saved:
            raise AcceptanceError("Report snapshot changed; review existing evidence")
    observations = (sampling or {}).get("observations", {})
    passed = (not stopped and report["outcome"] == "complete"
              and sampling.get("assessment") == "pass"
              and observations.get("restoration_confirmed") is True)
    return DiagnosticReport.model_validate_json(json.dumps(report)), {
        "assessment": "pass" if passed else "incomplete", "run_id": run_id,
        "outcome": report["outcome"], "mode": "sensor_check",
        "sampling_calls": int(sampling_attempted),
        "wakeup_attempted": observations.get("wakeup_attempted"),
        "restoration_confirmed": observations.get("restoration_confirmed"),
        "decoded": observations.get("decoded"),
        "scope": "real production MCP and operator approval; no model or fault injection",
    }


async def run(config_path, target_id, address):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise AcceptanceError("Run acceptance in the operator's own interactive terminal")
    config = load_configuration(config_path)
    target = TargetRegistry(config.targets).get(target_id)
    if address not in target.expected_addresses:
        raise AcceptanceError("Use an address already allowed by the existing registration")
    if target.conflicting_use_excluded is not True:
        raise AcceptanceError("Operator must first set conflicting_use_excluded=true for this target in the existing registration. No device access performed.")
    with RunStore.open_reader(config.state_directory) as store:
        if store.is_quarantined(target_id):
            raise AcceptanceError("Target is quarantined; do not change its ID or store")
    answer = input(f"Confirm MPU6050 address {address:#04x} and all other I2C programs stopped. Type ready: ")
    if answer != "ready":
        raise AcceptanceError("Current hardware conditions were not confirmed")

    async def approve(run_id, plan_id):
        command = [sys.executable, "-I", "-m", "embedsleuth.sampling_approval", "--config", str(config_path),
                   "--run-id", run_id, "--plan-id", plan_id]
        # Inherit the human operator's stdin/stdout; no pipe or automatic input.
        return await asyncio.to_thread(lambda: subprocess.run(command, check=False).returncode == 0)

    parameters = StdioServerParameters(command=sys.executable,
        args=["-I", "-m", "embedsleuth.mcp_server", "--config", str(config_path)])
    async with Client(parameters, read_timeout_seconds=20) as client:
        report, result = await exercise(client, target, address, approve)
    with RunStore.open_reader(config.state_directory) as store:
        if (DiagnosticReport.model_validate_json(json.dumps(store.load_report(report.run_id))) != report
                or store.load_state(report.run_id)["pending_operation"] is not None):
            raise AcceptanceError("Durable report closure could not be verified")
    result["mcp_session"] = "closed"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--address", type=lambda value: int(value, 0), choices=(104, 105), default=104)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.config, args.target, args.address))
    except AcceptanceError as exc:
        parser.exit(2, str(exc) + "\n")
    except (ValueError, OSError, KeyError, TypeError, EOFError):
        parser.exit(2, "Acceptance stopped. Review existing reports; do not repeat hardware checks automatically.\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["assessment"] == "pass" else 3


if __name__ == "__main__":
    sys.exit(main())

"""Local interactive approval of one fixed sensor plan. No SSH or MCP writes."""

import argparse
import json
from pathlib import Path
import sys

from embedsleuth.config import load_configuration
from embedsleuth.contracts import Identifier
from embedsleuth.sampling import approve_plan
from embedsleuth.storage import RunStore
from embedsleuth.targets import TargetRegistry
from pydantic import TypeAdapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--plan-id", required=True)
    args = parser.parse_args()
    try:
        TypeAdapter(Identifier).validate_python(args.run_id)
        TypeAdapter(Identifier).validate_python(args.plan_id)
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError("Approval requires an interactive operator terminal")
        config = load_configuration(args.config)
        with RunStore.open_reader(config.state_directory) as reader:
            state = reader.load_state(args.run_id)
            saved = state.get("sampling_plan") or {}
            if saved.get("plan_id") != args.plan_id:
                raise ValueError("Requested plan was not found")
            print(json.dumps(saved["plan"], ensure_ascii=False, indent=2))
        print("This may temporarily clear SLEEP, read sensor registers and restore PWR_MGMT_1.")
        print("Restoration cannot be guaranteed after power loss, transport loss or stuck kernel IO.")
        if input("Type approve " + args.plan_id + " to authorize this plan once: ") != "approve " + args.plan_id:
            raise ValueError("Approval was not granted")
        with RunStore.open_existing_writer(config.state_directory) as store:
            approve_plan(store, TargetRegistry(config.targets), args.run_id, args.plan_id)
        print("Plan approved once. No device access was performed by this command.")
    except (OSError, ValueError, EOFError):
        parser.exit(2, "Approval stopped. No new approval granted; review the plan and local configuration.\n")


if __name__ == "__main__":
    main()

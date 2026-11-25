"""Print definitions for review; never install hooks or grant host trust."""

import argparse
import json
from pathlib import Path
import sys

from embedsleuth.hooks import codex_hook_definitions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--hook-state", required=True, type=Path)
    args = parser.parse_args()
    try:
        definitions = codex_hook_definitions(Path(sys.executable), args.config, args.hook_state)
    except ValueError:
        parser.exit(2, "Use absolute paths without parent traversal\n")
    print(json.dumps(definitions, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Skill packaging and wire examples, not a test of natural-language routing."""

import json
from pathlib import Path
import re
import tomllib

import yaml

from embedsleuth.tools import tool_input_schemas, validate_arguments


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/diagnose-mpu6050"


def test_repository_discovery_alias_resolves_to_the_authored_skill():
    discovered = ROOT / ".agents/skills/diagnose-mpu6050"
    assert discovered.is_symlink()
    assert not discovered.readlink().is_absolute()
    assert discovered.resolve() == SKILL.resolve()


def test_skill_identity_metadata_and_dependency_match_host_example():
    text = (SKILL / "SKILL.md").read_text()
    assert text.startswith("---\n")
    metadata = yaml.safe_load(text.split("---", 2)[1])
    assert metadata["name"] == SKILL.name
    assert isinstance(metadata["description"], str) and 0 < len(metadata["description"]) <= 1024
    ui = yaml.safe_load((SKILL / "agents/openai.yaml").read_text())
    assert 25 <= len(ui["interface"]["short_description"]) <= 64
    assert "$" + SKILL.name in ui["interface"]["default_prompt"]
    assert ui["policy"]["allow_implicit_invocation"] is True
    dependency, = ui["dependencies"]["tools"]
    host = tomllib.loads((ROOT / "examples/codex-mcp.toml").read_text())
    assert dependency["type"] == "mcp" and dependency["transport"] == "stdio"
    assert dependency["value"] in host["mcp_servers"]
    server = host["mcp_servers"][dependency["value"]]
    assert server["args"][:3] == ["-I", "-m", "embedsleuth.mcp_server"]
    assert "default_tools_approval_mode" not in server


def test_skill_references_are_local_discoverable_and_do_not_escape_package():
    visited = set()

    def visit(path):
        path = path.resolve()
        assert path.is_relative_to(SKILL.resolve()) and path.is_file()
        if path in visited:
            return
        visited.add(path)
        for destination in re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text()):
            if "://" in destination or destination.startswith("#"):
                continue
            visit(path.parent / destination.split("#")[0])
    visit(SKILL / "SKILL.md")
    assert set(SKILL.rglob("*.md")) == visited


def test_documented_tool_examples_validate_against_live_strict_input_schemas():
    text = (SKILL / "references/tool-contracts.md").read_text()
    block, = re.findall(r"```json\n(.*?)\n```", text, re.S)
    examples = json.loads(block.replace("$run_id", "run-example").replace("$evidence_id", "ev-example"))
    assert {item["tool"] for item in examples} == set(tool_input_schemas())
    for item in examples:
        request = validate_arguments(item["tool"], item["arguments"])
        assert json.loads(request.model_dump_json()) | item["arguments"] == json.loads(request.model_dump_json())

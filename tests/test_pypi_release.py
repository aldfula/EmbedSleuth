"""Publication selects verified distributions without executing their contents."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import tarfile
import zipfile

import pytest
import yaml

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("pypi_artifacts", ROOT / "scripts/verify_pypi_artifacts.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.fixture
def release(tmp_path):
    directory = tmp_path / "download";directory.mkdir()
    wheel = directory / "embedsleuth-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("embedsleuth-0.1.0.dist-info/METADATA", "Name: embedsleuth\nVersion: 0.1.0\n")
    source = directory / "embedsleuth-0.1.0.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        data = b'[project]\nname="embedsleuth"\nversion="0.1.0"\n'
        member = tarfile.TarInfo("embedsleuth-0.1.0/pyproject.toml");member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    (directory / "constraints-runtime.txt").write_text("mcp==2.3.0\n")
    manifest = {"project": "embedsleuth", "version": "0.1.0", "source_commit": "a"*40,
                "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}}
    path = tmp_path / "manifest.json";path.write_text(json.dumps(manifest))
    return directory, path, tmp_path / "upload"


def test_only_verified_distributions_are_selected_not_constraints_or_extra_files(release):
    directory, manifest, output = release
    (directory / "unrelated-secret.txt").write_text("not-a-distribution")
    result = runner.verify(directory, manifest, output)
    assert result["assessment"] == "pass"
    assert {p.name for p in output.iterdir()} == set(result["distributions"])
    assert all((output / name).read_bytes() == (directory / name).read_bytes() for name in result["distributions"])


@pytest.mark.parametrize("change", ["wheel", "source", "constraints", "symlink", "missing", "name", "version", "commit", "extra_manifest"])
def test_bad_release_is_rejected_before_output_is_created(release, change):
    directory, path, output = release
    manifest = json.loads(path.read_text())
    names = list(manifest["files"])
    if change in {"wheel", "source", "constraints"}:
        name = next(n for n in names if n.endswith({"wheel": ".whl", "source": ".tar.gz", "constraints": ".txt"}[change]))
        (directory / name).write_bytes(b"changed")
    if change == "symlink":
        original = directory / names[0];saved = directory / "saved";original.rename(saved);original.symlink_to(saved)
    if change == "missing": (directory / names[0]).unlink()
    if change == "name": manifest["project"] = "someone-else"
    if change == "version": manifest["version"] = "../bad"
    if change == "commit": manifest["source_commit"] = "invalid"
    if change == "extra_manifest": manifest["files"]["../outside"] = "a"*64
    path.write_text(json.dumps(manifest))
    with pytest.raises((ValueError, OSError)): runner.verify(directory, path, output)
    assert not output.exists()


def test_consistent_hash_cannot_hide_wrong_package_metadata(release):
    directory, path, output = release
    manifest = json.loads(path.read_text())
    name = next(n for n in manifest["files"] if n.endswith(".whl"))
    with zipfile.ZipFile(directory / name, "w") as archive:
        archive.writestr("embedsleuth-0.1.0.dist-info/METADATA", "Name: other-package\nVersion: 0.1.0\n")
    manifest["files"][name] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="identity"): runner.verify(directory, path, output)
    assert not output.exists()


def test_workflow_never_publishes_by_default_and_limits_oidc_to_publish_job():
    workflow = yaml.safe_load((ROOT / ".github/workflows/pypi-publish.yml").read_text())
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["on"]["workflow_dispatch"]["inputs"]["publish"]["default"] is False
    assert workflow["permissions"] == {"contents": "read"}
    validate, publish = workflow["jobs"]["validate"], workflow["jobs"]["publish"]
    assert publish["needs"] == "validate" and "inputs.publish" in publish["if"]
    assert "refs/heads/main" in publish["if"] and "refs/heads/main" in validate["if"]
    assert publish["permissions"] == {"id-token": "write"} and publish["environment"]["name"] == "pypi"
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "uses" in step: assert re.search(r"@[0-9a-f]{40}$", step["uses"])
    assert "secrets." not in json.dumps(workflow)

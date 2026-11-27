"""Select the exact already-tested GitHub release files for PyPI publication.

No credentials, network calls, source execution, rebuilding or version changes.
The repository-reviewed manifest binds bytes, project identity and version.
"""

import argparse
from email.parser import BytesParser
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
import tomllib
import zipfile


def verify(directory, manifest_path, output):
    manifest = json.loads(manifest_path.read_text())
    project, version = manifest["project"], manifest["version"]
    if (project != "embedsleuth" or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None
            or re.fullmatch(r"[0-9a-f]{40}", manifest["source_commit"]) is None):
        raise ValueError("Invalid release identity")
    wheel, source = f"{project}-{version}-py3-none-any.whl", f"{project}-{version}.tar.gz"
    if set(manifest["files"]) != {wheel, source, "constraints-runtime.txt"}:
        raise ValueError("Unexpected release file set")
    verified = {}
    for name, expected in manifest["files"].items():
        path = directory / name
        if (path.is_symlink() or not path.is_file() or path.stat().st_size > 5*1024*1024
                or re.fullmatch(r"[0-9a-f]{64}", expected) is None):
            raise ValueError("Unsafe release file")
        contents = path.read_bytes()
        if hashlib.sha256(contents).hexdigest() != expected:
            raise ValueError("Release checksum mismatch")
        verified[name] = contents
    with zipfile.ZipFile(io.BytesIO(verified[wheel])) as archive:
        metadata = BytesParser().parsebytes(archive.read(f"{project}-{version}.dist-info/METADATA"))
        if (metadata["Name"], metadata["Version"]) != (project, version):
            raise ValueError("Wheel identity differs from release")
        if any(PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts for name in archive.namelist()):
            raise ValueError("Unsafe wheel paths")
    with tarfile.open(fileobj=io.BytesIO(verified[source]), mode="r:gz") as archive:
        member = archive.getmember(f"{project}-{version}/pyproject.toml")
        if not member.isfile():
            raise ValueError("Source metadata must be a regular file")
        config = tomllib.loads(archive.extractfile(member).read().decode())
        if (config["project"]["name"], config["project"]["version"]) != (project, version):
            raise ValueError("Source identity differs from release")
    # Verify everything before creating output, and write the verified bytes.
    # Only the two Python distributions belong in the publisher's directory.
    output.mkdir(mode=0o700)
    for name in (wheel, source):
        with (output / name).open("xb") as stream:
            stream.write(verified[name])
    return {"project": project, "version": version, "source_commit": manifest["source_commit"],
            "distributions": [wheel, source], "assessment": "pass"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify(args.directory, args.manifest, args.output)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, zipfile.BadZipFile):
        parser.exit(1, "PyPI artifact verification failed; nothing may be published\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

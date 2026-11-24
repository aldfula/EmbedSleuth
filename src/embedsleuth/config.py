"""Trusted, local-only MCP startup configuration. Never supplied by a tool call."""

import os
from pathlib import Path
import stat
from typing import Annotated, Literal

from pydantic import Field, field_validator

from embedsleuth.contracts import Contract
from embedsleuth.targets import RegisteredTarget, TargetRegistry


class ConfigurationError(ValueError):
    """Safe startup error; deliberately excludes file contents and paths."""


class ServerConfiguration(Contract):
    schema_version: Literal["1.0"] = "1.0"
    state_directory: Path
    targets: Annotated[tuple[RegisteredTarget, ...], Field(max_length=32)]

    @field_validator("state_directory")
    @classmethod
    def absolute_state_directory(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("state directory must be absolute")
        return value


def load_configuration(path: Path) -> ServerConfiguration:
    """Read at most 64 KiB from an owner-controlled regular JSON file.

    0600 or 0644 are acceptable: the file contains references, never passwords
    or private keys. Other users must not be able to replace its contents.
    RunStore separately enforces 0700/0600 on the evidence store.
    """
    try:
        if not path.is_absolute():
            raise ValueError("absolute configuration path required")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o022 or metadata.st_nlink != 1):
                raise ValueError("unsafe configuration file")
            contents = stream.read(65537)
        if len(contents) > 65536:
            raise ValueError("configuration too large")
        # Pydantic's JSON parser otherwise accepts duplicate object keys.
        import json

        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate configuration key")
                result[key] = value
            return result

        json.loads(contents, object_pairs_hook=unique_keys)
        config = ServerConfiguration.model_validate_json(contents)
        TargetRegistry(config.targets)  # Reject duplicate IDs and SSH endpoints.
        return config
    except (OSError, ValueError, RecursionError):
        raise ConfigurationError("Invalid or unsafe local MCP configuration") from None

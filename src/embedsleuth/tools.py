"""MVP tool argument schemas; this module does not execute tools."""

import json
from types import MappingProxyType
from typing import Any, Mapping

from embedsleuth.contracts import (
    Contract, FinalizeReportRequest, IdentityReadRequest, ProfileRequest,
    RunRequest, StartDiagnosisRequest,
)

TOOL_REQUEST_MODELS: Mapping[str, type[Contract]] = MappingProxyType({
    "get_component_profile": ProfileRequest,
    "start_diagnosis": StartDiagnosisRequest,
    "inspect_target": RunRequest,
    "inspect_i2c_environment": RunRequest,
    "read_device_identity": IdentityReadRequest,
    "evaluate_findings": RunRequest,
    "finalize_report": FinalizeReportRequest,
    "get_report": RunRequest,
    "cancel_diagnosis": RunRequest,
})


def validate_arguments(tool_name: str, arguments: Mapping[str, Any]) -> Contract:
    """Validate JSON-compatible arguments without Python coercion of scalar types."""
    try:
        model = TOOL_REQUEST_MODELS[tool_name]
    except KeyError:
        raise ValueError("unknown diagnostic tool") from None
    # JSON is the actual MCP boundary. Strict JSON parsing accepts arrays/dates
    # in their wire form while still rejecting bool/string values for integers.
    return model.model_validate_json(json.dumps(dict(arguments), allow_nan=False))


def tool_input_schemas() -> dict[str, dict[str, Any]]:
    return {name: model.model_json_schema() for name, model in TOOL_REQUEST_MODELS.items()}

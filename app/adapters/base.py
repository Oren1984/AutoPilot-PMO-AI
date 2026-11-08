"""Enterprise adapter interface shared by every integration (demo API, demo UI, Salesforce)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    input_schema: dict
    method: str  # "api" (structured / MCP-style tool) or "ui" (browser automation)
    kind: str = "read"  # read | write | financial | admin

    def tool_definition(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


@dataclass
class AdapterResult:
    data: Any
    method: str
    screenshot: str | None = None
    meta: dict = field(default_factory=dict)


class AdapterError(Exception):
    """Integration failure. `retryable` tells the gateway whether a retry is safe."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False, uncertain: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.uncertain = uncertain  # the request may have reached the system; outcome unknown


class EnterpriseAdapter(Protocol):
    name: str

    def capabilities(self) -> list[ActionSpec]: ...

    def execute(self, action: str, params: dict, context: dict) -> AdapterResult: ...

    def health(self) -> dict: ...


def schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


S = {"type": "string", "maxLength": 200}
I = {"type": "integer", "minimum": 1}  # noqa: E741


def validate_params(spec: ActionSpec, params: Any) -> list[str]:
    """Minimal JSON-schema validation (type, required, additionalProperties, enum, maxLength, minimum)."""
    errors: list[str] = []
    sch = spec.input_schema
    if not isinstance(params, dict):
        return ["parameters must be an object"]
    props = sch.get("properties", {})
    for req in sch.get("required", []):
        if req not in params:
            errors.append(f"missing required parameter '{req}'")
    for key, value in params.items():
        if key not in props:
            if sch.get("additionalProperties") is False:
                errors.append(f"unexpected parameter '{key}'")
            continue
        p = props[key]
        t = p.get("type")
        ok = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }.get(t, True)
        if not ok:
            errors.append(f"parameter '{key}' must be {t}")
            continue
        if "enum" in p and value not in p["enum"]:
            errors.append(f"parameter '{key}' must be one of {p['enum']}")
        if isinstance(value, str) and "pattern" in p and not re.fullmatch(p["pattern"], value):
            errors.append(f"parameter '{key}' has an invalid format")
        if isinstance(value, str) and "maxLength" in p and len(value) > p["maxLength"]:
            errors.append(f"parameter '{key}' exceeds {p['maxLength']} characters")
        if isinstance(value, (int, float)) and "minimum" in p and value < p["minimum"]:
            errors.append(f"parameter '{key}' must be >= {p['minimum']}")
    return errors

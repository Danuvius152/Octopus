"""The persisted Agent fields, independent of credentials and host capabilities."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, NoReturn, TypeGuard

from pydantic import ValidationError

from .harness import has_backend
from .models import SubagentDefinition
from .session_operations import is_utf8_text

_TEXT_FIELDS = ("name", "description", "system_prompt", "tool_allow", "tool_deny")
_NULLABLE_TEXT_FIELDS = ("avatar", "model", "credential_id", "reasoning_effort", "service_tier")
_JSON_FIELDS = ("mcp_servers", "subagents")
AGENT_CONFIGURATION_FIELDS = (*_TEXT_FIELDS, *_NULLABLE_TEXT_FIELDS, "backend", *_JSON_FIELDS)


class AgentConfigurationError(ValueError):
    def __init__(self, agent_id: str, field: str):
        self.agent_id = agent_id
        self.field = field
        super().__init__(f"Agent {agent_id}: invalid configuration field {field}")


def _is_utf8_text(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and is_utf8_text(value)


def _is_json_value(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if not _is_utf8_text(key) or not _is_json_value(item):
                return False
        return True
    if isinstance(value, list):
        for item in value:
            if not _is_json_value(item):
                return False
        return True
    if isinstance(value, str):
        return is_utf8_text(value)
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or isinstance(value, (bool, int))


def _validate_field(agent_id: str, field: str, value: Any) -> None:
    if field == "backend":
        valid = _is_utf8_text(value) and bool(value) and has_backend(value)
    elif field == "mcp_servers":
        valid = isinstance(value, list) and all(_is_utf8_text(name) for name in value)
    elif field == "subagents":
        valid = isinstance(value, list)
        if valid:
            for definition in value:
                try:
                    SubagentDefinition.model_validate(definition, strict=True)
                    # The runtime also receives extension fields that the model ignores.
                    if not _is_json_value(definition):
                        raise AgentConfigurationError(agent_id, field)
                except (ValidationError, RecursionError):
                    raise AgentConfigurationError(agent_id, field) from None
    elif field == "name":
        valid = _is_utf8_text(value) and bool(value.strip())
    elif field == "reasoning_effort":
        valid = value is None or _is_utf8_text(value) and 1 <= len(value) <= 32
    elif field == "service_tier":
        valid = value is None or isinstance(value, str) and value in ("default", "fast")
    else:
        valid = _is_utf8_text(value) or (field in _NULLABLE_TEXT_FIELDS and value is None)
    if not valid:
        raise AgentConfigurationError(agent_id, field)


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"Invalid JSON constant: {value}")


def decode_agent_configuration(agent_id: str, stored: Mapping[str, Any]) -> dict[str, Any]:
    """Check original stored values without normalizing away malformed data."""
    config = {}
    for field, value in stored.items():
        if field in _JSON_FIELDS:
            try:
                value = json.loads(value, parse_constant=_reject_json_constant) if isinstance(value, str) else None
            except (ValueError, RecursionError):
                raise AgentConfigurationError(agent_id, field) from None
        _validate_field(agent_id, field, value)
        config[field] = value
    return config


def encode_agent_configuration(agent_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """Validate effective Python values before SQLite can coerce them."""
    encoded = dict(fields)
    for field in AGENT_CONFIGURATION_FIELDS:
        if field not in fields:
            continue
        value = fields[field]
        _validate_field(agent_id, field, value)
        if field in _JSON_FIELDS:
            try:
                encoded[field] = json.dumps(value, allow_nan=False)
            except (TypeError, ValueError, RecursionError):
                raise AgentConfigurationError(agent_id, field) from None
    return encoded

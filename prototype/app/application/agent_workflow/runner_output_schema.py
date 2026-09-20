from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel


_HOST_VALIDATED_KEYWORDS = frozenset(
    {
        "default",
        "description",
        "examples",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "multipleOf",
        "pattern",
        "title",
        "uniqueItems",
    }
)


def compile_strict_runner_output_schema(
    model: type[BaseModel],
) -> dict[str, Any]:
    """Compile the App Server structured-output subset for one wire model.

    App Server strict output requires every object property to be required.
    Nullable fields remain explicit `anyOf[..., null]`; empty arrays remain the
    model's representation of optional collections. Rich validation constraints
    stay authoritative in Pydantic/Host validation after the turn, avoiding a
    second, weaker domain contract in the model backend.
    """

    return _compile_node(deepcopy(model.model_json_schema()))


def _compile_node(value: Any) -> Any:
    if isinstance(value, list):
        return [_compile_node(item) for item in value]
    if not isinstance(value, dict):
        return value
    compiled = {}
    for key, child in value.items():
        if key in _HOST_VALIDATED_KEYWORDS:
            continue
        if key in {
            "properties",
            "$defs",
            "definitions",
            "patternProperties",
            "dependentSchemas",
        }:
            # These dictionaries contain user-defined names, not schema
            # keywords. A business field named "title" must never disappear.
            compiled[key] = {
                name: _compile_node(schema) for name, schema in child.items()
            }
        elif key in {
            "items",
            "prefixItems",
            "additionalProperties",
            "allOf",
            "anyOf",
            "oneOf",
            "not",
            "if",
            "then",
            "else",
            "contains",
        }:
            compiled[key] = _compile_node(child)
        else:
            compiled[key] = child
    properties = compiled.get("properties")
    if compiled.get("type") == "object" and isinstance(properties, dict):
        compiled["required"] = list(properties)
        compiled["additionalProperties"] = False
    return compiled


__all__ = ["compile_strict_runner_output_schema"]

"""Tool specs and strict argument checks (Phase 4.1).

A tool is either a READ (runs now, its result goes back to the model) or a WRITE (never
runs from the loop: the loop stops and code draws a confirm card for it). Arguments are
checked against a small JSON-schema subset before anything sees them; a bad call is sent
back to the model as an error so it can correct itself within the round limit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_STRING = 200
SUPPORTED = ("object", "array", "string", "integer", "boolean")


class ArgError(ValueError):
    pass


class AlreadyError(ArgError):
    """Nothing to change: it is already like that (the card says so; a recipe skips the step)."""


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]                       # JSON schema, type object
    write: bool = False
    # READ tools: (args) -> JSON-able result. WRITE tools have none (code runs them after the card).
    run: Callable[[dict[str, Any]], Awaitable[Any]] | None = None
    # WRITE tools: (args) -> one card line (llm.cards.Prepared), built from the current state.
    # Raises ArgError when it cannot be done ("bed_ac is off"): that goes back to the model.
    prepare: Callable[[dict[str, Any]], Awaitable[Any]] | None = None

    def spec(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


def parse_args(tool: Tool, raw: str) -> dict[str, Any]:
    """The model's JSON text -> checked dict. Raises ArgError with a short, model-readable reason."""
    try:
        args = json.loads(raw) if raw.strip() else {}
    except (ValueError, RecursionError):
        raise ArgError("arguments are not valid JSON") from None
    check(tool.parameters, args, "")
    return args


def check(schema: dict[str, Any], value: Any, path: str) -> None:
    where = path or "arguments"
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ArgError(f"{where} must be an object")
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                raise ArgError(f"missing {_join(path, k)}")
        for k, v in value.items():
            if k not in props:
                raise ArgError(f"unknown field {_join(path, k)}")
            check(props[k], v, _join(path, k))
    elif kind == "array":
        if not isinstance(value, list):
            raise ArgError(f"{where} must be a list")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ArgError(f"{where} has more than {schema['maxItems']} items")
        if schema.get("minItems", 0) > len(value):
            raise ArgError(f"{where} needs at least {schema['minItems']} item(s)")
        for i, v in enumerate(value):
            check(schema.get("items", {}), v, f"{where}[{i}]")
    elif kind == "string":
        if not isinstance(value, str):
            raise ArgError(f"{where} must be a string")
        if len(value) > schema.get("maxLength", DEFAULT_MAX_STRING):
            raise ArgError(f"{where} is too long")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ArgError(f"{where} has the wrong format")
    elif kind == "integer":
        # bool is an int in Python; 23.0 is not accepted either (strict)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ArgError(f"{where} must be a whole number")
        if "minimum" in schema and value < schema["minimum"]:
            raise ArgError(f"{where} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ArgError(f"{where} must be at most {schema['maximum']}")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ArgError(f"{where} must be true or false")
    elif kind is not None:
        raise ArgError(f"{where}: unsupported schema type {kind}")
    # Compare types too: True == 1 in Python
    if "enum" in schema and not any(type(value) is type(e) and value == e for e in schema["enum"]):
        raise ArgError(f"{where} must be one of: {', '.join(map(str, schema['enum']))}")


def schema_problems(schema: dict[str, Any], path: str = "") -> list[str]:
    """Coding mistakes in a tool schema (checked by tests), so a typo never reaches the model as an ArgError."""
    where = path or "parameters"
    kind = schema.get("type")
    if kind not in SUPPORTED:
        return [f"{where}: unsupported type {kind!r}"]
    out: list[str] = []
    if kind == "object":
        props = schema.get("properties", {})
        out += [f"{where}: required {k!r} is not a property" for k in schema.get("required", []) if k not in props]
        for k, v in props.items():
            out += schema_problems(v, _join(path, k))
    elif kind == "array":
        out += schema_problems(schema.get("items", {}), f"{where}[]")
    return out


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key

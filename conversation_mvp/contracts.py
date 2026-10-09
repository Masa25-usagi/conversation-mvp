"""Strict contracts. No permissive repair of invalid model JSON."""

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRIGGERS = ("IGNORE", "SUGGEST_PRIVATE", "CALL_VOICE_AI", "TRIGGER_PROTOTYPE", "REQUIRE_PERMISSION")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO 8601 string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("non-finite JSON number")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)


def schema(name):
    return strict_json((ROOT / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8"))


def validate(value, spec, where="$"):
    """Validate the exact JSON Schema subset used by the supplied contracts."""
    kind = spec.get("type")
    valid = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "number": lambda v: type(v) in (int, float) and math.isfinite(v),
        "boolean": lambda v: type(v) is bool,
    }
    if kind and not valid[kind](value):
        raise ValueError(f"{where}: expected {kind}")
    if "enum" in spec and value not in spec["enum"]:
        raise ValueError(f"{where}: value is outside enum")
    if kind == "object":
        properties = spec.get("properties", {})
        if set(spec.get("required", [])) - value.keys():
            raise ValueError(f"{where}: missing required keys")
        if spec.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ValueError(f"{where}: extra keys")
        for key in value.keys() & properties.keys():
            validate(value[key], properties[key], f"{where}.{key}")
    elif kind == "array":
        if len(value) < spec.get("minItems", 0) or len(value) > spec.get("maxItems", float("inf")):
            raise ValueError(f"{where}: invalid array length")
        for item in value:
            validate(item, spec["items"], where + "[]")
    elif kind == "string":
        if len(value) < spec.get("minLength", 0) or len(value) > spec.get("maxLength", float("inf")):
            raise ValueError(f"{where}: invalid string length")
    elif kind == "number":
        if not spec.get("minimum", -math.inf) <= value <= spec.get("maximum", math.inf):
            raise ValueError(f"{where}: number outside range")
    return value


def validate_output(value, role):
    validate(value, schema(role))
    if role in ("trigger", "decision"):
        sentences = [part.strip() for part in re.split(r"[。！？!?\n]+|(?<!\d)\.(?!\d)", value["reason"]) if part.strip()]
        if len(sentences) != 1:
            raise ValueError("reason must contain exactly one sentence")
    return value


def utterance(value):
    if not isinstance(value, dict) or set(value) - {"id", "text", "start_at", "end_at", "source", "provenance", "speaker"}:
        raise ValueError("invalid utterance keys")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value.get("id", "")):
        raise ValueError("invalid utterance id")
    if not isinstance(value.get("text"), str) or not 1 <= len(value["text"].strip()) <= 8000:
        raise ValueError("text must have 1..8000 characters")
    if parse_time(value["end_at"]) < parse_time(value["start_at"]):
        raise ValueError("utterance ends before it starts")
    if value.get("provenance") not in ("real", "synthetic"):
        raise ValueError("provenance must be real or synthetic")
    if not isinstance(value.get("source"), str) or len(value["source"]) > 100:
        raise ValueError("invalid input source")
    if "speaker" in value and (not isinstance(value["speaker"], str) or len(value["speaker"]) > 100):
        raise ValueError("invalid speaker")
    return dict(value)

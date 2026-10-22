"""The JSON Schema a tenant tool declares for its parameters: a small, strict subset.

A tool's schema is shown to the model and checked again before anything is sent. Accepting a
keyword the checker then ignores ("format": "email", "$ref", nested objects) would publish a rule
nobody enforces, so the subset is closed: an object of scalar parameters with

    type: string | number | integer | boolean
    description, enum, default, pattern, minLength, maxLength, minimum, maximum

and nothing else. Arguments the schema does not declare are refused, not dropped: the model learns
that it cannot pass, say, a customer id (identity always comes from the signed-in user).
"""

from __future__ import annotations

import math
import re
from typing import Any

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
TYPES = {"string", "number", "integer", "boolean"}
PARAM_KEYS = {"type", "description", "enum", "default", "pattern", "minLength", "maxLength", "minimum", "maximum"}
TOP_KEYS = {"type", "properties", "required", "description"}
MAX_PARAMS = 16
MAX_PATTERN = 200
# Any string argument, whatever its schema says: patterns are tenant-written regular expressions,
# and a short input bounds what a badly written one can cost.
MAX_STRING = 500
# Patterns that can backtrack exponentially: a quantified group that itself repeats or alternates
# ("(a+)+", "(\\w+\\s?)*", "(a|aa)+"), and backreferences. Refused when the schema is saved; the rest is
# linear enough for 500 characters.
_NESTED = re.compile(r"\((?:[^()\\]|\\.)*(?:[+*]|\{\d*,\d*\}|\|)(?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,\d*\})")
_BACKREF = re.compile(r"\\[1-9]|\(\?P=")
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")


class SchemaError(ValueError):
    """A schema outside the subset, or arguments that break it. The message names the parameter."""


def validate_schema(schema: Any) -> dict[str, Any]:
    """Raise SchemaError unless ``schema`` is inside the subset; return it unchanged."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise SchemaError("参数定义的 type 必须是 object")
    unknown = sorted(set(schema) - TOP_KEYS)
    if unknown:
        raise SchemaError(f"参数定义不支持这些关键字：{', '.join(unknown)}")
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict) or len(properties) > MAX_PARAMS:
        raise SchemaError(f"properties 必须是对象，最多 {MAX_PARAMS} 个参数")
    for name, spec in properties.items():
        if not PARAM_RE.match(str(name)):
            raise SchemaError(f"参数名 {name!r} 只能用小写字母、数字和下划线")
        _check_param(name, spec)
    required = schema.get("required") or []
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise SchemaError("required 必须是参数名列表")
    missing = [r for r in required if r not in properties]
    if missing:
        raise SchemaError(f"required 里的参数没有定义：{', '.join(missing)}")
    return schema


def _check_param(name: str, spec: Any) -> None:
    if not isinstance(spec, dict):
        raise SchemaError(f"参数 {name} 的定义必须是对象")
    unknown = sorted(set(spec) - PARAM_KEYS)
    if unknown:
        raise SchemaError(f"参数 {name} 不支持这些关键字：{', '.join(unknown)}")
    kind = spec.get("type")
    if kind not in TYPES:
        raise SchemaError(f"参数 {name} 的 type 只能是 {', '.join(sorted(TYPES))}")
    if "enum" in spec:
        values = spec["enum"]
        if not isinstance(values, list) or not values or len(values) > 50:
            raise SchemaError(f"参数 {name} 的 enum 必须是 1–50 个取值")
        for value in values:
            _coerce(name, spec, value, checking_schema=True)
    if "pattern" in spec:
        pattern = spec["pattern"]
        if kind != "string" or not isinstance(pattern, str) or len(pattern) > MAX_PATTERN:
            raise SchemaError(f"参数 {name} 的 pattern 只能用于 string，最长 {MAX_PATTERN} 个字符")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SchemaError(f"参数 {name} 的 pattern 不是合法的正则表达式：{exc}") from None
        if _NESTED.search(pattern) or _BACKREF.search(pattern):
            raise SchemaError(f"参数 {name} 的 pattern 有嵌套重复或反向引用，可能回溯很久，请改写")
    for key in ("minLength", "maxLength"):
        if key in spec and (kind != "string" or not _whole(spec[key]) or spec[key] < 0):
            raise SchemaError(f"参数 {name} 的 {key} 只能用于 string，且是非负整数")
    for key in ("minimum", "maximum"):
        if key in spec and (kind not in ("number", "integer") or not _finite(spec[key])):
            raise SchemaError(f"参数 {name} 的 {key} 只能用于数字，且是有限数值")
    if "default" in spec:
        _coerce(name, spec, spec["default"], checking_schema=True)
    if "description" in spec and not isinstance(spec["description"], str):
        raise SchemaError(f"参数 {name} 的 description 必须是文字")


def validate_arguments(schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """The arguments as they will be sent: checked, coerced ("120.5" -> 120.5) and with defaults
    filled in. Raises SchemaError naming the first parameter that is wrong."""
    if not isinstance(args, dict):
        raise SchemaError("参数必须是 JSON 对象")
    properties = schema.get("properties") or {}
    extra = sorted(set(args) - set(properties))
    if extra:
        raise SchemaError(f"不接受这些参数：{', '.join(extra)}")
    result: dict[str, Any] = {}
    for name, spec in properties.items():
        value = args.get(name)
        if value is None or value == "":
            if "default" in spec:
                result[name] = spec["default"]
            elif name in (schema.get("required") or []):
                raise SchemaError(f"缺少参数 {name}（{spec.get('description') or name}）")
            continue
        result[name] = _coerce(name, spec, value)
    return result


def _coerce(name: str, spec: dict[str, Any], value: Any, checking_schema: bool = False) -> Any:
    kind = spec["type"]
    bad = SchemaError(f"参数 {name} 的取值不符合定义" if not checking_schema else f"参数 {name} 的 enum/default 与 type 不符")
    if kind == "boolean":
        if not isinstance(value, bool):
            raise bad
        coerced: Any = value
    elif kind in ("number", "integer"):
        if isinstance(value, bool):
            raise bad
        if isinstance(value, str) and not checking_schema:
            text = value.strip().replace(",", "")
            if not _NUMBER.match(text):  # "1_000", "0x10", "1e400" are not how amounts are written
                raise bad
            value = float(text)
        if not isinstance(value, (int, float)):
            raise bad
        try:
            if not math.isfinite(float(value)):
                raise bad
        except OverflowError:  # an int beyond float range
            raise bad from None
        if kind == "integer":
            if float(value) != int(value):
                raise bad
            coerced = int(value)
        else:
            coerced = float(value)
        if "minimum" in spec and coerced < spec["minimum"]:
            raise SchemaError(f"参数 {name} 不能小于 {spec['minimum']}")
        if "maximum" in spec and coerced > spec["maximum"]:
            raise SchemaError(f"参数 {name} 不能大于 {spec['maximum']}")
    else:
        if not isinstance(value, str):
            if checking_schema or isinstance(value, (dict, list, bool)):
                raise bad
            value = str(value)
        coerced = value.strip()
        if len(coerced) > MAX_STRING:
            raise SchemaError(f"参数 {name} 太长（最多 {MAX_STRING} 个字符）")
        if "minLength" in spec and len(coerced) < spec["minLength"]:
            raise SchemaError(f"参数 {name} 太短")
        if "maxLength" in spec and len(coerced) > spec["maxLength"]:
            raise SchemaError(f"参数 {name} 太长（最多 {spec['maxLength']} 个字符）")
        if "pattern" in spec and not re.search(spec["pattern"], coerced):
            raise SchemaError(f"参数 {name} 的格式不对（{spec.get('description') or spec['pattern']}）")
    if "enum" in spec and not checking_schema and coerced not in spec["enum"]:
        raise SchemaError(f"参数 {name} 只能是 {'、'.join(map(str, spec['enum']))}")
    return coerced


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

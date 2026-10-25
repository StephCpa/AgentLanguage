"""Canonical immutable descriptions and strict JSON values.

This deliberately supports a small interoperable JSON domain. Floats, booleans
as quantities, duplicate object keys and oversized integers are rejected.
"""
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from .errors import InvalidData
from .flow import Label, PUBLIC


MAX_INTEGER = 2**63 - 1


def quantity(value):
    if type(value) is not int or not 0 <= value <= MAX_INTEGER:
        raise InvalidData("resource quantities must be nonnegative signed-64-bit integers")
    return value


def identifier(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise InvalidData("expected a nonempty identifier of at most 512 characters")
    return value


def validate_json(value, depth=0):
    if depth > 64:
        raise InvalidData("JSON nesting exceeds 64 levels")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if not -MAX_INTEGER <= value <= MAX_INTEGER:
            raise InvalidData("integer exceeds the supported range")
        return
    if type(value) is str:
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise InvalidData("invalid Unicode scalar value") from exc
        return
    if type(value) is list:
        for item in value:
            validate_json(item, depth + 1)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for key, item in value.items():
            validate_json(key, depth + 1)
            validate_json(item, depth + 1)
        return
    raise InvalidData("only canonical JSON data is accepted; floats and host objects are forbidden")


def canonical(value: Any) -> str:
    validate_json(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def load_json(text: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidData("duplicate JSON object key")
            result[key] = value
        return result
    try:
        result = json.loads(text, object_pairs_hook=pairs)
    except (ValueError, RecursionError) as exc:
        raise InvalidData("invalid JSON document") from exc
    validate_json(result)
    return result


def resource_vector(value):
    if type(value) is not dict:
        raise InvalidData("resource vector must be an object")
    for key, amount in value.items():
        identifier(key)
        quantity(amount)
    return dict(value)


@dataclass(frozen=True)
class Principal:
    tenant: str
    subject: str

    def __post_init__(self):
        identifier(self.tenant)
        identifier(self.subject)


@dataclass(frozen=True)
class Slot:
    node: str
    activation: str = "0"

    def __post_init__(self):
        identifier(self.node)
        identifier(self.activation)


@dataclass(frozen=True, init=False)
class Intent:
    encoded: str

    def __init__(self, tool, version, scope, args, receipts=(), *, label=PUBLIC):
        value = {"tool": identifier(tool), "version": identifier(version),
                 "scope": identifier(scope), "args": args,
                 "receipts": [identifier(x) for x in receipts]}
        if not isinstance(label,Label):
            raise InvalidData('intent flow policy must be a Label')
        if label!=PUBLIC:
            value['flow']=label.to_data()
        object.__setattr__(self, "encoded", canonical(value))

    @property
    def value(self):
        return json.loads(self.encoded)

    @property
    def digest(self):
        return hashlib.sha256(self.encoded.encode("utf-8")).hexdigest()

    @property
    def label(self):
        return Label.from_data(self.value.get('flow'))

"""Typed access to parsed JSON; files on disk are not trusted to have a shape."""

import json
from datetime import datetime
from typing import cast


class FormatError(ValueError):
    """A run record or event line does not have the expected shape."""


def load_object(text: str) -> dict[str, object]:
    """Parse ``text`` as a JSON object."""
    value: object = json.loads(text)
    if not isinstance(value, dict):
        raise FormatError("expected a JSON object")
    return {str(k): v for k, v in cast(dict[object, object], value).items()}


def dump_object(value: dict[str, object], *, pretty: bool) -> str:
    """Deterministic JSON: sorted keys, so diffs and hashes are stable."""
    if pretty:
        return json.dumps(value, sort_keys=True, indent=2) + "\n"
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def get_str(obj: dict[str, object], key: str) -> str:
    """The string at ``key``."""
    value = obj.get(key)
    if not isinstance(value, str):
        raise FormatError(f"{key}: expected a string")
    return value


def get_opt_str(obj: dict[str, object], key: str) -> str | None:
    """The string at ``key``, or None when absent or null."""
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise FormatError(f"{key}: expected a string or null")
    return value


def get_int(obj: dict[str, object], key: str) -> int:
    """The integer at ``key`` (booleans rejected)."""
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise FormatError(f"{key}: expected an integer")
    return value


def get_opt_int(obj: dict[str, object], key: str) -> int | None:
    """The integer at ``key``, or None when absent or null."""
    if obj.get(key) is None:
        return None
    return get_int(obj, key)


def get_object(obj: dict[str, object], key: str) -> dict[str, object]:
    """The nested object at ``key``."""
    value = obj.get(key)
    if not isinstance(value, dict):
        raise FormatError(f"{key}: expected an object")
    return {str(k): v for k, v in cast(dict[object, object], value).items()}


def get_str_list(obj: dict[str, object], key: str) -> list[str]:
    """The list of strings at ``key``."""
    value = obj.get(key)
    if not isinstance(value, list):
        raise FormatError(f"{key}: expected a list")
    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        raise FormatError(f"{key}: expected a list of strings")
    return [str(item) for item in items]


def get_datetime(obj: dict[str, object], key: str) -> datetime:
    """An ISO 8601 timestamp at ``key``."""
    return datetime.fromisoformat(get_str(obj, key))

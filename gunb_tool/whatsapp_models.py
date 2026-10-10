"""Small, bounded Meta wire primitives; no configuration, network or database."""

from __future__ import annotations

import json
import re


def numeric_id(value: str, *, maximum: int = 32, minimum: int = 1) -> str:
    if (not isinstance(value, str) or not minimum <= len(value) <= maximum
            or not re.fullmatch(r'[1-9][0-9]*', value)):
        raise ValueError('Niepoprawny identyfikator Meta')
    return value


def message_id(value: str) -> str:
    if not isinstance(value, str) or not 7 <= len(value) <= 256 or not re.fullmatch(r'wamid\.[A-Za-z0-9._=:-]+', value):
        raise ValueError('Niepoprawny identyfikator wiadomości Meta')
    return value


def strict_json(raw: bytes, limit: int):
    """Reject ambiguous JSON and cap bytes/depth before consumers inspect fields."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Powtórzony klucz')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('Niedozwolona stała')

    try:
        if type(raw) is not bytes or not 1 <= len(raw) <= limit:
            raise ValueError('Limit JSON')
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
        pending = [(result, 1)]
        while pending:
            node, depth = pending.pop()
            if depth > 20:
                raise ValueError('Głębokość JSON')
            if isinstance(node, dict):
                pending.extend((child, depth + 1) for child in node.values())
            elif isinstance(node, list):
                pending.extend((child, depth + 1) for child in node)
        return result
    except (ValueError, TypeError, RecursionError):
        raise ValueError('Niepoprawny JSON Meta') from None

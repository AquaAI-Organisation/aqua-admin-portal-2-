"""Helpers for storing structured data safely in JSON fields."""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID


def sanitize_json(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): sanitize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_json(item) for item in value]
    return value

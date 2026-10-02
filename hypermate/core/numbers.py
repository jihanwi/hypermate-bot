"""Decimal parsing. API numbers are never converted to float."""

from decimal import Decimal, InvalidOperation
from typing import Optional


def to_decimal(value) -> Optional[Decimal]:
    """Parse an API number (usually a string) to Decimal. None if missing or not a number."""
    if value is None or value == 'N/A':
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

"""Expiry checks shared by account policy and SQLite write predicates."""

from datetime import UTC, datetime


def is_expired(iso: str | None, *, now: datetime | None = None) -> bool:
    if not iso:
        return False
    try:
        return datetime.fromisoformat(iso) <= (now or datetime.now(UTC))
    except ValueError:
        # An unparseable deadline cannot authorize an operation.
        return True

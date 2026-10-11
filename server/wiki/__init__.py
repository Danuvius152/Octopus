"""Explicit, account-scoped access to an existing local knowledge vault."""

from .registry import registry
from .store import WikiError

__all__ = ["WikiError", "registry"]

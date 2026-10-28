"""Explicit complete-record SELECTs and their matching field names."""

from collections.abc import Sequence
from typing import Any


class RecordProjection:
    """A fixed allowlist, never the arbitrary columns of an incoming SQL row.

    Pairs name an output field and its SQL expression (including JOIN aliases
    or an aggregate). Plain names may be qualified by a query's table alias.
    Domain readers still own decoding and defaults.
    """

    def __init__(self, *columns: str | tuple[str, str]) -> None:
        self.fields = tuple((column, column) if isinstance(column, str) else column for column in columns)
        if len({name for name, _ in self.fields}) != len(self.fields):
            raise ValueError("Duplicate record projection field")

    def sql(self, alias: str | None = None) -> str:
        return ", ".join(
            f"{alias}.{expression}" if alias and name == expression else expression
            for name, expression in self.fields
        )

    def __str__(self) -> str:
        return self.sql()

    def read(self, row: Sequence[Any]) -> dict[str, Any]:
        if len(row) != len(self.fields):
            raise ValueError(f"Expected {len(self.fields)} record columns, got {len(row)}")
        return dict(zip((name for name, _ in self.fields), row, strict=True))

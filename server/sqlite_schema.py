"""Pure handling of trusted SQLite declarations, without storage or versions."""

import re
import sqlite3
from collections.abc import Iterator


def statements(script: str) -> Iterator[str]:
    """Split trusted DDL at complete statements, including trigger bodies."""
    start = 0
    for end, char in enumerate(script, 1):
        if char == ";" and sqlite3.complete_statement(script[start:end]):
            yield script[start:end]
            start = end
    if script[start:].strip():
        raise ValueError("Incomplete SQLite DDL")


_TOKEN = re.compile(
    r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]"
    r"|--[^\n]*|/\*.*?\*/|\w+|[^\s]", re.DOTALL,
)


def definition(kind: str, sql: str) -> tuple:
    """Compare declarations and constraints, preserving every quoted token.

    Table declaration order is ignored here. An owner whose positional writers
    depend on column order must also compare native table_xinfo metadata.
    """
    tokens = []
    for token in _TOKEN.findall(sql):
        if token.startswith(("--", "/*")):
            continue
        tokens.append(token if token.startswith(("'", '"', '`', '[')) else token.lower())
    if tokens[-1:] == [";"]:
        tokens.pop()
    for offset in (2, 3):
        if tokens[offset:offset + 3] == ["if", "not", "exists"]:
            del tokens[offset:offset + 3]
            break
    if kind != "table":
        return tuple(tokens)
    opening = tokens.index("(")
    declarations = []
    start, depth = opening + 1, 1
    for offset in range(start, len(tokens)):
        token = tokens[offset]
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        if depth == 0 or (token == "," and depth == 1):
            declarations.append(tuple(tokens[start:offset]))
            start = offset + 1
        if depth == 0:
            return (tuple(tokens[:opening]), tuple(sorted(declarations)), tuple(tokens[offset + 1:]))
    raise ValueError("Incomplete SQLite table declaration")

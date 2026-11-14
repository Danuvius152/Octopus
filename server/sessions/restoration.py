"""Lightweight browser restore identities, owned by SessionManager lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal


class RestoreRefused(ValueError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class RestoreOperation:
    actor: str | None
    session_id: str
    generation: str
    state: Literal["running", "finished", "sealed"]
    task: asyncio.Task[tuple[int, str] | None] | None = None
    status_code: int | None = None
    message: str | None = None

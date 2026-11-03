"""Non-secret comparison evidence for a resolved model authorization.

The resolver creates this from the exact row it decrypts. A process retains it
for its lifetime; reading the current row after a rejection cannot recreate it.
Directory credentials deliberately have no evidence: a mutable auth.json path
does not identify what an independent CLI actually loaded or refreshed.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any


@dataclass(frozen=True)
class CredentialUse:
    credential_id: str
    user_id: str | None
    backend: str
    auth_type: str
    material: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> CredentialUse:
        return cls(row["id"], row["user_id"], row["backend"], row["auth_type"],
                   sha256(row["secret_encrypted"].encode()).hexdigest())

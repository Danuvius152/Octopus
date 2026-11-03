"""Opaque evidence of a single installation's resolved authorization."""

import hashlib
import json
from typing import Any, Literal

GrantDisposition = Literal["applied", "obsolete", "gone"]


def grant_stamp(installation: dict[str, Any], encrypted: str, api_base: str | None) -> str:
    material = [installation[key] for key in ("id", "user_id", "kind", "auth_type")]
    material += [encrypted, api_base.rstrip("/") if api_base else None]
    return hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()

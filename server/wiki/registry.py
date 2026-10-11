"""Account-owned vault registrations; no discovery or content on ordinary turns."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ..config import settings
from ..workspace import user_root
from .store import WikiError, WikiStore


class WikiRegistry:
    def state(self, user_id: str | None) -> Path:
        return (
            user_root(user_id) / "wiki"
            if user_id
            else Path(settings.wiki_dir).expanduser()
        )

    def config(self, user_id: str | None) -> dict[str, Any]:
        path = self.state(user_id) / "config.json"
        if not path.is_file():
            raise WikiError("No local wiki is registered for this account", 404)
        return json.loads(path.read_text(encoding="utf-8"))

    def configure(
        self,
        user_id: str | None,
        root: Path,
        curator_agent_id: str,
        app_id: str | None = None,
    ) -> dict[str, Any]:
        if not root.is_dir():
            raise WikiError("Vault root must be an existing directory")
        state = self.state(user_id)
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        old = self.config(user_id) if (state / "config.json").exists() else None
        if old and Path(old["root"]).resolve() != root.resolve():
            raise WikiError(
                "Changing the authoritative vault requires a separate migration", 409
            )
        config = {
            "root": str(root.resolve()),
            "curator_agent_id": curator_agent_id,
            "app_id": app_id,
            "vault_id": old["vault_id"] if old else str(uuid.uuid4()),
        }
        with tempfile.NamedTemporaryFile(
            dir=state, mode="w", encoding="utf-8", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(config, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state / "config.json")
        return config

    def store(self, user_id: str | None) -> WikiStore:
        config = self.config(user_id)
        return WikiStore(Path(config["root"]), self.state(user_id), config["vault_id"])


registry = WikiRegistry()

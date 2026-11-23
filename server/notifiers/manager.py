"""Select committed notifier targets and dispatch each event in parallel.

Adding a new notifier type: add a class to `_make`'s dispatch.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .base import NotifierBase, NotifierEvent
from .webhook import WebhookNotifier

if TYPE_CHECKING:
    from ..database import Database

logger = logging.getLogger(__name__)


class NotifierManager:
    def __init__(self) -> None:
        self._db: Database | None = None

    def set_db(self, db: Database) -> None:
        self._db = db

    def _make(self, row: dict[str, Any]) -> NotifierBase | None:
        """Build a concrete NotifierBase from a DB row, by type."""
        t = row["type"]
        if t == "webhook":
            return WebhookNotifier(
                id=row["id"], label=row["label"], config=row["config"]
            )
        logger.warning("Unknown notifier type %s (id=%s); skipping", t, row["id"])
        return None

    async def fire(self, event: NotifierEvent, user_id: str | None = None) -> None:
        """Dispatch `event` to the account's registered notifiers, in parallel.

        `user_id=None` is the pre-accounts install, where every target is the
        one operator's. With accounts it is required in practice: an event
        carries somebody's session name and the fact that they are working, and
        a webhook is an address off this box.

        Notifier exceptions are caught + logged so a single bad target
        doesn't poison the rest.
        """
        db = self._db
        if db is None:
            return
        try:
            rows = await db.load_notifiers(user_id, enabled_only=True)
        except Exception as error:
            db.health.require_healthy()
            logger.warning(
                "Notifier target selection failed for event=%s (%s)",
                event.type, type(error).__name__,
            )
            return
        targets = [target for row in rows if (target := self._make(row)) is not None]
        if not targets:
            return
        logger.debug(
            "Firing %s to %d notifier(s)", event.type, len(targets)
        )
        await asyncio.gather(
            *(self._safe_send(n, event) for n in targets),
            return_exceptions=False,
        )

    @staticmethod
    async def _safe_send(notifier: NotifierBase, event: NotifierEvent) -> None:
        try:
            await notifier.send(event)
        except Exception:
            logger.error(
                "Notifier %s (%s) event=%s error=send_error",
                notifier.id, notifier.type, event.type,
            )


# App-lifetime singleton — wired into the FastAPI lifespan in main.py.
notifier_manager = NotifierManager()

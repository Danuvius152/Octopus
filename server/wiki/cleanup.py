"""Internal scheduler sweep; no model turns or knowledge-content maintenance."""

import asyncio
import logging

from ..aio import complete_cleanup
from .history import WikiHistory
from .registry import registry

logger = logging.getLogger(__name__)


async def sweep():
    states = registry.states()
    results = []
    for user_id, state in states:
        if not (state / "config.json").is_file():
            continue
        try:

            def clean(user_id=user_id):
                return WikiHistory(registry.store(user_id)).cleanup()

            result = await complete_cleanup(asyncio.to_thread(clean))
            results.append(result)
            logger.info("Wiki retention account=%s result=%s", user_id, result)
        except Exception:
            logger.exception("Wiki retention failed for account=%s", user_id)
    return results

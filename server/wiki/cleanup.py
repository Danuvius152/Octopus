"""Internal scheduler sweep; no model turns or knowledge-content maintenance."""

import asyncio
import logging
from pathlib import Path

from ..aio import complete_cleanup
from ..config import settings
from .history import WikiHistory
from .registry import registry

logger = logging.getLogger(__name__)


async def sweep():
    states = [(None, Path(settings.wiki_dir).expanduser())]
    root = Path(settings.users_root).expanduser()
    states.extend((path.parent.name, path) for path in root.glob("*/wiki"))
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

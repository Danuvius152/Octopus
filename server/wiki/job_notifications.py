"""At-least-once cross-database delivery; the receiver commits before ACK."""

import asyncio
import logging
from functools import partial

from ..aio import complete_cleanup
from . import job_events, job_returns
from .registry import registry
from .store import WikiError

logger = logging.getLogger(__name__)


async def _io(function, *args):
    return await complete_cleanup(asyncio.to_thread(partial(function, *args)))


def _pending(jobs, cursor):
    with jobs.store.connect() as db:
        job_events.require(db)
        after, through = cursor if cursor else (None, db.execute("SELECT coalesce(max(rowid),0) FROM wiki_job_outbox").fetchone()[0])
        deliveries = job_events.pending(db, after=after, through=through)
        if not deliveries and after is not None:
            through = db.execute("SELECT coalesce(max(rowid),0) FROM wiki_job_outbox").fetchone()[0]
            deliveries = job_events.pending(db, through=through)
        return deliveries, through


def _acknowledge(jobs, delivery, outcome):
    with registry._account_lock(jobs.user_id), registry.registration_lock(jobs.user_id), jobs.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if registry.config(jobs.user_id) != jobs.registration:
            raise WikiError("Wiki registration changed before notification acknowledgement", 409)
        job_events.acknowledge(db, delivery["event_id"], delivery["subscriber_id"], outcome)


async def dispatch(jobs, manager):
    """One fair bounded batch. Failed deliveries keep their original identity.

    A fixed high watermark bounds each pass even while new events arrive.
    The cursor only schedules attempts; durable ACKs remain the sole truth.
    """
    if not hasattr(manager, "_wiki_notice_cursors"):
        manager._wiki_notice_cursors = {}
    key = (jobs.user_id, jobs.registration["vault_id"], str(jobs.store.state.resolve()))
    deliveries, through = await _io(_pending, jobs, manager._wiki_notice_cursors.get(key))
    failure = None
    for delivery in deliveries:
        try:
            outcome = await complete_cleanup(manager.db.receive_wiki_job_notice(
                jobs.registration["vault_id"], jobs.user_id, delivery,
            ))
            if outcome == "delivered" and delivery["kind"] in {"chat", "schedule"}:
                # An invalidation only; reconnect reads the committed card.
                await manager._broadcast({
                    "type": "wiki_job_changed", "session_id": delivery["target"],
                    "vault_id": jobs.registration["vault_id"], "job_id": delivery["job_id"],
                    "state_version": delivery["state_version"],
                }, user_id=jobs.user_id)
                outcome = await job_returns.deliver(jobs, manager, delivery, _io)
            if outcome is not None:
                await _io(_acknowledge, jobs, delivery, outcome)
        except Exception as exc:
            failure = failure or exc
        manager._wiki_notice_cursors[key] = ((delivery["outbox_order"], delivery["subscriber_id"]), through)
    if failure is not None:
        raise failure
    return len(deliveries)


async def run(manager):
    """A worker-owned receiver survives jobs finishing and resumes after restart."""
    from .jobs import WikiJobs

    while True:
        for user_id, state in registry.states():
            if not (state / "config.json").is_file():
                continue
            try:
                jobs = await _io(WikiJobs, user_id)
                await dispatch(jobs, manager)
            except Exception as error:
                logger.exception("Wiki notification delivery remains pending for account=%s", user_id)
                # The joined I/O can fail after cleanup deferred cancellation.
                # Its domain error must not restart a cancelled receiver.
                if (receiver := asyncio.current_task()) is not None and receiver.cancelling():
                    raise asyncio.CancelledError from error
        await asyncio.sleep(1)

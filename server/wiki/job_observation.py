"""Read current job facts through an original chat's durable observation link."""

from typing import Any

from ..mcp_identity import McpScope
from . import job_events
from .contracts import JobSnapshot
from .registry import registry
from .store import WikiError
from .tasks import tasks


async def current_cards(user_id: str | None, cards: list[dict[str, Any]], scope: McpScope | None) -> list[dict[str, Any]]:
    if not cards:
        return cards
    try:
        return await registry.run_sync(_current_cards, user_id, cards, scope)
    except WikiError:
        # Missing/unreadable jobs and registrations share the same projection;
        # retaining a permitted historical summary reveals no new existence.
        return cards


def _current_cards(user_id: str | None, cards: list[dict[str, Any]], scope: McpScope | None) -> list[dict[str, Any]]:
    store = registry.store(user_id)
    with store.operation(), registry.registered(user_id, store) as registration, store.connect() as db:
        tasks.caller(user_id, scope, registration)
        db.execute("BEGIN")
        job_events.require(db)
        result = []
        for card in cards:
            if card["vault_id"] != registration["vault_id"]:
                result.append(card)
                continue
            # Every authoritative change emits this immutable, bounded snapshot
            # in its original transaction. ACK/dispatch eligibility is irrelevant
            # to reading it; no subscriber or model input is created here.
            row = db.execute(
                "SELECT o.body FROM jobs j JOIN wiki_job_state s ON s.job_id=j.id "
                "JOIN wiki_job_outbox o ON o.job_id=s.job_id AND o.state_version=s.state_version WHERE j.id=?",
                (card["job_id"],),
            ).fetchone()
            result.append(card if row is None else card | JobSnapshot.model_validate_json(row[0]).model_dump()
                          | {"current_state": "available"})
        return result

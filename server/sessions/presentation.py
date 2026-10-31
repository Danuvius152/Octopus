"""Pure session summaries shared by live and archived response readers."""

from __future__ import annotations

from typing import Any

from ..harness import get_harness
from ..models import SessionInfo, SessionStatus
from .base import Session, fork_info_fields


def session_info(
    source: Session | dict[str, Any], message_count: int | None = None,
    archived: bool = False, *, transcript_integrity: str | None = None,
) -> SessionInfo:
    """Assemble fields only. Detail counts and integrity take precedence over
    cached live summaries. This never queries storage or mutates a session.
    """
    get = source.get if isinstance(source, dict) else lambda name, default=None: getattr(source, name, default)
    backend = get("backend") or "claude-code"
    try:
        can_steer = get_harness(backend).profile.can_steer
    except ValueError:
        can_steer = False
    return SessionInfo(
        id=get("id"), name=get("name"), working_dir=get("working_dir"),
        status=SessionStatus.idle if archived else get("status"), created_at=get("created_at"),
        message_count=message_count if message_count is not None else get("_message_count", 0),
        transcript_integrity=transcript_integrity if transcript_integrity is not None else get("_transcript_integrity", "ok"),
        claude_session_id=get("claude_session_id"), credential_id=get("credential_id"),
        agent_id=get("agent_id"), origin=get("origin") or "user", backend=backend,
        model=get("model"), reasoning_effort=get("reasoning_effort"), service_tier=get("service_tier"),
        parent_session_id=get("parent_session_id"), delegation_request=get("delegation_request"),
        app_id=get("app_id"), archived=archived, can_steer=can_steer,
        **fork_info_fields(
            backend=backend, forked_from_session_id=get("forked_from_session_id"),
            fork_after_seq=get("fork_after_seq"), fork_metadata=get("fork_metadata"),
            fork_revert_record=get("fork_revert_record"),
        ),
    )

"""Authorized content operations retain one Store through the real file effect."""

from contextlib import contextmanager
from pathlib import Path

from .curation import WikiCuration
from .history import WikiHistory
from .registry import registry
from .store import WikiError


@contextmanager
def _authorized(user_id, caller, *, human_only=False):
    with registry.using(user_id) as (store, registration), WikiHistory(store).locked():
        with registry.registered(user_id, store, registration, caller=caller):
            task = caller.task(user_id, registration, publish=not human_only, human_only=human_only)
            yield WikiCuration(store), task, task.agent_id if task else "human"


def edit(user_id, *, path, content, expected_revision, reason, caller):
    with _authorized(user_id, caller, human_only=True) as (curation, _, actor):
        if Path(path).suffix.lower() not in {".md", ".markdown", ".txt", ".html", ".htm"}:
            raise WikiError("This endpoint edits text documents only")
        return curation.history.write(path, content.encode(), expected_revision, actor, reason)


def restore(user_id, *, change_id, expected_revision, side, caller):
    with _authorized(user_id, caller, human_only=True) as (curation, _, actor):
        return curation.history.restore(change_id, expected_revision, side, actor)


def restore_snapshot(user_id, *, snapshot_id, paths, expected_revisions, caller):
    with _authorized(user_id, caller, human_only=True) as (curation, _, _):
        return curation.history.restore_snapshot(snapshot_id, paths, expected_revisions)


def publish(user_id, *, caller, **content):
    with _authorized(user_id, caller) as (curation, task, actor):
        return curation.publish(**content, actor=actor, task=task)


def organize(user_id, *, caller, **content):
    with _authorized(user_id, caller) as (curation, task, actor):
        publication = {"job_id": task.job_id, "session_id": task.session_id} if task and task.job_id else None
        return curation.organize(**content, actor=actor, task=task, publication=publication)


def restructure(user_id, *, caller, **content):
    with _authorized(user_id, caller) as (curation, task, actor):
        return curation.restructure(**content, actor=actor, task=task)


def revise(user_id, body, *, caller):
    from .revise import revise as apply_revision

    with _authorized(user_id, caller) as (curation, task, actor):
        return apply_revision(curation, body, actor, task)

"""Authorized content operations retain one Store through the real file effect."""

from contextlib import contextmanager

from .curation import WikiCuration
from .history import WikiHistory
from .registry import registry
from .tasks import tasks


@contextmanager
def _authorized(user_id, scope, app_id):
    with registry.using(user_id) as (store, registration), WikiHistory(store).locked():
        with registry.registered(user_id, store, registration):
            task = tasks.caller(user_id, scope, registration, app_id=app_id, publish=True)
            yield WikiCuration(store), task, task.agent_id if task else "human"


def publish(user_id, *, scope=None, app_id=None, **content):
    with _authorized(user_id, scope, app_id) as (curation, task, actor):
        return curation.publish(**content, actor=actor, task=task)


def organize(user_id, *, scope=None, app_id=None, **content):
    with _authorized(user_id, scope, app_id) as (curation, task, actor):
        publication = {"job_id": task.job_id, "session_id": task.session_id} if task and task.job_id else None
        return curation.organize(**content, actor=actor, task=task, publication=publication)


def restructure(user_id, *, scope=None, app_id=None, **content):
    with _authorized(user_id, scope, app_id) as (curation, task, actor):
        return curation.restructure(**content, actor=actor, task=task)


def revise(user_id, body, *, scope=None, app_id=None):
    from .revise import revise as apply_revision

    with _authorized(user_id, scope, app_id) as (curation, task, actor):
        return apply_revision(curation, body, actor, task)

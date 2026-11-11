"""Same-generation maintenance observations, including their live caller."""

from . import maintenance_selections, maintenance_state
from .history import WikiHistory
from .maintenance_contract import actor_for
from .registry import registry
from .tasks import tasks


def status(user_id, *, scope=None, app_id=None, **filters):
    with registry.using(user_id) as (store, registration), WikiHistory(store).locked():
        with registry.registered(user_id, store, registration):
            task = tasks.caller(user_id, scope, registration, app_id=app_id)
            result = maintenance_state.status(store, registration, user_id, actor=actor_for(task, app_id),
                                              model_response=task is not None, **filters)
            tasks.caller(user_id, scope, registration, app_id=app_id, original=task)
            return result


def create(user_id, *, caller, directory="Library", limit=None):
    with registry.using(user_id) as (store, registration), WikiHistory(store).locked():
        with registry.registered(user_id, store, registration, caller=caller):
            task = caller.task(user_id, registration)
            return maintenance_selections.create(store, registration, user_id, actor=actor_for(task, caller.app_id),
                                                 directory=directory, limit=limit,
                                                 authorize=lambda: caller.task(user_id, registration, original=task))


def read(user_id, selection_id, *, scope=None, app_id=None, limit=None, cursor=None):
    with registry.using(user_id) as (store, registration):
        with registry.registered(user_id, store, registration):
            task = tasks.caller(user_id, scope, registration, app_id=app_id)
            result = maintenance_selections.read(store, registration, user_id, selection_id,
                                                 actor=actor_for(task, app_id), limit=limit, cursor=cursor)
            tasks.caller(user_id, scope, registration, app_id=app_id, original=task)
            return result

"""Shared maintenance observation contract; observations confer no authority."""

import hashlib
import json

PURPOSE = "daily-review-v1"
DAILY_SCOPE = "按当前用途复核当前人工版本，只修确有必要且获准的派生信息；保留原文、未知字段及删除结果，短记录不强套文章结构；相关旧文只读，不复刻敏感值。"
# A legacy save qualifies only through a real settled worker item/publication.
MAINTENANCE_OPERATIONS = frozenset({"save", "organize", "normalize", "publish", "restructure", "patch", "confirm_sources", "no_change"})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def actor_for(task, app_id=None):
    if task is not None:
        return task.agent_id or "session:" + task.session_id
    return "app:" + app_id if app_id is not None else "human"

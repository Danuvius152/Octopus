"""Framework-owned task guides, loaded at the shared harness boundary."""

import json
from pathlib import Path
from typing import Any

from .contracts import GuideModule
from .registry import registry
from .store import WikiError
from .tasks import tasks

PROMPTS = Path(__file__).with_name("prompts")
PUBLIC_GUIDES = ("query", "retrieval", "entry")
WORKER_GUIDES = ("maintenance", "article", "writing")


def guide(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def instructions(mode: str, task_id: str | None = None) -> str:
    task = tasks.active.get(task_id)
    worker = tasks.is_curator_worker(task)
    injected = ["worker"] if mode == "maintain" and worker else ["query"]
    if injected != ["worker"] and not (task and task.job_id) and mode in {"auto", "maintain"}:
        injected.append("entry")
    card: dict[str, Any] = {
        "mode": mode,
        "organization": "Library 分类；Key note；细粒度标签；必要关联",
        "excluded": "PDF 不处理；工具/tmp/配套静态资产按需读取",
        "injected_guides": injected,
    }
    if task:
        try:
            config = registry.config(task.user_id)
            card |= {
                "vault_id": config["vault_id"],
                "curator_agent_id": config["curator_agent_id"],
                "writable_files": list(task.writable),
            }
        except WikiError:
            card["state"] = "主库尚未绑定；用 info 确认状态"
        if task.job_id:
            card["job_id"] = task.job_id
        elif mode == "maintain":
            from .tasks import daily_command

            if daily_command(task.request):
                card["selection"] = {"mode": "incremental", "directory": "Library"}
    base = "本次知识操作卡：" + json.dumps(card, ensure_ascii=False) + "\n"
    base += "injected_guides 标记本轮常驻内容；按需指南已有内容时复用，仅上下文缺失时再 load_guide。\n"
    base += guide("access") + "\n"
    if mode == "maintain" and worker:
        base += guide("worker-v2")
    else:
        base += guide("query-v2")
        if task and task.job_id:
            base += "\n当前维护 assignment 已结束或不可用；只可查询，不发起或继续维护。\n"
        elif mode == "auto":
            base += "\n" + guide("auto-entry")
        elif mode == "maintain":
            base += "\n" + guide("entry")
        elif mode == "read":
            base += "\n本轮只读：可查询，不可提交维护或写入。\n"
    return base


def load_guide(module: GuideModule, task_id: str | None) -> str:
    task = tasks.active.get(task_id)
    if task and module in PUBLIC_GUIDES:
        return guide({"query": "query-v2", "retrieval": "retrieval-v2", "entry": "auto-entry" if task.mode == "auto" else "entry"}[module])
    if not tasks.is_curator_worker(task) or module not in WORKER_GUIDES:
        raise WikiError("This guide requires an active curator assignment", 403)
    return guide({"maintenance": "maintenance-v2", "article": "article-v2", "writing": "writing"}[module])

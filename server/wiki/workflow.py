"""Framework-owned task guides, loaded at the shared harness boundary."""

import json
from pathlib import Path
from typing import Any

from .registry import registry
from .store import WikiError
from .tasks import tasks

PROMPTS = Path(__file__).with_name("prompts")


def guide(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def instructions(mode: str, task_id: str | None = None) -> str:
    task = tasks.active.get(task_id)
    card: dict[str, Any] = {
        "mode": mode,
        "organization": "Library 分类；Key note；细粒度标签；必要关联",
        "excluded": "PDF 不处理；工具/tmp/配套静态资产按需读取",
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
    base = "本次知识操作卡：" + json.dumps(card, ensure_ascii=False) + "\n"
    base += "使用本次 mcp__wiki__ 工具；工具描述提供真实参数。原生助手共享当前任务凭据，等待它们结束后完成任务；之后没有知识授权。\n"
    base += guide("query")
    if mode == "maintain":
        base += (
            "\n"
            + guide("entry")
            + "\n"
            + guide("maintenance")
            + "\n"
            + guide("writing")
        )
    return base

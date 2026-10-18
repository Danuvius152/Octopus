"""One fresh authority description for task cards, guides and visible tools.

Descriptions and hashes are observations. Every HTTP mutation still checks the
live turn, original assignment, registration and captured document identities.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..mcp_result import TEXT_RESULT_MAX_BYTES
from .contracts import GuideModule
from .registry import registry
from .schema import VERSION as SCHEMA_VERSION
from .store import WikiError
from .tasks import WikiTask, tasks

PROMPTS = Path(__file__).with_name("prompts")
POLICY_VERSION = 3
CARD_MAX_BYTES = 8192
PATHS_MAX_BYTES = 4096
PATHS_MAX_ITEMS = 16
PUBLIC_GUIDES = ("query", "retrieval", "entry")
WORKER_GUIDES = ("maintenance", "article", "writing")
GUIDE_FILES = (
    "access", "query-v2", "retrieval-v2", "entry", "auto-entry",
    "worker-v2", "maintenance-v2", "article-v2", "writing",
)
QUERY_TOOLS = frozenset({
    "info", "search", "read", "read_many", "list_documents", "graph", "inspect_image",
    "jobs", "load_guide", "maintenance_status", "maintenance_select", "maintenance_selection",
})


def guide(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def _paths(card, name, values, remaining):
    """Omit complete references when full; a shortened path names another file."""
    selected = []
    for path in values:
        size = len(json.dumps(path, ensure_ascii=False).encode()) + 2
        if len(selected) < PATHS_MAX_ITEMS and size <= remaining:
            selected.append(path)
            remaining -= size
    card |= {name: selected, name + "_count": len(values), name + "_truncated": len(selected) != len(values)}
    return remaining


@dataclass(frozen=True)
class PolicyView:
    card: dict[str, Any]
    tools: frozenset[str]
    guides: dict[str, str]
    injected_text: tuple[str, ...]

    def load(self, module: GuideModule) -> str:
        if module not in self.guides:
            raise WikiError("This guide requires an active curator assignment", 403)
        return self.guides[module]


def view(task: WikiTask | None, *, mode: str = "read") -> PolicyView:
    """Re-evaluate the original task and assignment; never reuse a past card."""
    root = (task.active_task or task) if task else None
    live = root is not None and any(current is root for current in list(tasks.active.values()))
    mode = task.mode if task else mode
    config = None
    if task:
        try:
            config = registry.config(task.user_id)
        except WikiError:
            pass
    grant = tasks.current_grant(task, include_selection=True) if live and mode == "maintain" and task.job_id else None
    if grant and (not config or (grant["vault_id"], grant["registration_generation"])
                  != (config["vault_id"], config["registration_generation"])):
        grant = None
    worker = grant is not None
    submitter = bool(live and mode in {"auto", "maintain"} and not task.job_id)
    role = "worker" if worker else "ended_worker" if task and task.job_id else "submitter" if submitter else "query"
    names = set(QUERY_TOOLS) if live else set()
    if worker or submitter:
        names |= {"cancel_job", "retry_job"}
    if grant is not None:
        names |= {"organize", "blocks", "restructure", "revise"}
        if grant["allow_new_pages"]:
            names.add("publish")
    elif submitter:
        names.add("maintain")

    # The digest describes the installed text bundle, not current permission.
    # Keeping one snapshot also makes load_guide's digest match its actual text.
    bundle = {name: guide(name) for name in GUIDE_FILES}
    digest = hashlib.sha256(json.dumps([POLICY_VERSION, bundle], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    injected = ["worker"] if worker else ["query", "entry"] if submitter else ["query"]
    available = {"query": bundle["query-v2"], "retrieval": bundle["retrieval-v2"],
                 "entry": bundle["auto-entry" if mode == "auto" else "entry"]} if live else {}
    if worker:
        available |= {"maintenance": bundle["maintenance-v2"], "article": bundle["article-v2"], "writing": bundle["writing"]}
    card: dict[str, Any] = {
        "policy_version": POLICY_VERSION, "guide_hash": digest, "schema_version": SCHEMA_VERSION,
        "actor_role": role, "mode": mode,
        "organization": "Library 分类；Key note；细粒度标签；必要关联",
        "excluded": "PDF 不处理；工具/tmp/配套静态资产按需读取",
        "injected_guides": injected, "available_guides": sorted(available), "available_tools": sorted(names),
        "response_limits": {"text_bytes": TEXT_RESULT_MAX_BYTES, "card_bytes": CARD_MAX_BYTES,
                            "scope_paths": PATHS_MAX_ITEMS},
    }
    if config:
        card |= {"vault_id": config["vault_id"], "curator_agent_id": config["curator_agent_id"],
                 "registration_generation": config["registration_generation"], "writable_files": []}
    elif task:
        card["state"] = "主库尚未绑定；用 info 确认状态"
    if task and task.job_id:
        card |= {"job_id": task.job_id, "scope_details": {"tool": "jobs", "job_id": task.job_id, "view": "full", "export": True}}
    if grant is not None:
        # The V2 grant's persistent primary identity is its original job_id.
        card |= {"grant_version": 2, "grant_id": grant["job_id"],
                 "selection_ids": grant["selection_ids"], "selection_ids_truncated": grant["selection_ids_truncated"]}
        if len(grant["selection_ids"]) == 1 and not grant["selection_ids_truncated"]:
            card["selection_id"] = grant["selection_ids"][0]
        remaining = _paths(card, "writable_files", [item["current_path"] for item in grant["selected_documents"] if item["current_path"]], PATHS_MAX_BYTES)
        _paths(card, "allowed_new_paths", grant["allowed_new_paths"], remaining)
    elif submitter and mode == "maintain":
        from .tasks import daily_command

        if daily_command(task.request):
            card["selection"] = {"mode": "incremental", "directory": "Library"}
    body = [bundle["access"]]
    if worker:
        body += ["正文与新页按操作卡目标；已有导航 Markdown 可用 relations-only organize 补本批入口，不取得该页正文修改权。", bundle["worker-v2"]]
    else:
        body.append(bundle["query-v2"])
        if task and task.job_id:
            body.append("当前维护 assignment 已结束或不可用；只可查询，不发起或继续维护。")
        elif submitter:
            body.append(bundle["auto-entry" if mode == "auto" else "entry"])
        elif mode == "read":
            body.append("本轮只读：可查询，不可提交维护或写入。")
    if len(json.dumps(card, ensure_ascii=False).encode()) > CARD_MAX_BYTES:
        raise WikiError("Wiki operation card exceeds its response budget; use current job details", 503)
    return PolicyView(card, frozenset(names), available, tuple(body))


def instructions(mode: str, task_id: str | None = None) -> str:
    current = view(tasks.active.get(task_id), mode=mode)
    return ("本次知识操作卡：" + json.dumps(current.card, ensure_ascii=False) + "\n"
            + "injected_guides 标记本轮常驻内容；已有内容时复用，缺失时再 load_guide；guide_hash 只标记文本版本，不授予权限。\n"
            + "\n".join(current.injected_text))


def load_guide(module: GuideModule, task_id: str | None) -> str:
    return view(tasks.active.get(task_id)).load(module)

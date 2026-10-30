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
POLICY_VERSION = 4
CARD_MAX_BYTES = 8192
PATHS_MAX_BYTES = 4096
PATHS_MAX_ITEMS = 16
PUBLIC_GUIDES = ("query", "retrieval", "entry")
WORKER_GUIDES = ("worker", "maintenance", "article", "writing")
GUIDE_FILES = (
    "access", "query-v2", "retrieval-v2", "entry",
    "worker-v2", "maintenance-v2", "article-v2", "writing",
)
QUERY_TOOLS = frozenset({
    "info", "search", "read", "read_many", "list_documents", "graph", "inspect_image",
    "jobs", "load_guide", "maintenance_status", "maintenance_select", "maintenance_selection",
})

BOOTSTRAP = """共享 Wiki 可在普通任务中按需自主查询，无需 /wiki 或先询问其他 agent；查询不自动保存对话或维护文章。
按本次目的 load_guide：查询用 query，提交维护用 entry，执行维护先用 worker，其余模块按需。首次加载取得公共规则与操作卡；同一上下文复用完整指南，后续模块可按工具说明省略未变的公共回包。跨回合先用 info 核对 guide_digests，缺失或变动的正文重新加载。
查询共享资料使用 Wiki 工具：已知路径/id/uid 直接 read，未知位置先 compact search 或相关目录发现，已确定多个目标或范围用 read_many；按当前上下文实际保留的同版正文补足缺口。
操作卡、任务、来源要求和权限以本轮实时回执为准；指南与摘要不授予权限，服务端逐次检查授权。使用当前工具，不复用旧票据；未使用 Wiki 的任务无需加载。"""


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
    access_text: str

    def load(self, module: GuideModule) -> str:
        if module not in self.guides:
            raise WikiError("This guide requires an active curator assignment", 403)
        return self.guides[module]

    def guide_response(self, module: GuideModule, known_context_digest: str | None = None) -> dict:
        content = self.load(module)
        context_digest = hashlib.sha256(json.dumps(
            [self.card, self.access_text], ensure_ascii=False, sort_keys=True,
        ).encode()).hexdigest()
        result = {"module": module, "content": content,
                  "policy_version": self.card["policy_version"], "guide_hash": self.card["guide_hash"],
                  "guide_digest": self.card["guide_digests"][module], "context_digest": context_digest}
        if context_digest != known_context_digest:
            result |= {"access": self.access_text, "operation": self.card}
        return result


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
    submitter = bool(live and tasks.may_submit(task))
    if submitter and task.knowledge is not None:
        try:
            if config is None:
                raise WikiError("The Wiki registration is unavailable", 403)
            tasks.require_submitter(task, config)
        except WikiError:
            submitter = False
    role = "worker" if worker else "ended_worker" if task and task.job_id else "submitter" if submitter else "query"
    names = set(QUERY_TOOLS) if live else set()
    if worker or submitter:
        names |= {"cancel_job", "retry_job"}
    if grant is not None:
        names |= {"organize", "blocks", "restructure", "revise"}
        if grant["allow_new_pages"]:
            names.add("publish")
    elif submitter:
        names |= {"maintain", "maintain_batch"}

    # The digest describes the installed text bundle, not current permission.
    # Keeping one snapshot also makes load_guide's digest match its actual text.
    bundle = {name: guide(name) for name in GUIDE_FILES}
    digest = hashlib.sha256(json.dumps([POLICY_VERSION, bundle], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    available = {"query": bundle["query-v2"], "retrieval": bundle["retrieval-v2"],
                 "entry": bundle["entry"]} if live else {}
    if worker:
        available |= {
            "worker": bundle["worker-v2"],
            "maintenance": bundle["maintenance-v2"], "article": bundle["article-v2"], "writing": bundle["writing"],
        }
    guide_digests = {module: hashlib.sha256((bundle["access"] + "\n\n" + text).encode()).hexdigest()
                     for module, text in available.items()}
    card: dict[str, Any] = {
        "policy_version": POLICY_VERSION, "guide_hash": digest, "schema_version": SCHEMA_VERSION,
        "actor_role": role, "mode": mode,
        "may_submit": submitter,
        "source_requirements": list(task.knowledge.sources) if task and task.knowledge else [],
        "organization": "Library 分类；Key note；细粒度标签；必要关联",
        "excluded": "PDF 不处理；工具/tmp/配套静态资产按需读取",
        "injected_guides": [], "available_guides": sorted(available), "available_tools": sorted(names),
        "guide_digests": guide_digests,
        "response_limits": {"text_bytes": TEXT_RESULT_MAX_BYTES, "card_bytes": CARD_MAX_BYTES,
                            "scope_paths": PATHS_MAX_ITEMS},
    }
    if config:
        card |= {"vault_id": config["vault_id"], "curator_agent_id": config["curator_agent_id"],
                 "registration_generation": config["registration_generation"], "writable_files": []}
    elif task:
        card["state"] = "主库尚未绑定；用 info 确认状态"
    if task and task.job_id:
        card["job_id"] = task.job_id
    if grant is not None:
        # The V2 grant's persistent primary identity is its original job_id.
        card |= {"grant_version": 2, "grant_id": grant["job_id"],
                 "selection_ids": grant["selection_ids"], "selection_ids_truncated": grant["selection_ids_truncated"]}
        if len(grant["selection_ids"]) == 1 and not grant["selection_ids_truncated"]:
            card["selection_id"] = grant["selection_ids"][0]
        remaining = _paths(card, "writable_files", [item["current_path"] for item in grant["selected_documents"] if item["current_path"]], PATHS_MAX_BYTES)
        _paths(card, "allowed_new_paths", grant["allowed_new_paths"], remaining)
        if card["writable_files_truncated"] or card["allowed_new_paths_truncated"]:
            card["scope_details"] = {"tool": "jobs", "job_id": task.job_id, "view": "full", "export": True}
    elif submitter and mode == "maintain":
        from .tasks import daily_command

        if daily_command(task.request):
            card["selection"] = {"mode": "incremental", "directory": "Library"}
    if len(json.dumps(card, ensure_ascii=False).encode()) > CARD_MAX_BYTES:
        raise WikiError("Wiki operation card exceeds its response budget; use current job details", 503)
    return PolicyView(card, frozenset(names), available, bundle["access"])


def instructions(mode: str, task_id: str | None = None) -> str:
    """Route this turn by the existing policy; guides and authority remain live."""
    current = view(tasks.active.get(task_id), mode=mode)
    role = current.card["actor_role"]
    route = {
        "worker": "当前角色 worker：框架已分派维护，先加载 worker 指南。",
        "submitter": "当前角色 submitter：包括普通 Weber 会话；提交或重试维护先加载 entry，实际维护交给框架 worker。",
        "ended_worker": "当前角色 ended_worker：维护分派已结束，仅可查询；按需加载 query。",
        "query": "当前角色 query：按需加载 query，仅查询，不提交维护。",
    }[role]
    return BOOTSTRAP + "\n" + route


def load_guide(module: GuideModule, task_id: str | None) -> str:
    return view(tasks.active.get(task_id)).load(module)

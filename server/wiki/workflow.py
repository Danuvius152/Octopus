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
需要使用 Wiki 时再取得本轮操作卡与指南：没有对应指南先 load_guide；已有指南先用 info 核对当前操作卡的 guide_digests，仅复用仍在上下文且正文 digest 相同的内容。普通查询用 query，提交具体维护请求用 entry；执行维护先加载 worker，再按需加载 maintenance、article、writing，这些模块只向当前有效 curator assignment 开放。retrieval 按检索细节需要加载。load_guide 同时返回当前操作卡、公共规则和该模块正文。
操作卡、任务、来源要求和权限以本轮实时回执为准；历史卡、指南版本及缓存均不授予权限，服务端逐次检查授权。使用当前提供的 Wiki 工具，禁止复用旧票据。指南被压缩或不再完整可见时重新加载；未使用 Wiki 的任务无需加载。"""


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
        names.add("maintain")

    # The digest describes the installed text bundle, not current permission.
    # Keeping one snapshot also makes load_guide's digest match its actual text.
    bundle = {name: guide(name) for name in GUIDE_FILES}
    digest = hashlib.sha256(json.dumps([POLICY_VERSION, bundle], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    available = {"query": bundle["query-v2"], "retrieval": bundle["retrieval-v2"],
                 "entry": bundle["entry"]} if live else {}
    if worker:
        available |= {
            "worker": "正文与新页按操作卡目标；已有导航 Markdown 可用 relations-only organize 补本批入口，不取得该页正文修改权。\n\n" + bundle["worker-v2"],
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
    if len(json.dumps(card, ensure_ascii=False).encode()) > CARD_MAX_BYTES:
        raise WikiError("Wiki operation card exceeds its response budget; use current job details", 503)
    return PolicyView(card, frozenset(names), available, bundle["access"])


def instructions(mode: str, task_id: str | None = None) -> str:
    """Stable discovery only; live authority and guide bodies are loaded on use."""
    return BOOTSTRAP


def load_guide(module: GuideModule, task_id: str | None) -> str:
    return view(tasks.active.get(task_id)).load(module)

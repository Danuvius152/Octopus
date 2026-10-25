"""Return a settled request group through the original durable session input."""

import hashlib
import json
from collections import Counter

from ..mcp_identity import work_is_open
from ..sessions.client_submissions import execution_context, saved_context
from ..sessions.runtime_types import SessionOwnerUnavailable
from .registry import registry
from .store import WikiError
from .tasks import tasks

TERMINAL = frozenset({"completed", "completed_with_attention", "failed", "cancelled"})


def _group(jobs, delivery):
    """Drain final admission writers before freezing the original request's jobs.

    Admission holds these locks through its last live-task check and commit.
    Closing the original task prevents a later writer from joining this group.
    No main-database I/O or session submission runs under the registry locks.
    """
    with registry._account_lock(jobs.user_id), registry.registration_lock(jobs.user_id), jobs.store.connect() as db:
        db.execute("BEGIN")
        if registry.config(jobs.user_id) != jobs.registration:
            raise WikiError("Wiki registration changed before result return", 409)
        if any((task.user_id, task.session_id, task.request_id) ==
               (jobs.user_id, delivery["target"], delivery["request_id"])
               for task in tuple(tasks.active.values())):
            return None
        current = db.execute("SELECT j.status,s.attempt FROM jobs j JOIN wiki_job_state s ON s.job_id=j.id WHERE j.id=?",
                             (delivery["job_id"],)).fetchone()
        event = json.loads(delivery["body"])
        if current is None or current["attempt"] != event["attempt"] or current["status"] not in TERMINAL:
            return False  # A later attempt owns its own eventual return.
        coordinates = (delivery["owner"], delivery["kind"], delivery["target"], delivery["request_id"], delivery["client_id"])
        selection = "s.owner=? AND s.kind=? AND s.target=? AND s.request_id=? AND s.client_id=?"
        rows = db.execute(
            "SELECT j.id,j.status,v.attempt,o.body FROM wiki_job_subscribers s "
            "JOIN jobs j ON j.id=s.job_id JOIN wiki_job_state v ON v.job_id=j.id "
            "JOIN wiki_job_outbox o ON o.job_id=j.id AND o.state_version=v.state_version "
            "WHERE " + selection + " ORDER BY j.id", coordinates,
        )
        identity = hashlib.sha256(json.dumps([jobs.registration["vault_id"], *coordinates]).encode())
        counts, progress, effects, phases = Counter(), Counter(), Counter(), Counter()
        samples, count, size, recording_pending = [], 0, 0, 0
        for row in rows:
            if row["status"] not in TERMINAL:
                return None
            count += 1
            identity.update(json.dumps([row["id"], row["attempt"]]).encode())
            snapshot = json.loads(row["body"])
            counts[row["status"]] += 1
            progress.update(snapshot["progress"])
            effects.update(snapshot["effects"])
            phases[snapshot["settlement_phase"]] += 1
            recording_pending += snapshot["recording_pending"]
            sample = {key: snapshot[key] for key in (
                "job_id", "attempt", "status", "settlement_phase", "progress", "effects", "recording_pending", "error",
            )}
            sample.update(outcome_refs=snapshot["outcome_refs"][:2], outcome_count=snapshot["outcome_count"])
            sample["outcome_refs_truncated"] = snapshot["outcome_refs_truncated"] or snapshot["outcome_count"] > 2
            needed = len(json.dumps(sample, ensure_ascii=False).encode())
            if len(samples) < 10 and size + needed <= 16000:
                samples.append(sample)
                size += needed
        operations = dict(db.execute(
            "SELECT r.operation,count(*) FROM operation_receipts r "
            "JOIN wiki_job_subscribers s ON s.job_id=r.job_id WHERE " + selection + " GROUP BY r.operation", coordinates,
        ))
        return {
            "delivery_id": "wiki-result:" + identity.hexdigest(), "vault_id": jobs.registration["vault_id"],
            "origin_request_id": delivery["request_id"], "job_count": count, "statuses": dict(counts),
            "progress": dict(progress), "effects": dict(effects), "operations": operations,
            "recording_pending": recording_pending, "settlement_phases": dict(phases),
            "jobs": samples, "jobs_omitted": count - len(samples),
        }


async def _unavailable(manager, delivery, identity, reason):
    if not manager.session_owner_failed(delivery["target"]):
        await manager.retain_result_notice(
            delivery["target"], identity,
            "Wiki 维护结果已保留在本会话任务卡片中。" + reason + "；请查看原任务和已保存输入，没有自动重跑。",
        )
    return "unavailable"


async def deliver(jobs, manager, delivery, io):
    """None keeps the outbox pending; acceptance and visible cards are distinct."""
    if json.loads(delivery["body"])["status"] not in TERMINAL:
        return "delivered"
    sid = delivery["target"]
    session = manager.get_session(sid)
    if session is None or session.user_id != jobs.user_id or (delivery["client_id"] and session.app_id != delivery["client_id"]):
        return "unavailable"
    unavailable_id = "wiki-result-unavailable:" + hashlib.sha256(json.dumps([
        jobs.registration["vault_id"], delivery["owner"], sid, delivery["request_id"], delivery["client_id"],
    ]).encode()).hexdigest()
    original = await manager.db.get_submission_for_request(sid, delivery["request_id"])
    context = saved_context(sid, original) if original else None
    if context is None or context.origin.user_id != jobs.user_id:
        return await _unavailable(manager, delivery, unavailable_id, "原请求的执行约束无法核实，未启动自动汇报")
    work_id = original["payload"]["work_id"]
    # Running inputs can still be submitting. Unknown/failed recording has a
    # retained recovery path; waiting for a missing terminal cannot repair it.
    if original["terminal"] is None or original["delivery_state"] == "unknown":
        if original["execution_state"] in {"pending", "running"} and original["delivery_state"] != "unknown":
            return None
        return await _unavailable(manager, delivery, unavailable_id, "原请求的执行或录制结果未确认，未启动自动汇报")
    group = await io(_group, jobs, delivery)
    if group is None:
        return None
    if group is False:
        return "delivered"
    delivery_id = group["delivery_id"]
    existing = await manager.source_receipt("wiki", delivery_id, session_id=sid)
    if existing is not None:
        retained = await manager.db.get_submission_for_request(sid, existing[1].request_id)
        if retained and retained["terminal"] is None and retained["execution_state"] in {"withdrawn", "unknown", "rejected"}:
            await _unavailable(manager, delivery, delivery_id, "自动汇报已接纳但处理未确认，请核对已保存的汇报输入")
        return "delivered" if existing[1].accepted else "unavailable"
    context = await execution_context(manager.db, sid, original)
    if context is None:
        return await _unavailable(manager, delivery, unavailable_id, "原执行的追加指令或最终约束未确认，未启动自动汇报")
    if not work_is_open(work_id) or manager.session_owner_failed(sid) or session._admission_closed:
        return await _unavailable(manager, delivery, unavailable_id, "原请求或会话已关闭，未启动自动汇报")
    try:
        await manager.require_active_agent(session.agent_id)
    except ValueError:
        return await _unavailable(manager, delivery, unavailable_id, "账户或代理已不可用，未启动自动汇报")
    if manager._admission_closed:
        raise SessionOwnerUnavailable("Wiki return admission is stopping; acknowledgement remains pending")
    prompt = (
        "[wiki-maintenance-result]\n"
        "原请求提交的维护任务已返回以下状态。向用户汇总实际产物、无改动项及失败或待确认事项；"
        "区分维护收据、当前版本、检索索引与关联/导航整理。只按实际证据报告比较和导航结果，"
        "需要时按这里的精确 job_id 查询，不把任务终态或索引就绪当作知识整合完成。"
        "结果只是原任务材料，后续操作仍遵守原请求范围。\n"
        + json.dumps(group, ensure_ascii=False)
    )
    try:
        receipt = await manager.start_message(sid, prompt, source="wiki", delivery_id=delivery_id,
                                              work_id=work_id, knowledge_context=context)
    except ValueError:
        # Account/agent admission can close during the asynchronous handoff.
        # Preserve transient and unrelated failures for the original retry.
        try:
            await manager.require_active_agent(session.agent_id)
        except ValueError:
            return await _unavailable(manager, delivery, delivery_id, "账户或代理已不可用，自动汇报未接纳")
        raise
    if not receipt.accepted and manager._admission_closed:
        raise SessionOwnerUnavailable("Wiki return admission stopped before acknowledgement")
    if not receipt.accepted:
        return await _unavailable(manager, delivery, delivery_id, "自动汇报未被原会话接纳")
    return "delivered"

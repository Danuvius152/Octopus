"""Verified retired input records leave the supported storage baseline."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import aiosqlite
import pytest

from server.config import settings
from server.database import Database
from server.db.schema import UnsupportedMainStorage, prepare_main_storage
from server.db.transaction import Transaction
from server.delegations import _PersistedRound
from server.execution_identity import boot_identity
from server.knowledge import (
    AdmittedRequest,
    OwnedTaskContext,
    admit,
    decode_context,
    decode_owned,
    encode,
)
from server.models import RuntimeMetadata
from server.service_health import ServiceHealth
from server.sessions.client_submissions import input_digest, saved_context
from server.wiki.contracts import JobSnapshot
from tests.accounts import create_account
from tests.main_storage import write_unversioned_baseline, write_version_one_baseline
from tests.test_service_runtime import installation_settings


def notification_fact(stored, *, owner, target):
    snapshot = JobSnapshot(
        job_id="wiki-job", state_version=1, attempt=0, status="completed", settlement_phase="confirmed",
        worker_session_id=None, scope="Current operation", scope_truncated=False,
        progress={"total": 1, "completed": 1, "attention": 0, "failed": 0},
        outcome_refs=[], outcome_count=0, outcome_refs_truncated=False,
        error=None, error_truncated=False, created_at="original-time", updated_at="finished-time",
    )
    stored.execute("INSERT INTO wiki_job_notifications(vault_id,event_id,subscriber_id,user_id,job_id,state_version,kind,"
                   "target,request_id,client_id,body,outcome,received_at) "
                   "VALUES('vault','event','subscriber',?,'wiki-job',1,'chat',?,'retired-request','',?,'delivered','finished-time')",
                   (owner, target, snapshot.model_dump_json()))


async def prior_input(tmp_path, *, accepted=False):
    path = tmp_path / "main.db"
    database = Database(str(path))
    await database.initialize()
    try:
        owner, _ = await create_account(database, extra_roots=[tmp_path])
        agent = await database.get_system_agent(owner["id"])
        await database.save_session("session", "Owned", str(tmp_path), "2026-10-24",
                                    agent_id=agent["id"], user_id=owner["id"])
        context = admit(session_id="session", request_id="retired-request", user_id=owner["id"],
                        prompt="Prior read-only request", selected="read")
        payload = {"content": context.trusted_request, "attachment_ids": [], "attachments": [],
                   "wiki_mode": "read", "knowledge": encode(context), "trusted_request": context.trusted_request,
                   "source": "human", "delivery_id": None, "work_id": "prior-work", "order": 1}
        await database.admit_client_submission(
            "session", "retired-client", input_digest(payload), "retired-request", payload=payload,
            receipt={"accepted": accepted, "disposition": "accepted" if accepted else "rejected"},
        )
    finally:
        await database.close()
    payload["wiki_mode"] = "off"
    with closing(sqlite3.connect(path)) as stored:
        stored.execute("PRAGMA user_version=3")
        stored.execute(
            "UPDATE client_submissions SET payload=?,payload_sha256=?,execution_state='withdrawn',reason_code=?",
            (json.dumps(payload), input_digest(payload), None if accepted else "legacy_rejected"),
        )
        stored.commit()
    return path


@pytest.mark.parametrize(("column", "value"), [
    ("receipt", None), ("receipt", '{"accepted":0}'), ("receipt", '{"accepted":"false"}'),
    ("receipt", '{"accepted":true}'), ("execution_state", "running"), ("execution_state", "unknown"),
    ("execution_owner", "retired-request"), ("execution_generation", "original-generation"),
    ("execution_boot", "original-boot"), ("process_group", 200),
    ("delivery_state", "unknown"), ("recovery_of", "another-input"),
])
async def test_refusal_label_never_overrides_contradictory_or_missing_evidence(tmp_path, column, value):
    path = await prior_input(tmp_path)
    with closing(sqlite3.connect(path)) as stored:
        stored.execute(f"UPDATE client_submissions SET {column}=?", (value,))
        stored.commit()
    before = path.read_bytes()
    with pytest.raises(UnsupportedMainStorage):
        await Database(str(path)).inspect()
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as stored:
        assert stored.execute("PRAGMA user_version").fetchone() == (3,)
        assert stored.execute("SELECT count(*) FROM client_submissions").fetchone() == (1,)


@pytest.mark.parametrize("kind", ["terminal", "request", "attempt", "dependent", "child", "source"])
async def test_refused_input_with_actual_execution_or_recovery_references_is_not_erased(tmp_path, kind):
    path = await prior_input(tmp_path)
    with closing(sqlite3.connect(path)) as stored:
        if kind == "source":
            payload = json.loads(stored.execute("SELECT payload FROM client_submissions").fetchone()[0])
            context = decode_context(payload["knowledge"])
            owned = OwnedTaskContext(context, AdmittedRequest(context.origin, context, "prior-work"))
            stored.execute("INSERT INTO bg_tasks(id,session_id,command,working_dir,status,started_at,knowledge_context) "
                           "VALUES('source-task','session','offline task',?,'completed','original-time',?)",
                           (str(tmp_path), encode(owned)))
        elif kind in {"dependent", "child"}:
            stored.execute(
                "INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id,execution_owner,recovery_of) "
                "VALUES('session','other-client','current-digest','other-request',?,?)",
                ("retired-request" if kind == "dependent" else None,
                 "retired-client" if kind == "child" else None),
            )
        else:
            stored.execute(
                "INSERT INTO messages(session_id,seq,role,type,terminal_request_id,tool_input) "
                "VALUES('session',1,'assistant','text',?,?)",
                ("retired-request" if kind == "terminal" else None,
                 json.dumps({kind + "_context": {"request_id": "retired-request"}})),
            )
        stored.commit()
    before = path.read_bytes()
    with pytest.raises(UnsupportedMainStorage):
        await Database(str(path)).inspect()
    assert path.read_bytes() == before


@pytest.mark.parametrize("retirement", ["managed", "previous-boot"])
async def test_exact_committed_result_survives_retirement_of_its_old_raw_input(tmp_path, retirement):
    path = await prior_input(tmp_path, accepted=True)
    with closing(sqlite3.connect(path)) as stored:
        stored.execute(
            "UPDATE client_submissions SET execution_state='done',execution_owner=request_id,"
            "execution_generation='original-generation',execution_boot=?,process_group=200",
            (boot_identity() if retirement == "managed" else str(uuid.uuid4()),),
        )
        outcome = {"kind": "outcome", "request_id": "retired-request", "status": "success"}
        stored.execute(
            "INSERT INTO messages(session_id,seq,role,type,content,terminal_request_id) "
            "VALUES('session',1,'system','result',?,'retired-request')", (json.dumps(outcome),),
        )
        stored.commit()
        messages = stored.execute("SELECT * FROM messages").fetchall()
    reopened = Database(str(path))
    await reopened.inspect(previous_writers_retired=retirement == "managed")
    await reopened.initialize(previous_writers_retired=retirement == "managed")
    try:
        assert await reopened.get_client_submission("session", "retired-client") is None
        assert await reopened.get_committed_terminal("session", "retired-request") == outcome
        async with reopened.transaction(write=False) as tx:
            assert await tx.fetch_all("SELECT * FROM messages") == messages
        assert await reopened.get_submission_for_request("session", "retired-request") is None
    finally:
        await reopened.close()
    with closing(sqlite3.connect(path)) as stored:
        assert stored.execute("PRAGMA user_version").fetchone() == (4,)


async def test_never_started_accepted_old_input_is_retired_at_the_cold_boundary(tmp_path):
    path = await prior_input(tmp_path, accepted=True)
    reopened = Database(str(path))
    await reopened.inspect()
    await reopened.initialize()
    try:
        assert await reopened.get_client_submission("session", "retired-client") is None
    finally:
        await reopened.close()


async def test_current_successor_can_be_recovered_after_its_old_ancestor_is_retired(tmp_path):
    path = await prior_input(tmp_path, accepted=True)
    with closing(sqlite3.connect(path)) as stored:
        parent = json.loads(stored.execute("SELECT payload FROM client_submissions").fetchone()[0])
        origin = decode_context(parent["knowledge"]).origin
        context = admit(session_id="session", request_id="child-request", user_id=origin.user_id,
                        prompt="Current child input", selected="read")
        payload = {**parent, "content": context.trusted_request, "trusted_request": context.trusted_request,
                   "wiki_mode": "read", "knowledge": encode(context), "recovery_of_client_id": "retired-client"}
        stored.execute("INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id,payload,receipt,"
                       "execution_state,recovery_of) VALUES('session','child-client',?,'child-request',?,?,'withdrawn','retired-client')",
                       (input_digest(payload), json.dumps(payload), '{"accepted":true,"disposition":"accepted"}'))
        stored.commit()
        original_child = stored.execute("SELECT * FROM client_submissions WHERE client_id='child-client'").fetchone()
    reopened = Database(str(path))
    await reopened.initialize()
    try:
        assert await reopened.get_client_submission("session", "retired-client") is None
        child = await reopened.get_client_submission("session", "child-client")
        assert child["recovery_of"] == "retired-client" and saved_context("session", child) == context
        async with reopened.transaction(write=False) as tx:
            assert await tx.fetch_one("SELECT * FROM client_submissions WHERE client_id='child-client'") == original_child
        fresh = admit(session_id="session", request_id="fresh-request", user_id=origin.user_id,
                      prompt="Restore the current child", selected="read")
        fresh_payload = {**payload, "content": fresh.trusted_request, "trusted_request": fresh.trusted_request,
                         "knowledge": encode(fresh), "recovery_of_client_id": "child-client"}
        created, successor = await reopened.admit_client_submission(
            "session", "fresh-client", input_digest(fresh_payload), "fresh-request", payload=fresh_payload,
            receipt={"accepted": True, "disposition": "accepted"}, recovery_of="child-client")
        assert created and successor["recovery_of"] == "child-client"
    finally:
        await reopened.close()


async def test_current_background_return_keeps_its_own_receipt_after_old_human_input_is_retired(tmp_path):
    path = await prior_input(tmp_path, accepted=True)
    with closing(sqlite3.connect(path)) as stored:
        parent = json.loads(stored.execute("SELECT payload FROM client_submissions").fetchone()[0])
        context = decode_context(parent["knowledge"])
        owned = OwnedTaskContext(context, AdmittedRequest(context.origin, context, "prior-work"))
        stored.execute("UPDATE client_submissions SET execution_state='done',execution_owner=request_id,"
                       "execution_generation='original-generation'")
        outcome = {"request_id": "retired-request", "status": "success"}
        stored.execute("INSERT INTO messages(session_id,seq,role,type,content,terminal_request_id) "
                       "VALUES('session',1,'system','result',?,'retired-request')", (json.dumps(outcome),))
        stored.execute("INSERT INTO bg_tasks(id,session_id,command,working_dir,status,started_at,work_id,knowledge_context) "
                       "VALUES('task','session','offline task',?,'completed','original-time','prior-work',?)",
                       (str(tmp_path), encode(owned)))
        payload = {**parent, "content": "Independent result", "source": "bg", "delivery_id": "task", "wiki_mode": "read"}
        stored.execute("INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id,payload,receipt,execution_state) "
                       "VALUES('session','bg:task',?,'return-request',?,?,'pending')",
                       (input_digest(payload), json.dumps(payload), '{"accepted":true,"disposition":"accepted"}'))
        stored.commit()
        original_return = stored.execute("SELECT * FROM client_submissions WHERE client_id='bg:task'").fetchone()
    reopened = Database(str(path))
    await reopened.inspect(previous_writers_retired=True)
    await reopened.initialize(previous_writers_retired=True)
    try:
        assert await reopened.get_client_submission("session", "retired-client") is None
        returned = await reopened.get_source_submission("bg", "task", session_id="session")
        assert returned["request_id"] == "return-request" and returned["receipt"]["accepted"]
        assert decode_owned((await reopened.get_bg_task("task"))["knowledge_context"]) == owned
        assert await reopened.get_committed_terminal("session", "retired-request") == outcome
        async with reopened.transaction(write=False) as tx:
            assert await tx.fetch_one("SELECT * FROM client_submissions WHERE client_id='bg:task'") == original_return
    finally:
        await reopened.close()


@pytest.mark.parametrize("damage", ["unknown-delivery", "missing-result", "wrong-result", "live-writer"])
async def test_done_label_and_writer_exit_alone_do_not_prove_settlement(tmp_path, damage):
    path = await prior_input(tmp_path, accepted=True)
    with closing(sqlite3.connect(path)) as stored:
        stored.execute("UPDATE client_submissions SET execution_state='done',execution_owner=request_id,"
                       "execution_generation='original-generation',execution_boot=?,process_group=?",
                       (boot_identity(), os.getpgrp()))
        if damage == "unknown-delivery":
            stored.execute("UPDATE client_submissions SET delivery_state='unknown'")
        if damage != "missing-result":
            outcome = {"request_id": "another-request" if damage == "wrong-result" else "retired-request",
                       "status": "success"}
            stored.execute("INSERT INTO messages(session_id,seq,role,type,content,terminal_request_id) "
                           "VALUES('session',1,'system','result',?,'retired-request')", (json.dumps(outcome),))
        stored.commit()
    before = path.read_bytes()
    with pytest.raises(UnsupportedMainStorage):
        await Database(str(path)).inspect(previous_writers_retired=damage != "live-writer")
    assert path.read_bytes() == before


@pytest.mark.parametrize(("delivery", "same_generation"), [("confirmed", True), ("unknown", True),
                                                         ("none", True), ("confirmed", False)])
async def test_shared_terminal_requires_this_input_delivery_and_its_exact_execution_owner(tmp_path, delivery, same_generation):
    path = await prior_input(tmp_path, accepted=True)
    with closing(sqlite3.connect(path)) as stored:
        stored.execute("UPDATE client_submissions SET execution_state='done',execution_owner='actual-owner',"
                       "execution_generation='original-generation',delivery_state=?", (delivery,))
        stored.execute("INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id,receipt,"
                       "execution_state,execution_owner,execution_generation) "
                       "VALUES('session','current-owner','current-digest','actual-owner',?, 'done','actual-owner',?)",
                       ('{"accepted":true,"disposition":"accepted"}',
                        "original-generation" if same_generation else "different-generation"))
        outcome = {"request_id": "actual-owner", "status": "success"}
        stored.execute("INSERT INTO messages(session_id,seq,role,type,content,terminal_request_id) "
                       "VALUES('session',1,'system','result',?,'actual-owner')", (json.dumps(outcome),))
        stored.commit()
        current = stored.execute("SELECT * FROM client_submissions WHERE client_id='current-owner'").fetchone()
    before = path.read_bytes()
    database = Database(str(path))
    if delivery != "confirmed" or not same_generation:
        with pytest.raises(UnsupportedMainStorage):
            await database.inspect(previous_writers_retired=True)
        assert path.read_bytes() == before
        return
    await database.inspect(previous_writers_retired=True)
    await database.initialize(previous_writers_retired=True)
    try:
        assert await database.get_client_submission("session", "retired-client") is None
        assert await database.get_committed_terminal("session", "actual-owner") == outcome
        async with database.transaction(write=False) as tx:
            assert await tx.fetch_one("SELECT * FROM client_submissions WHERE client_id='current-owner'") == current
    finally:
        await database.close()


async def test_current_format_cannot_silently_reintroduce_retired_requests(tmp_path):
    path = await prior_input(tmp_path)
    with closing(sqlite3.connect(path)) as stored:
        stored.execute("PRAGMA user_version=4")
        stored.commit()
    before = path.read_bytes()
    with pytest.raises(UnsupportedMainStorage):
        await Database(str(path)).inspect(previous_writers_retired=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize("accepted", [False, True], ids=["refused", "accepted-unclaimed"])
@pytest.mark.parametrize("phase", ["inspect", "transaction-recheck"])
@pytest.mark.parametrize("evidence", ["runtime", "delegation", "delegation-ancestor", "bg-ancestor", "research-ancestor",
                                      "delegation-completion", "wiki-notification", "request-origin", "returned-input"])
async def test_existing_runtime_and_nested_task_facts_disprove_unexecuted_in_both_checks(
    tmp_path, accepted, phase, evidence,
):
    path = await prior_input(tmp_path, accepted=accepted)
    await Database(str(path)).inspect()
    with closing(sqlite3.connect(path)) as keeper:
        keeper.execute("BEGIN")
        keeper.execute("SELECT * FROM client_submissions").fetchall()
        with closing(sqlite3.connect(path)) as stored:
            payload = json.loads(stored.execute("SELECT payload FROM client_submissions").fetchone()[0])
            context = decode_context(payload["knowledge"])
            if evidence == "runtime":
                metadata = RuntimeMetadata(request_id="retired-request", attempt_id="actual-attempt", tool_call_id="actual-tool")
                stored.execute("INSERT INTO messages(session_id,seq,role,type,runtime_metadata) "
                               "VALUES('session',1,'assistant','tool_use',?)", (metadata.model_dump_json(),))
            else:
                stored.execute("INSERT INTO sessions(id,name,working_dir,created_at,user_id,agent_id,origin,parent_session_id) "
                               "SELECT 'delegated-child','Child',working_dir,created_at,user_id,agent_id,'delegation',id "
                               "FROM sessions WHERE id='session'")
                reference = (replace(context.origin, session_id="delegated-child", request_id="child-request")
                             if evidence.endswith("ancestor") else context.origin)
                owned = OwnedTaskContext(context, AdmittedRequest(reference, context, "prior-work"))
                if evidence == "delegation-completion":
                    upstream = replace(context, origin=replace(context.origin, session_id="delegated-child", request_id="upstream-request"))
                    owned = OwnedTaskContext(upstream, AdmittedRequest(upstream.origin, upstream, "upstream-work"))
                    stored.execute("UPDATE sessions SET origin='user',parent_session_id=NULL WHERE id='delegated-child'")
                    stored.execute("UPDATE sessions SET origin='delegation',parent_session_id='delegated-child' WHERE id='session'")
                if evidence.startswith("delegation"):
                    record = _PersistedRound(
                        work_id="delegated-work", parent_work_id="prior-work", target_agent_name="Current agent",
                        request="Current delegated task", created_at="original-time",
                        state="running" if evidence == "delegation-completion" else "completed",
                        finished_at=None if evidence == "delegation-completion" else "finished-time",
                        error=None, awaiting_sub_relay=False,
                        deferred_request_id="retired-request" if evidence == "delegation-completion" else None,
                        terminal_disposition=None,
                        knowledge_context=encode(owned),
                    )
                    stored.execute("UPDATE sessions SET delegation_state=? WHERE id=?",
                                   (record.model_dump_json(), "session" if evidence == "delegation-completion" else "delegated-child"))
                elif evidence == "wiki-notification":
                    notification_fact(stored, owner=context.origin.user_id, target="session")
                elif evidence == "request-origin":
                    tool_input = {"request_context": {"request_id": "child-request", "knowledge": json.loads(encode(context))}}
                    stored.execute("INSERT INTO messages(session_id,seq,role,type,tool_input) "
                                   "VALUES('delegated-child',1,'user','text',?)", (json.dumps(tool_input),))
                elif evidence == "returned-input":
                    returned = {**payload, "wiki_mode": "read", "source": "bg", "delivery_id": "nested-task"}
                    stored.execute("INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id,payload,receipt) "
                                   "VALUES('delegated-child','bg:nested-task',?,'returned-request',?,?)",
                                   (input_digest(returned), json.dumps(returned), '{"accepted":true,"disposition":"accepted"}'))
                elif evidence == "bg-ancestor":
                    stored.execute("INSERT INTO bg_tasks(id,session_id,command,working_dir,status,started_at,knowledge_context) "
                                   "VALUES('nested-task','delegated-child','offline task',?,'completed','original-time',?)",
                                   (str(tmp_path), encode(owned)))
                else:
                    stored.execute("INSERT INTO research_jobs(id,session_id,question,status,created_at,knowledge_context) "
                                   "VALUES('nested-research','delegated-child','Current research','completed','original-time',?)",
                                   (encode(owned),))
            stored.commit()
        wal = Path(str(path) + "-wal")
        before, before_wal = path.read_bytes(), wal.read_bytes()
        assert len(before_wal) > 32
        if phase == "inspect":
            with pytest.raises(UnsupportedMainStorage):
                await Database(str(path)).inspect()
        else:
            async with aiosqlite.connect(path) as writer:
                statements = []
                await writer.set_trace_callback(statements.append)
                tx = Transaction(writer, ServiceHealth(), asyncio.get_running_loop().time() + 5)
                await tx.execute("BEGIN IMMEDIATE")
                try:
                    with pytest.raises(UnsupportedMainStorage):
                        await prepare_main_storage(tx)
                finally:
                    await tx.finish()
                    await writer.rollback()
                assert not any(sql.lstrip().upper().startswith(("DELETE", "PRAGMA USER_VERSION=")) for sql in statements)
        assert path.read_bytes() == before and wal.read_bytes() == before_wal


@pytest.mark.parametrize("accepted", [False, True], ids=["refused", "accepted-unclaimed"])
async def test_same_request_id_in_other_session_does_not_block_retirement(tmp_path, accepted):
    path = await prior_input(tmp_path, accepted=accepted)
    with closing(sqlite3.connect(path)) as stored:
        payload = json.loads(stored.execute("SELECT payload FROM client_submissions").fetchone()[0])
        old = decode_context(payload["knowledge"])
        context = replace(old, origin=replace(old.origin, session_id="other-session"))
        stored.execute("INSERT INTO sessions(id,name,working_dir,created_at,user_id,agent_id) "
                       "SELECT 'other-session','Other',working_dir,created_at,user_id,agent_id FROM sessions WHERE id='session'")
        metadata = RuntimeMetadata(request_id="retired-request", attempt_id="other-attempt", tool_call_id="other-tool")
        stored.execute("INSERT INTO messages(session_id,seq,role,type,runtime_metadata) "
                       "VALUES('other-session',1,'assistant','tool_use',?)", (metadata.model_dump_json(),))
        owned = OwnedTaskContext(context, AdmittedRequest(context.origin, context, "other-work"))
        stored.execute("INSERT INTO bg_tasks(id,session_id,command,working_dir,status,started_at,knowledge_context) "
                       "VALUES('other-task','other-session','offline task',?,'completed','original-time',?)",
                       (str(tmp_path), encode(owned)))
        notification_fact(stored, owner=context.origin.user_id, target="other-session")
        stored.commit()
        facts = {table: stored.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                 for table in ("sessions", "messages", "bg_tasks", "wiki_job_notifications")}
    database = Database(str(path))
    await database.inspect()
    await database.initialize()
    try:
        assert await database.get_client_submission("session", "retired-client") is None
        async with database.transaction(write=False) as tx:
            for table, rows in facts.items():
                assert await tx.fetch_all(f"SELECT * FROM {table} ORDER BY rowid") == rows
    finally:
        await database.close()


@pytest.mark.parametrize("entry", ["database", "service"])
async def test_unsettled_old_input_in_real_wal_refuses_both_entries_before_keys_or_writes(tmp_path, monkeypatch, entry):
    config, configuration = installation_settings(tmp_path)
    for name, value in json.loads(configuration.read_text()).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "master_key", "retired-request-fixture-master")
    path = await prior_input(tmp_path)
    proof = tmp_path / "admitted"
    with closing(sqlite3.connect(path)) as keeper:
        keeper.execute("BEGIN")
        keeper.execute("SELECT * FROM client_submissions").fetchall()
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("UPDATE client_submissions SET delivery_state='unknown'")
            writer.commit()
        wal = Path(str(path) + "-wal")
        before, before_wal = path.read_bytes(), wal.read_bytes()
        assert len(before_wal) > 32
        result = subprocess.run([sys.executable, "-c", """
import asyncio,json,sys
from pathlib import Path
from server.config import settings
for name,value in json.loads(Path(sys.argv[1]).read_text()).items():setattr(settings,name,value)
settings.master_key=''
async def main():
    if sys.argv[2]=='database':
        from server.database import Database
        await Database(settings.db_path).initialize()
        Path(sys.argv[3]).touch()
    else:
        from server.main import app
        async with app.router.lifespan_context(app):Path(sys.argv[3]).touch()
asyncio.run(main())
""", str(configuration), entry, str(proof)], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=15,
            env=dict(os.environ, OCTOPUS_ENV_FILE=str(tmp_path / "no-project.env"),
                     CODEX_HOME=str(tmp_path / "codex"), CLAUDE_CONFIG_DIR=str(tmp_path / "claude")))
        assert result.returncode == 70, result.stdout + result.stderr
        assert not proof.exists() and not Path(config.master_key_file).exists()
        assert path.read_bytes() == before and wal.read_bytes() == before_wal


@pytest.mark.parametrize("version", [0, 1, 2, 3])
@pytest.mark.parametrize("phase", ["after-delete", "before-version", "commit-ack"])
async def test_retirement_and_format_commit_are_atomic_across_real_process_loss(tmp_path, version, phase):
    path = await prior_input(tmp_path)
    if version == 0:
        write_unversioned_baseline(path)
    elif version == 1:
        write_version_one_baseline(path)
    else:
        with closing(sqlite3.connect(path)) as stored:
            stored.execute(f"PRAGMA user_version={version}")
            stored.commit()
    proof = tmp_path / "fault-reached"
    result = subprocess.run([sys.executable, "-c", """
import asyncio,os,sys
from pathlib import Path
import aiosqlite
from aiosqlite.context import contextmanager
from server.database import Database
connect=aiosqlite.Connection._connect
phase=sys.argv[2]
async def fault_connection(connection):
    await connect(connection)
    execute,commit=connection.execute,connection.commit
    def stop():
        Path(sys.argv[3]).write_text(phase)
        os._exit(71)
    @contextmanager
    async def fault_execute(sql,parameters=()):
        if phase=='before-version' and sql.strip().upper().startswith('PRAGMA USER_VERSION='):stop()
        cursor=await execute(sql,parameters)
        if phase=='after-delete' and sql.strip().upper().startswith('DELETE FROM CLIENT_SUBMISSIONS'):stop()
        return cursor
    async def fault_commit():
        await commit()
        if phase=='commit-ack':
            Path(sys.argv[3]).write_text(phase)
            raise OSError('Actual commit succeeded; acknowledgment unavailable')
    connection.execute,connection.commit=fault_execute,fault_commit
    return connection
aiosqlite.Connection._connect=fault_connection
asyncio.run(Database(sys.argv[1]).initialize())
""", str(path), phase, str(proof)], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=15,
        env=dict(os.environ, OCTOPUS_ENV_FILE=str(tmp_path / "no-project.env")))
    assert result.returncode == (70 if phase == "commit-ack" else 71), result.stdout + result.stderr
    assert proof.read_text() == phase
    with closing(sqlite3.connect(path)) as stored:
        committed = phase == "commit-ack"
        assert stored.execute("PRAGMA user_version").fetchone() == (4 if committed else version,)
        assert stored.execute("SELECT count(*) FROM client_submissions").fetchone() == (0 if committed else 1,)
        assert bool(stored.execute("SELECT 1 FROM sqlite_schema WHERE name='schema_migrations'").fetchone()) == (
            version == 0 and not committed)
    for _ in range(2):
        reopened = Database(str(path))
        await reopened.initialize()
        try:
            assert await reopened.get_client_submission("session", "retired-client") is None
        finally:
            await reopened.close()


async def test_previously_refused_off_record_is_retired_without_removing_current_input_or_messages(tmp_path):
    path = tmp_path / "main.db"
    database = Database(str(path))
    await database.initialize()
    try:
        owner, _ = await create_account(database, extra_roots=[tmp_path])
        agent = await database.get_system_agent(owner["id"])
        await database.save_session("session", "Current session", str(tmp_path), "2026-10-24T00:00:00Z",
                                    agent_id=agent["id"], user_id=owner["id"])
        context = admit(session_id="session", request_id="current-request", user_id=owner["id"],
                        prompt="Current read-only input", selected="read")
        current_payload = {
            "content": context.trusted_request, "attachment_ids": [], "attachments": [], "wiki_mode": "read",
            "knowledge": encode(context), "trusted_request": context.trusted_request, "source": "human",
            "delivery_id": None, "work_id": "current-work", "order": 1,
            "question_session_id": None, "question_id": None, "priority": False,
        }
        await database.admit_client_submission(
            "session", "current-client", input_digest(current_payload), "current-request",
            payload=current_payload, receipt={"accepted": True, "disposition": "accepted"},
        )
        await database.append_message("session", 1, "user", "text", "Visible current conversation")
        current = await database.get_client_submission("session", "current-client")
        async with database.transaction(write=False) as tx:
            messages = await tx.fetch_all("SELECT * FROM messages ORDER BY id")
    finally:
        await database.close()

    # Literal prior-format input: rejected before execution, with no writer,
    # delivery, recovery successor or source-return consumer to settle.
    retired_payload = {"content": "Retired request", "attachment_ids": [], "wiki_mode": "off", "source": "human"}
    with closing(sqlite3.connect(path)) as stored:
        stored.execute("PRAGMA user_version=3")
        stored.execute(
            "INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id,payload,receipt,"
            "execution_state,reason_code) VALUES(?,?,?,?,?,?,'withdrawn','legacy_rejected')",
            ("session", "retired-client", input_digest(retired_payload), "retired-request",
             json.dumps(retired_payload), '{"accepted":false,"disposition":"rejected"}'),
        )
        stored.commit()

    for _ in range(2):
        reopened = Database(str(path))
        await reopened.initialize()
        try:
            assert await reopened.get_client_submission("session", "retired-client") is None
            assert await reopened.get_client_submission("session", "current-client") == current
            async with reopened.transaction(write=False) as tx:
                assert await tx.fetch_all("SELECT * FROM messages ORDER BY id") == messages
        finally:
            await reopened.close()

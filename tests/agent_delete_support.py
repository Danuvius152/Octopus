"""HTTP and process probes for the original Agent deletion owner."""

import json
import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server import agent_memory
from server.agent_manager import AgentManager
from server.config import settings
from server.database import Database
from server.deps import get_session_manager, set_user_manager
from server.routers import agents as agents_router
from server.session_manager import SessionManager
from server.users import UserManager


@asynccontextmanager
async def agent_http(database, token):
    sessions = SessionManager()
    await sessions.initialize(database)
    set_user_manager(UserManager(database))
    agents_router.set_manager(AgentManager(database))
    app = FastAPI()
    app.include_router(agents_router.router)
    app.dependency_overrides[get_session_manager] = lambda: sessions
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as client:
        yield client, sessions


async def agent_delete_probe(config_file, proof_file, fault):
    """Run in an isolated process with production fail-stop and real commits."""
    for name, value in json.loads(config_file.read_text()).items():
        setattr(settings, name, value)
    proof = json.loads(proof_file.read_text())
    agent_id = proof["agent_id"]
    db = Database(settings.db_path)
    await db.initialize()
    async with agent_http(db, proof["token"]) as (client, sessions):
        async def observe(event):
            if event["type"] == "agent_deleted":
                proof_file.with_suffix(".event").touch()

        sessions.on_broadcast("observe-delete-probe", observe)
        delete = db.delete_agent
        commit = db.conn.commit
        execute = db.conn.execute
        memory = agent_memory.agent_state_dir(agent_id, proof["owner"])

        async def lost_commit():
            if fault == "commit-after":
                await commit()
            proof_file.with_suffix(".lost-commit").touch()
            raise OSError("Final sqlite commit acknowledgment was lost")

        async def refuse_delete(sql, parameters=()):
            cursor = await execute(sql, parameters)
            if sql.startswith("DELETE FROM agents"):
                await cursor.close()
                raise sqlite3.IntegrityError("Injected definite pre-commit refusal")
            return cursor

        async def fail_final(identifier, *, user_id):
            assert identifier == agent_id and user_id == proof["owner"] and not memory.exists()
            proof_file.with_suffix(".files-cleared").touch()
            if fault == "pre-final":
                os._exit(71)
            if fault == "rollback":
                db.conn.execute = refuse_delete
            else:
                db.conn.commit = lost_commit
            await delete(identifier, user_id=user_id)

        if fault.startswith("retirement-"):
            move = agent_memory.directory_cleanup.move_unoccupied
            remove = os.rmdir

            def exit_retirement():
                proof_file.with_suffix(".files-cleared").touch()
                os._exit(72)

            def exit_after_move(*args):
                moving_target = args[-1] == "directory"
                if moving_target and fault == "retirement-planned":
                    exit_retirement()
                if moving_target and fault in {"retirement-replaced", "retirement-replaced-before"}:
                    memory.rename(memory.with_name("moved-original"))
                    memory.mkdir()
                    proof_file.with_suffix(".replacement").write_text(str(memory.stat().st_ino))
                    if fault == "retirement-replaced-before":
                        exit_retirement()
                move(*args)
                if fault in {"retirement-unlinked-proof", "retirement-removed"}:
                    return
                boundary = args[-1].startswith("verified-") if fault == "retirement-verified" else moving_target
                if boundary:
                    exit_retirement()

            def exit_at_rmdir(name, **kwargs):
                if name == "directory" and fault == "retirement-unlinked-proof":
                    exit_retirement()
                remove(name, **kwargs)
                if name == "directory":
                    exit_retirement()

            agent_memory.directory_cleanup.move_unoccupied = exit_after_move
            if fault in {"retirement-unlinked-proof", "retirement-removed"}:
                os.rmdir = exit_at_rmdir
        db.delete_agent = fail_final
        response = await client.delete(f"/api/agents/{agent_id}")
        proof_file.with_suffix(".response").write_text(str(response.status_code))
    await db.close()

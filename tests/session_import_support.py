"""HTTP fixture and process probe for session import settlement."""

from server.database import Database
from server.deps import set_user_manager
from server.session_manager import SessionManager
from server.users import UserManager
from tests.session_api import session_client


async def lose_import_commit(path, token, transcript, proof, committed):
    db = Database(str(path))
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    set_user_manager(UserManager(db))
    install, commit = db.install_session_history, db.conn.commit

    async def lose_acknowledgment():
        assert db.conn.in_transaction and not manager.sessions
        if committed:
            await commit()
        proof.touch()
        raise OSError("test import commit acknowledgment was lost")

    async def fault_import(record, messages):
        db.conn.commit = lose_acknowledgment
        return await install(record, messages)

    db.install_session_history = fault_import
    async with session_client(manager, bearer=token) as client:
        response = await client.post("/api/sessions/import", json=transcript)
        proof.with_suffix(".response").write_text(str(response.status_code))
    await db.close()

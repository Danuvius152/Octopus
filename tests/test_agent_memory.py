"""Per-agent native-memory provisioning (docs/implemented/memory.md, Verification).

Pure path derivation + idempotent provisioning of the one canonical per-agent
memory dir. Memory is decoupled from both harnesses' config/auth dirs, so there
is no symlink/auth machinery to test here; the real read/write cycle is covered
by the gated real-CLI test.
"""

from __future__ import annotations

import pytest

from server import agent_memory
from server.config import settings


@pytest.fixture
def agents_root(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    monkeypatch.setattr(settings, "agents_dir", str(root))
    return root


def test_path_derivation(agents_root):
    assert agent_memory.agent_state_dir("a1") == agents_root / "a1"
    assert agent_memory.agent_memory_dir("a1") == agents_root / "a1" / "memory"


def test_ensure_and_remove_agent_dirs(agents_root):
    agent_memory.ensure_agent_dirs("a")
    assert agent_memory.agent_memory_dir("a").is_dir()

    agent_memory.remove_agent_dir("a")
    assert not agent_memory.agent_state_dir("a").exists()


def test_ensure_agent_dirs_is_idempotent(agents_root):
    agent_memory.ensure_agent_dirs("a")
    # Drop a file in, re-ensure, confirm it survives (no clobber).
    fact = agent_memory.agent_memory_dir("a") / "MEMORY.md"
    fact.write_text("- [x](x.md) — y")
    agent_memory.ensure_agent_dirs("a")
    assert fact.read_text() == "- [x](x.md) — y"


def test_uses_configured_agents_dir(tmp_path, monkeypatch):
    """`~` / settings override is honored at call time."""
    monkeypatch.setattr(settings, "agents_dir", "~/.octopus/agents")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert agent_memory.agent_memory_dir("z") == tmp_path / ".octopus/agents/z/memory"


@pytest.mark.asyncio
async def test_agent_manager_provisions_and_cleans_dirs(agents_root, tmp_path):
    """create_agent provisions the per-agent memory dir; hard delete removes it."""
    from server.agent_manager import AgentManager
    from server.database import Database
    from tests.accounts import create_account

    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    owner, _ = await create_account(db)
    try:
        mgr = AgentManager(db)
        agent = await mgr.create_agent(name="Mem Agent", user_id=owner["id"])
        aid = agent["id"]
        assert agent_memory.agent_memory_dir(aid, owner["id"]).is_dir()

        await mgr.delete_agent(aid, user_id=owner["id"])
        assert not agent_memory.agent_state_dir(aid, owner["id"]).exists()
    finally:
        await db.close()


async def test_unarchive_and_delete_keep_one_owner_of_agent_memory(agents_root, tmp_path, monkeypatch):
    import asyncio

    from server.agent_manager import AgentError, AgentManager
    from server.database import Database
    from server.session_manager import SessionManager
    from tests.accounts import create_account

    database = Database(str(tmp_path / "memory-operations.db"))
    await database.initialize()
    owner, _ = await create_account(database)
    agents = AgentManager(database)
    agent = await agents.create_agent(name="Restore and delete", user_id=owner["id"])
    memory = agent_memory.agent_memory_dir(agent["id"], owner["id"])
    original = memory / "MEMORY.md"
    original.write_bytes(b"Original account memory\n")
    await agents.archive_agent(agent["id"])
    sessions = SessionManager()
    await sessions.initialize(database)
    entered, release = asyncio.Event(), asyncio.Event()
    unarchive = database.unarchive_agent

    async def blocked(identifier):
        entered.set()
        await release.wait()
        await unarchive(identifier)

    monkeypatch.setattr(database, "unarchive_agent", blocked)
    restoring = asyncio.create_task(sessions.unarchive_agent(agent["id"], owner["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(AgentError, match="busy"):
            await agents.delete_agent(agent["id"], user_id=owner["id"])
        assert (await database.get_agent(agent["id"]))["archived"]
        assert original.read_bytes() == b"Original account memory\n"
        release.set()
        restored = await asyncio.wait_for(restoring, 2)
        assert not restored["archived"]
        assert original.read_bytes() == b"Original account memory\n"
        await agents.delete_agent(agent["id"], user_id=owner["id"])
        assert await database.get_agent(agent["id"]) is None
        assert not memory.parent.exists()
    finally:
        release.set()
        await asyncio.gather(restoring, return_exceptions=True)
        await sessions.close_session_owners()
        await database.close()


async def test_owned_memory_and_explicit_roots_survive_repeated_database_reopen(agents_root, tmp_path):
    import json

    from server.agent_manager import AgentManager
    from server.crypto import decrypt, encrypt
    from server.database import Database
    from server.users import UserManager
    from tests.accounts import create_account

    approved = tmp_path / "approved-project"
    approved.mkdir()
    path = str(tmp_path / "owned.db")
    db = Database(path)
    await db.initialize()
    owner, _ = await create_account(db, extra_roots=[approved])
    manager = AgentManager(db)
    agent = await manager.create_agent(name="Saved memory", user_id=owner["id"], mcp_servers=[])
    await db.update_agent(agent["id"], archived=True, system_prompt="Keep my current instructions")
    memory = agent_memory.agent_memory_dir(agent["id"], owner["id"])
    (memory / "empty").mkdir()
    (memory / "nested").mkdir()
    files = {"MEMORY.md": b"Original owned index\n", "nested/proof.md": b"Original evidence\x00\xff\n"}
    for name, content in files.items():
        (memory / name).write_bytes(content)
    key = UserManager(db).data_key(owner)
    ciphertext = encrypt("owned-api-credential", key)
    await db.save_credential("owned-secret", "codex", "Owned API", "api_key", ciphertext, "now", user_id=owner["id"])
    original_agents = await db.load_agents(include_archived=True, user_id=owner["id"])
    original_user = await db.get_user(owner["id"])
    await db.close()
    try:
        for _ in range(2):
            db = Database(path)
            await db.initialize()
            assert await db.get_user(owner["id"]) == original_user
            assert json.loads(original_user["extra_roots"]) == [str(approved)]
            assert await db.load_agents(include_archived=True, user_id=owner["id"]) == original_agents
            assert agent_memory.agent_memory_dir(agent["id"], owner["id"]) == memory
            assert {name: (memory / name).read_bytes() for name in files} == files
            assert (memory / "empty").is_dir()
            credential = await db.get_credential("owned-secret", owner["id"])
            assert credential["secret_encrypted"] == ciphertext
            assert decrypt(ciphertext, UserManager(db).data_key(await db.get_user(owner["id"]))) == "owned-api-credential"
            assert not agents_root.exists(), "Owned provisioning or restart created the legacy namespace"
            await db.close()
    finally:
        await db.close()

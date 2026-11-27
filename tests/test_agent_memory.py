"""Per-agent native-memory provisioning (docs/implemented/memory.md, Verification).

Pure path derivation + idempotent provisioning of the one canonical per-agent
memory dir. Memory is decoupled from both harnesses' config/auth dirs, so there
is no symlink/auth machinery to test here; the real read/write cycle is covered
by the gated real-CLI test.
"""

from __future__ import annotations

import os

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


@pytest.mark.parametrize("component", ["account", "agents", "agent"])
def test_private_agent_removal_rejects_links_in_its_address(tmp_path, monkeypatch, component):
    users = tmp_path / "users"
    monkeypatch.setattr(settings, "users_root", str(users))
    target = users / "original" / "agents" / "a"
    blocked = {"account": target.parents[1], "agents": target.parent, "agent": target}[component]
    blocked.parent.mkdir(parents=True)
    external = tmp_path / "keep"
    original = external / target.relative_to(blocked) / "memory" / "keep.md"
    original.parent.mkdir(parents=True)
    original.write_text("Keep the unrelated files\n")
    blocked.symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError):
        agent_memory.remove_agent_dir("a", "original")
    assert original.read_text() == "Keep the unrelated files\n"


def test_private_agent_removal_accepts_the_configured_root_alias(tmp_path, monkeypatch):
    users = tmp_path / "users"
    users.mkdir()
    configured = tmp_path / "configured-users"
    configured.symlink_to(users, target_is_directory=True)
    monkeypatch.setattr(settings, "users_root", str(configured))
    agent_memory.ensure_agent_dirs("a", "original")
    agent_memory.remove_agent_dir("a", "original")
    assert not (users / "original" / "agents" / "a").exists()


@pytest.mark.parametrize("component", ["account", "agents", "agent", "memory"])
def test_private_agent_removal_refuses_to_cross_mounts(tmp_path, monkeypatch, component):
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    agent_memory.ensure_agent_dirs("a", "original")
    memory = agent_memory.agent_memory_dir("a", "original")
    original = memory / "keep.md"
    original.write_text("Keep files behind the mount\n")
    paths = {"memory": memory, "agent": memory.parent, "agents": memory.parents[1], "account": memory.parents[2]}
    binding = paths[component].stat()
    mount_id = agent_memory.directory_cleanup.mount_id

    def replacement_mount(descriptor):
        return "unexpected-bind-mount" if os.path.samestat(os.fstat(descriptor), binding) else mount_id(descriptor)

    monkeypatch.setattr(agent_memory.directory_cleanup, "mount_id", replacement_mount)
    with pytest.raises(OSError, match="mount"):
        agent_memory.remove_agent_dir("a", "original")
    assert original.read_text() == "Keep files behind the mount\n"


@pytest.mark.parametrize("component", ["agents", "agent"])
def test_private_agent_removal_detects_directory_replacement(tmp_path, monkeypatch, component):
    users = tmp_path / "users"
    monkeypatch.setattr(settings, "users_root", str(users))
    agent_memory.ensure_agent_dirs("a", "original")
    target = agent_memory.agent_state_dir("a", "original")
    blocked = {"agents": target.parent, "agent": target}[component]
    retired = tmp_path / "original-directory"
    remove = agent_memory.directory_cleanup.empty_directory
    replace = True

    def swap_then_remove(*args, **kwargs):
        nonlocal replace
        if replace:
            replace = False
            blocked.rename(retired)
            decoy = blocked / target.relative_to(blocked) / "memory" / "keep.md"
            decoy.parent.mkdir(parents=True)
            decoy.write_text("Keep the replacement files\n")
        return remove(*args, **kwargs)

    monkeypatch.setattr(agent_memory.directory_cleanup, "empty_directory", swap_then_remove)
    with pytest.raises(OSError, match="changed"):
        agent_memory.remove_agent_dir("a", "original")
    assert (target / "memory" / "keep.md").read_text() == "Keep the replacement files\n"


def test_private_agent_removal_preserves_final_empty_replacement(agents_root, monkeypatch):
    agent_memory.ensure_agent_dirs("a")
    target = agent_memory.agent_state_dir("a")
    saved = agents_root / "moved-original"
    move = agent_memory.directory_cleanup.move_unoccupied
    switched = None

    def replace_at_final_move(source_parent, source, dest_parent, dest):
        nonlocal switched
        if source == "a":
            target.rename(saved)
            target.mkdir()
            switched = target.stat()
        return move(source_parent, source, dest_parent, dest)

    monkeypatch.setattr(agent_memory.directory_cleanup, "move_unoccupied", replace_at_final_move)
    with pytest.raises(OSError, match="original directory proof"):
        agent_memory.remove_agent_dir("a")
    slot = agents_root / agent_memory.retirement_name("a")
    preserved = slot / "directory"
    assert switched is not None and os.path.samestat(switched, preserved.stat())
    assert not target.exists()
    assert saved.is_dir()
    with pytest.raises(OSError, match="original directory proof"):
        agent_memory.remove_agent_dir("a")
    assert os.path.samestat(switched, preserved.stat())


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
        return await unarchive(identifier)

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

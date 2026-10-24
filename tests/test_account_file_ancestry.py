"""Deleting an ancestor does not invalidate a fork's owned attachment copy."""

import pytest

from server.users import UserManager
from server.workspace import paths_for
from tests.test_account_file_layout import managed_files as managed_files
from tests.test_service_layout import account_installation as account_installation


@pytest.fixture
async def ancestry(managed_files):
    _config, account, _profile, parent, client, manager = managed_files
    original = b"Exact bytes survive deleted and intermediate fork ancestors"
    uploaded = await client.post(f"/api/sessions/{parent.id}/attachments",
                                 files={"file": ("ancestry.txt", original, "text/plain")})
    assert uploaded.status_code == 201
    metadata = uploaded.json()
    await manager.db.append_message(parent.id, 0, "user", "text", "Original attachment", attachments=[metadata])
    for child, source in (("middlefork01", parent.id), ("lastfork0001", "middlefork01")):
        await manager.db.create_fork_session(
            fork_id=child, name=child, working_dir=parent.working_dir, created_at=parent.created_at,
            parent_id=source, backend=parent.backend, user_id=parent.user_id, agent_id=parent.agent_id,
            credential_id=None, resume_id=None, fork_after_seq=0,
        )
        await manager.db.update_session_field(child, fork_status="ready", archived=True)
    assert await manager.delete_session(parent.id)
    assert await manager.db.get_session_owner(parent.id) is None
    for child in ("middlefork01", "lastfork0001"):
        assert (account / "attachments" / child / f"{metadata['id']}__ancestry.txt").read_bytes() == original
    middle = await manager.unarchive_session("middlefork01", parent.user_id)
    return parent, middle, client, manager, account, metadata, original


async def test_deleting_an_intermediate_fork_uses_the_descendants_own_copy(ancestry):
    parent, middle, client, manager, account, metadata, original = ancestry
    messages = await manager.db.load_messages("lastfork0001")
    assert await manager.delete_session(middle.id)
    assert await manager.db.get_session_owner(middle.id) is None
    assert await manager.db.load_messages("lastfork0001") == messages
    downloaded = await client.get(f"/api/sessions/lastfork0001/attachments/{metadata['id']}")
    assert downloaded.status_code == 200 and downloaded.content == original
    for session_id in (parent.id, middle.id, "lastfork0001"):
        assert (account / "attachments" / session_id / f"{metadata['id']}__ancestry.txt").read_bytes() == original


async def test_a_new_fork_reads_the_nearest_owned_copy_before_a_deleted_ancestor(ancestry, monkeypatch):
    _parent, middle, client, manager, account, metadata, original = ancestry
    monkeypatch.setattr(manager, "_fork_copy_base", lambda: str(account / "forks"))
    duplicate = await client.post(f"/api/sessions/{middle.id}/duplicate", json={})
    assert duplicate.status_code == 201, duplicate.text
    child = duplicate.json()["id"]
    assert not (account / "attachments" / child).exists()
    downloaded = await client.get(f"/api/sessions/{child}/attachments/{metadata['id']}")
    assert downloaded.status_code == 200 and downloaded.content == original
    assert not (account / "attachments" / child).exists()


async def test_a_foreign_ancestor_never_authorizes_an_attachment_source(managed_files):
    _config, account, _profile, session, client, manager = managed_files
    users = UserManager(manager.db)
    foreign = await users.create_user(username="foreign-files", password="owned-foreign-password")
    agent = await manager.db.get_system_agent(foreign["id"])
    parent = await manager.create_session(agent["id"], name="Foreign parent")
    headers = {"Authorization": "Bearer " + await users.issue_token(foreign["id"])}
    uploaded = await client.post(f"/api/sessions/{parent.id}/attachments", headers=headers,
                                 files={"file": ("private.txt", b"Other account bytes", "text/plain")})
    assert uploaded.status_code == 201
    metadata = uploaded.json()
    # A retained contradictory relation is not new file-reading authority.
    await manager.db.update_session_field(session.id, forked_from_session_id=parent.id)
    forbidden = await client.get(f"/api/sessions/{session.id}/attachments/{metadata['id']}")
    assert forbidden.status_code == 404
    assert not (account / "attachments" / session.id).exists()
    source = paths_for(foreign["id"]).attachments / parent.id / f"{metadata['id']}__private.txt"
    assert source.read_bytes() == b"Other account bytes"

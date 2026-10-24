"""A published descendant file must be durable before its parent row is removed."""

import os

import pytest

from server import attachments
from tests.test_account_file_layout import managed_files as managed_files
from tests.test_service_layout import account_installation as account_installation


async def test_parent_delete_retry_confirms_an_already_published_copy(managed_files, monkeypatch):
    _config, account, _profile, parent, client, manager = managed_files
    original = b"Retried preservation must retain these complete original bytes"
    uploaded = await client.post(f"/api/sessions/{parent.id}/attachments",
                                 files={"file": ("durable.txt", original, "text/plain")})
    assert uploaded.status_code == 201
    metadata = uploaded.json()
    await manager.db.append_message(parent.id, 0, "user", "text", "Original attachment", attachments=[metadata])
    child = "durablefork01"
    await manager.db.create_fork_session(
        fork_id=child, name=child, working_dir=parent.working_dir, created_at=parent.created_at,
        parent_id=parent.id, backend=parent.backend, user_id=parent.user_id, agent_id=parent.agent_id,
        credential_id=None, resume_id=None, fork_after_seq=0,
    )
    await manager.db.update_session_field(child, fork_status="ready", archived=True)
    root = account / "attachments"
    directory = root / child
    source = root / parent.id / f"{metadata['id']}__durable.txt"
    destination = directory / source.name
    sync = attachments.os.fsync
    failed = False
    observations = []

    def confirm(descriptor):
        nonlocal failed
        observed = os.fstat(descriptor)
        if directory.exists() and os.path.samestat(observed, directory.stat()):
            assert destination.read_bytes() == original, "The link must already have been published"
            if not failed:
                failed = True
                observations.append("directory-failed")
                raise OSError("descendant directory fsync unavailable")
            sync(descriptor)
            observations.append("directory-confirmed")
        elif destination.exists() and os.path.samestat(observed, destination.stat()):
            sync(descriptor)
            observations.append("file-confirmed")
        elif os.path.samestat(observed, root.stat()):
            sync(descriptor)
            observations.append("root-confirmed")
        else:
            sync(descriptor)

    delete = manager.db.delete_session

    async def deleting(session_id):
        observations.append("delete-row")
        await delete(session_id)

    monkeypatch.setattr(attachments.os, "fsync", confirm)
    monkeypatch.setattr(manager.db, "delete_session", deleting)
    with pytest.raises(OSError, match="descendant directory fsync unavailable"):
        await manager.delete_session(parent.id)
    assert observations == ["directory-failed"]
    assert manager.get_session(parent.id, parent.user_id) is parent
    assert await manager.db.get_session_owner(parent.id) == parent.user_id
    assert source.read_bytes() == destination.read_bytes() == original
    assert await manager.delete_session(parent.id)
    assert observations == ["directory-failed", "file-confirmed", "directory-confirmed", "root-confirmed", "delete-row"], \
        "Retry must confirm the existing file and directory before deleting the parent row"
    assert await manager.db.get_session_owner(parent.id) is None
    assert source.read_bytes() == destination.read_bytes() == original
    downloaded = await client.get(f"/api/sessions/{child}/attachments/{metadata['id']}")
    assert downloaded.status_code == 200 and downloaded.content == original

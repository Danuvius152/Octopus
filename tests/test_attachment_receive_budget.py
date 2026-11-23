"""Upload admission, receive budgets and parser-owned temporary files."""

import asyncio
import tempfile
import threading
from datetime import UTC, datetime

import pytest
from starlette import formparsers
from starlette.datastructures import UploadFile

from server import deps
from server.attachments import MAX_FILE_BYTES, get_path
from server.main import app
from server.session_manager import session_manager
from server.sessions import SessionManager
from server.workspace import paths_for
from tests.attachment_runtime import account_user_id as account_user_id
from tests.attachment_runtime import client as client
from tests.attachment_runtime import users_root as users_root

BODY_LIMIT = 26 * 1024 * 1024
HEADERS = {"Content-Type": "multipart/form-data; boundary=upload-boundary"}
PREFIX = b'--upload-boundary\r\nContent-Disposition: form-data; name="file"; filename="input.bin"\r\n\r\n'
SUFFIX = b"\r\n--upload-boundary--\r\n"


@pytest.fixture
async def upload_url(client, account_user_id):
    agent = await session_manager.db.get_system_agent(account_user_id)
    response = await client.post("/api/sessions", json={"agent_id": agent["id"], "name": "Upload receive"})
    assert response.status_code == 201
    return f"/api/sessions/{response.json()['id']}/attachments"


@pytest.fixture
def temporary_files(monkeypatch):
    opened = []
    create = formparsers.SpooledTemporaryFile

    def tracked(*args, **kwargs):
        file = create(*args, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracked)
    return opened


@pytest.mark.parametrize("rejection", ["unauthorized", "session"])
async def test_admission_rejects_before_receiving_or_parsing(client, upload_url, temporary_files, rejection):
    consumed = []

    async def body():
        consumed.append(True)
        yield PREFIX + b"file bytes" + SUFFIX

    headers = {**HEADERS, **({"Authorization": ""} if rejection == "unauthorized" else {})}
    url = upload_url if rejection == "unauthorized" else "/api/sessions/unknown/attachments"
    response = await client.post(url, content=body(), headers=headers)
    assert response.status_code == (401 if rejection == "unauthorized" else 404)
    assert consumed == []
    assert temporary_files == []


@pytest.mark.parametrize("transition", ["archive", "delete", "archive-and-restore", "disable-account"])
async def test_upload_rechecks_the_original_session_after_read(
    client, upload_url, account_user_id, temporary_files, monkeypatch, transition,
):
    session_url = upload_url.removesuffix("/attachments")
    destination = paths_for(account_user_id).attachments / session_url.rsplit("/", 1)[1]
    read_finished, release = asyncio.Event(), asyncio.Event()
    read = UploadFile.read

    async def hold_after_read(file, size=-1):
        data = await read(file, size)
        read_finished.set()
        await release.wait()
        return data

    monkeypatch.setattr(UploadFile, "read", hold_after_read)
    pending = asyncio.create_task(client.post(upload_url, files={"file": ("old.bin", b"old upload")}))
    try:
        await asyncio.wait_for(read_finished.wait(), 3)
        if transition == "disable-account":
            assert await session_manager.db.set_user_disabled(account_user_id, at=datetime.now(UTC).isoformat())
        else:
            if transition == "delete":
                closed = await client.delete(session_url)
            else:
                closed = await client.post(f"{session_url}/archive", params={"replace": "false"})
            assert closed.status_code == 204, closed.text
        if transition == "archive-and-restore":
            status = await client.get(f"{session_url}/restoration")
            assert status.status_code == 200, status.text
            restored = await client.post(f"{session_url}/unarchive", json={
                "operation_id": "restore-during-upload",
                "service_generation": status.json()["service_generation"],
            })
            assert restored.status_code == 200, restored.text
            assert restored.json()["state"] == "finished", restored.text
        release.set()
        rejected = await asyncio.wait_for(pending, 3)
        assert rejected.status_code == 404, rejected.text
        assert temporary_files and all(file.closed for file in temporary_files)
        assert list(destination.glob("*")) == []
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)

    if transition == "archive-and-restore":
        fresh = await client.post(upload_url, files={"file": ("new.bin", b"new upload")})
        assert fresh.status_code == 201, fresh.text
        downloaded = await client.get(f"{upload_url}/{fresh.json()['id']}")
        assert downloaded.content == b"new upload"


async def test_upload_refuses_a_failed_owner_before_its_cleanup_callback(
    client, upload_url, account_user_id, temporary_files, monkeypatch,
):
    session_id = upload_url.split("/")[3]
    manager = SessionManager()
    await manager.initialize(session_manager.db)
    monkeypatch.setitem(app.dependency_overrides, deps.get_session_manager, lambda: manager)
    owner_ended = manager._owner_ended
    cleanup_pending = asyncio.Event()

    def delay_cleanup(owner, task):
        cleanup_pending.set()

    monkeypatch.setattr(manager, "_owner_ended", delay_cleanup)
    owner = manager._session_owner(manager.get_session(session_id))
    owner.task.cancel()
    try:
        await asyncio.wait_for(cleanup_pending.wait(), 3)
        response = await client.post(upload_url, files={"file": ("closed.bin", b"must not save")})
        assert response.status_code == 409, response.text
        assert temporary_files and all(file.closed for file in temporary_files)
        assert list((paths_for(account_user_id).attachments / session_id).glob("*")) == []
    finally:
        await asyncio.gather(owner.task, return_exceptions=True)
        owner_ended(owner, owner.task)


@pytest.mark.parametrize("declared_length", [None, "1"])
async def test_total_budget_stops_before_requesting_later_chunks(client, upload_url, temporary_files, declared_length):
    consumed = []
    chunks = [PREFIX, *([b"x" * (1024 * 1024)] * 28), SUFFIX]

    async def body():
        for chunk in chunks:
            consumed.append(len(chunk))
            yield chunk

    headers = {**HEADERS, **({"Content-Length": declared_length} if declared_length else {})}
    response = await client.post(upload_url, content=body(), headers=headers)
    assert response.status_code == 413
    assert BODY_LIMIT < sum(consumed) <= BODY_LIMIT + 1024 * 1024
    assert len(consumed) < len(chunks)
    assert temporary_files and all(file.closed for file in temporary_files)


async def test_exact_file_limit_is_accepted_and_roundtrips(client, upload_url, account_user_id, temporary_files):
    async def body():
        yield PREFIX
        for _ in range(25):
            yield b"x" * (1024 * 1024)
        yield SUFFIX

    response = await client.post(upload_url, content=body(), headers=HEADERS)
    assert response.status_code == 201, response.text
    assert response.json()["size"] == MAX_FILE_BYTES
    session_id = upload_url.split("/")[3]
    path = get_path(session_id, response.json()["id"], user_id=account_user_id)
    assert path.stat().st_size == 25 * 1024 * 1024
    assert temporary_files and all(file.closed for file in temporary_files)


async def test_declared_oversize_rejects_without_consuming_body(client, upload_url, temporary_files):
    consumed = []

    async def body():
        consumed.append(True)
        yield PREFIX + b"small body" + SUFFIX

    response = await client.post(upload_url, content=body(), headers={**HEADERS, "Content-Length": str(BODY_LIMIT + 1)})
    assert response.status_code == 413
    assert consumed == [] and temporary_files == []


@pytest.mark.parametrize("extra", ["second-file", "field-after-file", "wrong-name", "no-file"])
async def test_multipart_contract_closes_rejected_files(client, upload_url, temporary_files, extra):
    if extra == "second-file":
        body = PREFIX + b"x" * (2 * 1024 * 1024) + b"\r\n" + PREFIX + b"extra" + SUFFIX
    elif extra == "field-after-file":
        body = PREFIX + b"x" * (2 * 1024 * 1024) + b'\r\n--upload-boundary\r\nContent-Disposition: form-data; name="field"\r\n\r\nvalue' + SUFFIX
    elif extra == "wrong-name":
        body = PREFIX.replace(b'name="file"', b'name="other"') + b"content" + SUFFIX
    else:
        body = b"--upload-boundary--\r\n"
    response = await client.post(upload_url, content=body, headers=HEADERS)
    assert response.status_code == (400 if extra in {"second-file", "field-after-file"} else 422)
    assert all(file.closed for file in temporary_files)
    retried = await client.post(upload_url, files={"file": ("after.bin", b"complete")})
    assert retried.status_code == 201


@pytest.mark.parametrize("ending", [b'\r\n--upload-boundary\r\n\x00: broken\r\n', b''])
async def test_malformed_or_truncated_multipart_closes_every_temporary_file(client, upload_url, temporary_files, ending):
    async def body():
        yield PREFIX
        yield b"x" * (2 * 1024 * 1024)
        yield ending

    response = await client.post(upload_url, content=body(), headers=HEADERS)
    assert response.status_code in {400, 422}
    assert temporary_files and all(file.closed for file in temporary_files)


async def test_cancellation_during_parse_closes_rolled_files(client, upload_url, temporary_files):
    receiving = asyncio.Event()

    async def body():
        yield PREFIX + b"x" * (2 * 1024 * 1024)
        receiving.set()
        await asyncio.Event().wait()

    request = asyncio.create_task(client.post(upload_url, content=body(), headers=HEADERS))
    try:
        await asyncio.wait_for(receiving.wait(), 3)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert temporary_files and all(file.closed for file in temporary_files)
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
    assert (await client.post(upload_url, files={"file": ("after.bin", b"complete")})).status_code == 201


async def test_close_keeps_ownership_and_saved_file_through_repeated_cancellation(
    client, upload_url, account_user_id, temporary_files, monkeypatch,
):
    closing, release = asyncio.Event(), asyncio.Event()
    close = UploadFile.close

    async def delayed(file):
        closing.set()
        await release.wait()
        await close(file)

    monkeypatch.setattr(UploadFile, "close", delayed)
    request = asyncio.create_task(client.post(upload_url, files={"file": ("rolled.bin", b"x" * (2 * 1024 * 1024))}))
    try:
        await asyncio.wait_for(closing.wait(), 3)
        request.cancel("first disconnect")
        await asyncio.sleep(0)
        request.cancel("second disconnect")
        await asyncio.sleep(0)
        assert not request.done(), "the route must retain close responsibility"
        assert temporary_files and not temporary_files[0].closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert all(file.closed for file in temporary_files)
    finally:
        release.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
    session_id = upload_url.split("/")[3]
    [stored] = list((paths_for(account_user_id).attachments / session_id).iterdir())
    attachment_id, filename = stored.name.split("__", 1)
    assert filename == "rolled.bin"
    download = await client.get(f"{upload_url}/{attachment_id}")
    assert download.status_code == 200 and download.content == b"x" * (2 * 1024 * 1024)
    assert (await client.post(upload_url, files={"file": ("after.bin", b"complete")})).status_code == 201


@pytest.mark.parametrize("operation", ["rollover", "seek", "read"])
async def test_cancelled_file_worker_finishes_before_its_handle_is_closed(client, upload_url, temporary_files, monkeypatch, operation):
    entered = asyncio.Event()
    release, finished = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = getattr(tempfile.SpooledTemporaryFile, operation)

    def delayed(file, *args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3), "the test must release its original file worker"
        try:
            return original(file, *args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, operation, delayed)

    async def body():
        yield PREFIX
        yield b"x" * (2 * 1024 * 1024)
        yield SUFFIX

    request = asyncio.create_task(client.post(upload_url, content=body(), headers=HEADERS))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        request.cancel("disconnect while the original file worker is running")
        await asyncio.sleep(0)
        request.cancel("repeated cancellation")
        await asyncio.sleep(0)
        assert not request.done()
        assert temporary_files and not temporary_files[0].closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert await asyncio.to_thread(finished.wait, 3)
        assert all(file.closed for file in temporary_files)
    finally:
        release.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        assert await asyncio.to_thread(finished.wait, 3)
        for file in temporary_files:
            file.close()
    assert (await client.post(upload_url, files={"file": ("after.bin", b"complete")})).status_code == 201

import asyncio
import copy
import os
import uuid
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from server.database import Database
from server.models import SessionStatus
from server.session_manager import SessionManager
from server.users import UserManager
from server.workspace import paths_for
from tests.accounts import create_account
from tests.fake_run import FakeRunBase
from tests.harness_profiles import process_profile


@pytest.fixture
async def manager(tmp_path, monkeypatch):
    from server.harness import get_harness

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", process_profile(harness.profile))
    mgr = SessionManager()
    db = Database(":memory:")
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    await mgr.initialize(db)
    try:
        yield mgr
    finally:
        # Release any held CLI process before the manager goes away — a
        # SessionManager that keeps processes between turns must hand them
        # back when it's discarded, or the suite accumulates them.
        await mgr.stop_all_held_processes()
        # Close the aiosqlite connection so its worker thread exits
        # before the per-test event loop is torn down. A leaked
        # connection's thread later crashes with "Event loop is
        # closed" and pins the pytest process at atexit.
        await db.close()


async def _new(manager, name="S", working_dir=None, credential_id=None, origin="user"):
    """Create a session under the fixture account's protected default agent."""
    agent = await manager.db.get_system_agent()
    _create = manager.create_session
    return await _create(
        agent["id"], name, working_dir, credential_id=credential_id, origin=origin
    )


async def _session_client(manager, session):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from server.deps import get_session_manager
    from server.routers.sessions import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    token = await UserManager(manager.db).issue_token(session.user_id)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                       headers={"Authorization": "Bearer " + token})


async def _request_events(manager, session, prompt):
    """Observe one actual admitted request through its original completion.

    Admission/status frames belong to the dispatch interface. The returned
    frames are the runtime prefix, including its committed terminal, that
    used to be yielded by the private executor in these tests.
    """
    request_id = uuid.uuid4().hex
    events = []
    dispatch_frames = {"user_message", "queued", "dequeued", "dispatch_state", "status"}

    async def observe(event):
        if (event.get("session_id") == session.id
                and event.get("request_id") == request_id
                and event["type"] not in dispatch_frames):
            events.append(copy.deepcopy(event))

    manager.on_broadcast(request_id, observe)
    try:
        receipt = await manager.start_message(session.id, prompt, request_id=request_id)
        assert receipt.accepted and receipt.request_id == request_id
        completion = await asyncio.wait_for(receipt.wait(), 10)
        await asyncio.wait_for(manager.await_session_execution(session.id), 10)
        assert completion.input_request_id == request_id
        assert completion.request_id == request_id
        assert completion.recording_state == "committed"
        assert completion.outcome is not None
        terminal = [event for event in events if event["type"] == "result"]
        assert len(terminal) == 1
        assert terminal[0]["content"] == completion.outcome.model_dump()
        return events
    finally:
        manager.remove_broadcast(request_id)


class _QueuedRun(FakeRunBase):
    """A real Harness boundary whose stream is held until released/stopped."""

    def __init__(self, starts, *, release=None, stopped=None):
        self.starts = starts
        self.release = release or asyncio.Event()
        self.stops = stopped if stopped is not None else []
        self.started = asyncio.Event()
        self.prompt = None

    async def start(self, prompt, working_dir, resume_id=None, credential=None):
        self.prompt = prompt
        self.starts.append(prompt)
        self.started.set()

    async def stream(self):
        from server.harness import HarnessEvent

        await self.release.wait()
        yield HarnessEvent(type="result", session_id="queued-thread")

    async def interrupt(self):
        self.release.set()

    async def stop(self):
        self.stops.append(self.prompt)
        self.release.set()


@pytest.mark.asyncio
async def test_create_session(manager, tmp_path):
    directory = tmp_path / "project"
    directory.mkdir()
    session = await _new(manager, "Test Session", str(directory))
    assert session.name == "Test Session"
    assert session.working_dir == str(directory)
    assert session.status == SessionStatus.idle
    assert len(session.id) == 12
    assert session.id in manager.sessions
    assert any(row["id"] == session.id for row in await manager.db.load_sessions())


@pytest.mark.asyncio
async def test_create_session_default_dir(manager):
    # working_dir is frozen to an ABSOLUTE path at creation so the session's
    # storage location never depends on the server's runtime cwd. The default
    # is the account workspace, resolved once and retained by the session.

    session = await _new(manager, "Default Dir")
    assert session.working_dir == str(paths_for(session.user_id).workspace)
    assert os.path.isabs(session.working_dir)


@pytest.mark.asyncio
async def test_resolve_working_dir_is_absolute_and_cwd_independent(monkeypatch):
    """A relative working_dir is resolved to absolute once; an already-absolute
    one is returned unchanged regardless of the process cwd — so the derived
    Claude project slug can't shift when the server runs from a different
    directory (e.g. a cloud deployment with a different pwd)."""
    from server.session_manager import resolve_working_dir

    # Absolute input is stable no matter the cwd.
    monkeypatch.chdir("/tmp")
    assert resolve_working_dir("/srv/project") == "/srv/project"
    monkeypatch.chdir("/")
    assert resolve_working_dir("/srv/project") == "/srv/project"

    # Relative input resolves against the current cwd at call time.
    monkeypatch.chdir("/tmp")
    assert resolve_working_dir("proj") == str(Path("/tmp/proj"))
    assert os.path.isabs(resolve_working_dir("."))


@pytest.mark.asyncio
async def test_list_sessions(manager):
    assert manager.list_sessions() == []
    await _new(manager,"A")
    await _new(manager,"B")
    sessions = manager.list_sessions()
    assert len(sessions) == 2
    names = {s.name for s in sessions}
    assert names == {"A", "B"}


@pytest.mark.asyncio
async def test_get_session(manager):
    session = await _new(manager,"Find Me")
    found = manager.get_session(session.id)
    assert found is session
    assert manager.get_session("nonexistent") is None


@pytest.mark.asyncio
async def test_delete_session(manager):
    session = await _new(manager,"Delete Me")
    sid = session.id
    assert await manager.delete_session(sid) is True
    assert manager.get_session(sid) is None
    assert not any(row["id"] == sid for row in await manager.db.load_sessions())
    assert await manager.delete_session(sid) is False


@pytest.mark.asyncio
async def test_send_message_unknown_session(manager):
    with pytest.raises(ValueError, match="not found"):
        await manager.start_message("nonexistent", "hello")


@pytest.mark.asyncio
async def test_broadcast_registration(manager):
    calls = []

    async def cb(msg):
        calls.append(msg)

    manager.on_broadcast("test", cb)
    assert "test" in manager._broadcast_callbacks

    manager.remove_broadcast("test")
    assert "test" not in manager._broadcast_callbacks


@pytest.mark.asyncio
async def test_initialize_restores_sessions(tmp_path):
    """Create a session with one manager, then load into a fresh manager."""
    db = Database(":memory:")
    await db.initialize()
    try:
        await create_account(db, extra_roots=[tmp_path])
        mgr1 = SessionManager()
        await mgr1.initialize(db)
        directory = tmp_path / "restored-project"
        directory.mkdir()
        session = await _new(mgr1, "Restored", str(directory))
        sid = session.id

        # Create a fresh manager, initialize with the same DB
        mgr2 = SessionManager()
        await mgr2.initialize(db)
        restored = mgr2.get_session(sid)
        assert restored is not None
        assert restored.name == "Restored"
        assert restored.working_dir == str(directory)
        assert restored.status == SessionStatus.idle
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Message queue + interrupt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_message_queues_when_busy(manager, monkeypatch):
    session = await _new(manager,"Q")
    consumed: list[str] = []
    blocker = asyncio.Event()
    first = _QueuedRun(consumed, release=blocker)
    second = _QueuedRun(consumed)
    second.release.set()
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first, second]))

    events: list[dict] = []
    queued_published = asyncio.Event()

    async def cb(msg: dict) -> None:
        events.append(msg)
        if msg["type"] == "queued" and msg.get("content") == "second":
            queued_published.set()

    manager.on_broadcast("test", cb)

    original = await manager.start_message(session.id, "first")
    await asyncio.wait_for(first.started.wait(), 2)

    # Second start_message should queue rather than fire
    queued_receipt = await manager.start_message(session.id, "second")
    assert [item["summary"] for item in manager.dispatch_snapshot(session.id)["items"]] == ["second"]
    await asyncio.wait_for(queued_published.wait(), 2)

    queued = [e for e in events if e["type"] == "queued"]
    assert len(queued) == 1
    assert queued[0]["content"] == "second"
    assert queued[0]["queue_length"] == 1

    # Releasing the original stream allows the admitted next request to run.
    blocker.set()
    original_completion, queued_completion = await asyncio.wait_for(
        asyncio.gather(original.wait(), queued_receipt.wait()), 2,
    )

    assert consumed == ["first", "second"]
    assert original_completion.outcome.status == queued_completion.outcome.status == "success"
    assert manager.dispatch_snapshot(session.id)["items"] == []
    assert any(e["type"] == "dequeued" for e in events)


@pytest.mark.asyncio
async def test_interrupt_cancels_current_and_pauses_queue_until_resume(manager, monkeypatch):
    session = await _new(manager,"I")
    started: list[str] = []
    cancelled: list[str] = []

    first = _QueuedRun(started, stopped=cancelled)
    second = _QueuedRun(started, stopped=cancelled)
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first, second]))

    original = await manager.start_message(session.id, "first")
    await asyncio.wait_for(first.started.wait(), 2)
    assert started == ["first"]

    queued_receipt = await manager.start_message(session.id, "second")
    assert [item["summary"] for item in manager.dispatch_snapshot(session.id)["items"]] == ["second"]

    ok = await manager.interrupt(session.id)
    assert ok is True

    completion = await asyncio.wait_for(original.wait(), 2)
    await asyncio.wait_for(manager.await_session_execution(session.id), 2)
    assert completion.outcome.status == "interrupted"
    assert started == ["first"]
    assert cancelled == ["first"]
    assert [item["summary"] for item in manager.dispatch_snapshot(session.id)["items"]] == ["second"]
    assert manager.dispatch_snapshot(session.id)["paused"] is True
    await manager.resume_dispatch(session.id)

    # Explicit resume allows the preserved next prompt to start.
    await asyncio.wait_for(second.started.wait(), 2)

    assert started == ["first", "second"]
    assert cancelled == ["first"]
    assert manager.dispatch_snapshot(session.id)["items"] == []

    # Cleanup: cancel the second so the test doesn't hang
    await manager.interrupt(session.id)
    assert (await asyncio.wait_for(queued_receipt.wait(), 2)).outcome.status == "interrupted"


@pytest.mark.asyncio
async def test_interrupt_twice_in_a_row_each_works(manager, monkeypatch):
    """Reproduces the bug where pressing Esc to interrupt a queued message
    that just started running was a no-op."""
    session = await _new(manager,"DoubleInterrupt")
    started: list[str] = []
    cancelled: list[str] = []

    first = _QueuedRun(started, stopped=cancelled)
    second = _QueuedRun(started, stopped=cancelled)
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first, second]))

    original = await manager.start_message(session.id, "first")
    await asyncio.wait_for(first.started.wait(), 2)
    assert started == ["first"]

    queued_receipt = await manager.start_message(session.id, "second")
    assert [item["summary"] for item in manager.dispatch_snapshot(session.id)["items"]] == ["second"]

    # First interrupt
    assert await manager.interrupt(session.id) is True
    assert (await asyncio.wait_for(original.wait(), 2)).outcome.status == "interrupted"
    await asyncio.wait_for(manager.await_session_execution(session.id), 2)
    assert manager.dispatch_snapshot(session.id)["paused"] is True
    await manager.resume_dispatch(session.id)

    # Wait for the queue to advance and "second" to start
    await asyncio.wait_for(second.started.wait(), 2)
    assert started == ["first", "second"]
    assert cancelled == ["first"]

    # Second interrupt — this is the bug repro: must also succeed
    assert await manager.interrupt(session.id) is True
    assert (await asyncio.wait_for(queued_receipt.wait(), 2)).outcome.status == "interrupted"
    await asyncio.wait_for(manager.await_session_execution(session.id), 2)
    assert cancelled == ["first", "second"]
    assert manager.dispatch_snapshot(session.id)["items"] == []


@pytest.mark.asyncio
async def test_interrupt_does_not_wedge_on_slow_backend_stop(manager, monkeypatch):
    """If the backend's stop()/interrupt() hangs, the manager's interrupt()
    must still return promptly (within the timeout) so the WS receive loop
    isn't blocked from processing subsequent interrupts."""
    session = await _new(manager,"SlowStop")
    release_stop = asyncio.Event()

    class HangingBackend(FakeRunBase):
        name = "hanging"

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            started.set()

        def stream(self):
            async def _gen():
                await asyncio.sleep(60)
                yield  # never reached
            return _gen()

        async def stop(self):
            await release_stop.wait()  # Caller remains prompt while teardown is held.

        async def interrupt(self):
            await release_stop.wait()

    started = asyncio.Event()
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: HangingBackend())
    receipt = await manager.start_message(session.id, "x")
    await asyncio.wait_for(started.wait(), 2)

    # interrupt() must return within the backend-interrupt timeout (2s) + a margin
    try:
        ok = await asyncio.wait_for(manager.interrupt(session.id), timeout=4.0)
    except TimeoutError:
        pytest.fail("interrupt() blocked on hanging backend — WS would be wedged")
    finally:
        release_stop.set()

    assert ok is True

    assert (await asyncio.wait_for(receipt.wait(), 2)).outcome.status == "interrupted"


@pytest.mark.asyncio
async def test_interrupt_when_idle_returns_false(manager):
    session = await _new(manager,"Idle")
    assert await manager.interrupt(session.id) is False


@pytest.mark.asyncio
async def test_format_answers_handles_select_and_text(manager):
    questions = [
        {"question": "Favorite color?", "options": [{"label": "blue"}]},
        {"question": "Notes?", "options": []},
    ]
    answers = [
        {"selected": ["blue"]},
        {"text": "I like teal too"},
    ]
    session = await _new(manager, "Mixed human answers")
    question = await manager.create_pending_question(session.id, questions)
    assert await manager.answer_question(session.id, question, answers)
    assert await manager.wait_for_question_answer(session.id, question) == (
        "Q: Favorite color?\nA: blue\n\nQ: Notes?\nA: I like teal too"
    )


@pytest.mark.asyncio
async def test_answer_question_unknown_returns_false(manager):
    session = await _new(manager,"UnknownQ")
    assert await manager.answer_question(session.id, "nope", []) is False


@pytest.mark.asyncio
async def test_answer_question_wakes_waiter_removes_form_and_broadcasts_once(manager, monkeypatch):
    """Answering wakes the long-poll, removes the form and publishes once."""
    session = await _new(manager,"Q")
    events: list[dict] = []

    async def cb(msg: dict) -> None:
        events.append(msg)

    manager.on_broadcast("test", cb)

    qid = await manager.create_pending_question(
        session.id, [{"question": "Pick one", "options": [{"label": "A"}]}],
    )
    pending = manager.get_pending_questions(session.id)[0]
    waiting = asyncio.Event()
    ready_wait = pending.ready.wait

    async def observe_wait():
        waiting.set()
        return await ready_wait()

    monkeypatch.setattr(pending.ready, "wait", observe_wait)
    polling = asyncio.create_task(manager.wait_for_question_answer(session.id, qid, timeout=1))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert not polling.done()
        assert await manager.answer_question(session.id, qid, [{"selected": ["A"]}]) is True
        assert await asyncio.wait_for(polling, 1) == "Q: Pick one\nA: A"
    finally:
        polling.cancel()
        await asyncio.gather(polling, return_exceptions=True)
    assert not manager.has_pending_question(session.id, qid)
    qa = [e for e in events if e["type"] == "question_answer"]
    assert len(qa) == 1
    assert qa[0]["content"] == "Q: Pick one\nA: A"


@pytest.mark.asyncio
async def test_wait_for_question_answer_returns_none_on_timeout(manager):
    """When the user takes too long for a single poll window, the
    waiter returns None so the MCP server can re-poll."""
    session = await _new(manager,"Timeout")
    qid = await manager.create_pending_question(session.id, [{"question": "?", "options": []}])

    answer = await manager.wait_for_question_answer(
        session.id, qid, timeout=0.1
    )
    assert answer is None


@pytest.mark.asyncio
async def test_manual_answer_cancels_auto_answer_timer(manager, monkeypatch):
    """If the user answers before the timeout, the auto-answer timer
    should be cancelled and never fire — otherwise the long-poll
    would see the autonomy-mode text instead of the user's choice."""
    from server.config import settings

    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 0.5)

    session = await _new(manager,"ManualBeatsTimer")
    qid = await manager.create_pending_question(
        session.id, [{"question": "Pick", "options": [{"label": "A"}]}],
    )

    await manager.answer_question(session.id, qid, [{"selected": ["A"]}])

    # Wait past the timeout — the auto-answer text must NOT overwrite
    # the user's choice.
    await asyncio.sleep(0.7)
    assert await manager.wait_for_question_answer(session.id, qid, timeout=0.1) == "Q: Pick\nA: A"


@pytest.mark.asyncio
async def test_event_to_message_content_maps_question_request():
    """The translation layer keeps the persisted shape stable so existing
    UI handling for question_request messages doesn't break."""
    from server.harness import HarnessEvent
    from server.session_manager import SessionManager

    ev = HarnessEvent(
        type="question_request",
        tool_use_id="q-99",
        tool_input={"questions": [{"question": "X?", "options": []}]},
    )
    msg = SessionManager._event_to_message_content(ev)
    assert msg is not None
    assert msg.type == "question_request"
    assert msg.tool_name == "AskUserQuestion"
    assert msg.tool_use_id == "q-99"


@pytest.mark.asyncio
async def test_resolve_credential_returns_decrypted_secret(manager):
    """When a session has credential_id, _resolve_credential should fetch
    and decrypt the row."""
    from datetime import datetime

    from server.crypto import encrypt

    now = datetime.now(UTC).isoformat()
    owner = (await manager.db.get_system_agent())["user_id"]
    enc = encrypt("sk-ant-secret", await UserManager(manager.db).data_key_for(owner))
    await manager.db.save_credential(
        credential_id="c-1",
        backend="claude-code",
        label="L",
        auth_type="api_key",
        secret_encrypted=enc,
        created_at=now,
        user_id=owner,
    )
    session = await _new(manager,"S", credential_id="c-1")
    from server.harness import get_harness

    cred = await manager._resolve_credential(session, None, get_harness("claude-code"))
    assert cred is not None
    assert cred.backend == "claude-code"
    assert cred.auth_type == "api_key"
    assert cred.secret == "sk-ant-secret"


@pytest.mark.asyncio
async def test_resolve_credential_rejects_explicit_missing_binding(manager):
    session = await _new(manager, "S")
    # Explicit bindings never fall back to the host's default identity.
    session.credential_id = "ghost"
    from server.credential_bindings import CredentialBindingError
    from server.harness import get_harness

    with pytest.raises(CredentialBindingError, match="not found"):
        await manager._resolve_credential(session, None, get_harness("claude-code"))


@pytest.mark.asyncio
async def test_resolve_credential_oauth_bundle_returns_oauth_credential(manager):
    """OAuth-bundle credentials (stored as a JSON blob with refresh_token)
    return BackendCredential(auth_type='oauth', secret=access_token)."""
    import json
    import time
    from datetime import datetime

    from server.crypto import encrypt

    bundle = json.dumps(
        {
            "access_token": "oat-fresh-access",
            "refresh_token": "ort-refresh",
            # Comfortably in the future so no refresh is triggered.
            "expires_at_epoch": time.time() + 3600,
            "scopes": ["user:inference"],
            "token_type": "Bearer",
        }
    )
    owner = (await manager.db.get_system_agent())["user_id"]
    enc = encrypt(bundle, await UserManager(manager.db).data_key_for(owner))
    await manager.db.save_credential(
        credential_id="c-oauth",
        backend="claude-code",
        label="Pro/Max",
        auth_type="oauth",
        secret_encrypted=enc,
        created_at=datetime.now(UTC).isoformat(),
        user_id=owner,
    )
    session = await _new(manager,"S-oauth", credential_id="c-oauth")
    from server.harness import get_harness

    cred = await manager._resolve_credential(session, None, get_harness("claude-code"))
    assert cred is not None
    assert cred.auth_type == "oauth"
    assert cred.secret == "oat-fresh-access"


@pytest.mark.asyncio
async def test_resolve_credential_refreshes_expired_oauth_token(manager, monkeypatch):
    """When the stored access_token is past expiry, the resolver should
    call the provider's refresh endpoint, persist the new bundle, and
    hand back the fresh access_token."""
    import json
    import time
    from datetime import datetime

    from server import oauth_providers as op
    from server.crypto import decrypt, encrypt
    from server.oauth_providers import OAuthTokenSet

    expired_bundle = json.dumps(
        {
            "access_token": "oat-expired",
            "refresh_token": "ort-still-valid",
            # 1 minute ago — well past leeway
            "expires_at_epoch": time.time() - 60,
            "scopes": ["user:inference"],
            "token_type": "Bearer",
        }
    )
    owner = (await manager.db.get_system_agent())["user_id"]
    enc = encrypt(expired_bundle, await UserManager(manager.db).data_key_for(owner))
    await manager.db.save_credential(
        credential_id="c-stale",
        backend="claude-code",
        label="Stale",
        auth_type="oauth",
        secret_encrypted=enc,
        created_at=datetime.now(UTC).isoformat(),
        user_id=owner,
    )

    captured_refresh: list[str] = []

    async def fake_refresh(refresh_token):
        captured_refresh.append(refresh_token)
        return OAuthTokenSet(
            access_token="oat-brand-new",
            refresh_token="ort-still-valid",
            expires_at_epoch=time.time() + 3600,
            scopes=["user:inference"],
        )

    provider = op.get_provider("claude-code")
    monkeypatch.setattr(provider, "refresh_access_token", fake_refresh)

    session = await _new(manager,"S-stale", credential_id="c-stale")
    from server.harness import get_harness

    cred = await manager._resolve_credential(session, None, get_harness("claude-code"))

    assert captured_refresh == ["ort-still-valid"]
    assert cred is not None
    assert cred.auth_type == "oauth"
    assert cred.secret == "oat-brand-new"

    # The new bundle was persisted: a second resolve should find a fresh
    # row (and NOT refresh again, since the new bundle is not expired).
    captured_refresh.clear()
    cred2 = await manager._resolve_credential(session, None, get_harness("claude-code"))
    assert cred2 is not None
    assert cred2.secret == "oat-brand-new"
    assert captured_refresh == []  # no second refresh

    # And the stored bundle decrypts to the new access_token.
    row = await manager.db.get_credential("c-stale")
    new_bundle = json.loads(decrypt(row["secret_encrypted"], await UserManager(manager.db).data_key_for(owner)))
    assert new_bundle["access_token"] == "oat-brand-new"
    assert row.get("needs_reconnect") is False


@pytest.mark.asyncio
async def test_resolve_credential_marks_needs_reconnect_on_refresh_failure(
    manager, monkeypatch
):
    """Refresh failure → credential is marked needs_reconnect with a
    typed error code, and explicit execution is rejected without host fallback."""
    import json
    import time
    from datetime import datetime

    from server import oauth_providers as op
    from server.crypto import encrypt

    expired_bundle = json.dumps(
        {
            "access_token": "oat-expired",
            "refresh_token": "ort-dead",
            "expires_at_epoch": time.time() - 60,
            "scopes": [],
            "token_type": "Bearer",
        }
    )
    owner = (await manager.db.get_system_agent())["user_id"]
    enc = encrypt(expired_bundle, await UserManager(manager.db).data_key_for(owner))
    await manager.db.save_credential(
        credential_id="c-dead",
        backend="claude-code",
        label="Dead",
        auth_type="oauth",
        secret_encrypted=enc,
        created_at=datetime.now(UTC).isoformat(),
        user_id=owner,
    )

    async def fake_refresh(refresh_token):
        from server.oauth_errors import OAuthRefreshError, RefreshErrorCode

        raise OAuthRefreshError(
            "refresh endpoint returned 400: invalid_grant",
            RefreshErrorCode.refresh_token_invalidated, reconnect=True,
        )

    provider = op.get_provider("claude-code")
    monkeypatch.setattr(provider, "refresh_access_token", fake_refresh)

    session = await _new(manager,"S-dead", credential_id="c-dead")
    from server.harness import get_harness

    with pytest.raises(ValueError, match="Reconnect"):
        await manager._resolve_credential(session, None, get_harness("claude-code"))

    row = await manager.db.get_credential("c-dead")
    assert row.get("needs_reconnect") is True
    assert row.get("last_refresh_error_code") == "refresh_token_invalidated"

    # A subsequent resolve rejects the revoked credential before retrying refresh.
    with pytest.raises(ValueError, match="Reconnect"):
        await manager._resolve_credential(session, None, get_harness("claude-code"))


@pytest.mark.asyncio
async def test_oauth_credential_env_var_reaches_subprocess(manager, monkeypatch):
    """End-to-end: OAuth-bundle credential → resolver decrypts/refreshes →
    backend build_args lands the access_token in CLAUDE_CODE_OAUTH_TOKEN
    on the subprocess env. Mirrors the existing ANTHROPIC_API_KEY test."""
    import json
    import time
    from datetime import datetime

    from server.crypto import encrypt

    bundle = json.dumps(
        {
            "access_token": "oat-runtime-token",
            "refresh_token": "ort-x",
            "expires_at_epoch": time.time() + 3600,
            "scopes": ["user:inference"],
            "token_type": "Bearer",
        }
    )
    owner = (await manager.db.get_system_agent())["user_id"]
    enc = encrypt(bundle, await UserManager(manager.db).data_key_for(owner))
    await manager.db.save_credential(
        credential_id="c-env-oauth",
        backend="claude-code",
        label="EnvOAuth",
        auth_type="oauth",
        secret_encrypted=enc,
        created_at=datetime.now(UTC).isoformat(),
        user_id=owner,
    )
    session = await _new(manager,
        "EnvSessionOAuth", credential_id="c-env-oauth"
    )

    from server.harness import get_harness

    cred = await manager._resolve_credential(session, None, get_harness("claude-code"))
    assert cred is not None
    backend = manager._make_run(session)
    _, kwargs = backend.build_argv(
        "prompt", session.working_dir, None, credential=cred
    )
    env = kwargs.get("env") or {}
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "oat-runtime-token"
    # And we don't accidentally set both — that would confuse the CLI.
    assert "ANTHROPIC_API_KEY" not in {
        k for k in env.keys() if env[k] == "oat-runtime-token"
    }


@pytest.mark.asyncio
async def test_credential_env_var_reaches_spawned_subprocess(manager):
    """End-to-end-ish: when a session has a credential, the *decrypted*
    secret really lands in the env dict that would be passed to
    asyncio.create_subprocess_exec.

    Covers the chain: DB row → _resolve_credential → HarnessCredential →
    SessionManager._make_run → HarnessRun.build_argv.
    """
    from datetime import datetime

    from server.crypto import encrypt

    # Seed a credential
    owner = (await manager.db.get_system_agent())["user_id"]
    enc = encrypt("sk-real-secret", await UserManager(manager.db).data_key_for(owner))
    await manager.db.save_credential(
        credential_id="c-env",
        backend="claude-code",
        label="EnvTest",
        auth_type="api_key",
        secret_encrypted=enc,
        created_at=datetime.now(UTC).isoformat(),
        user_id=owner,
    )
    session = await _new(manager,"EnvSession", credential_id="c-env")

    # 1. Resolve through the session_manager pipeline
    from server.harness import get_harness

    cred = await manager._resolve_credential(session, None, get_harness("claude-code"))
    assert cred is not None and cred.secret == "sk-real-secret"

    # 2. The backend factory the session_manager would use must then turn
    # that credential into a real env on the subprocess invocation.
    backend = manager._make_run(session)
    argv, kwargs = backend.build_argv(
        "prompt", session.working_dir, None, credential=cred
    )
    env = kwargs.get("env") or {}
    assert env.get("ANTHROPIC_API_KEY") == "sk-real-secret", (
        f"decrypted secret didn't make it to subprocess env: {env.get('ANTHROPIC_API_KEY')!r}"
    )

    # argv should be the real claude CLI in VM0 shape — positional
    # argv prompt, --dangerously-skip-permissions instead of the old
    # --permission-prompt-tool=stdio path. The migration away from
    # --input-format=stream-json was the whole point of the refactor
    # (see docs/post-mortems/2026-05-18-bg-pipeline-hardening.md §2).
    argv_str = " ".join(str(a) for a in argv)
    assert "claude" in argv_str
    assert "--dangerously-skip-permissions" in argv_str
    assert "--input-format=stream-json" not in argv_str

    # And a sanity check on the *negative* path: a session with no
    # credential must NOT inject one (unless the parent shell already had).
    bare_session = await _new(manager,"Bare")
    bare_backend = manager._make_run(bare_session)
    _, bare_kwargs = bare_backend.build_argv(
        "p", bare_session.working_dir, None, credential=None
    )
    import os as _os
    assert (bare_kwargs.get("env") or {}).get("ANTHROPIC_API_KEY") == _os.environ.get(
        "ANTHROPIC_API_KEY"
    )


@pytest.mark.asyncio
async def test_make_run_applies_agent_config(manager):
    """_make_run builds a run whose argv reflects the agent's system prompt /
    model / MCP set / tool allow-deny (agent-refactor.md §5.2)."""
    import json as _json
    import uuid as _uuid
    from datetime import datetime

    now = datetime.now(UTC).isoformat()
    aid = _uuid.uuid4().hex[:12]
    await manager.db.save_agent(
        agent_id=aid,
        name="Configured",
        user_id=(await manager.db.get_system_agent())["user_id"],
        created_at=now,
        updated_at=now,
        system_prompt="You are a pirate.",
        model="claude-opus-4-7",
        mcp_servers=["ask"],
        tool_deny="Bash",
    )
    session = await manager.create_session(aid, name="S")
    agent = await manager.db.get_agent(aid)
    backend = manager._make_run(session, agent)
    argv, _ = backend.build_argv("hi", session.working_dir, None)

    # model
    assert "--model" in argv and "claude-opus-4-7" in argv
    # agent persona is appended ahead of the Octopus tools section
    ap = argv[argv.index("--append-system-prompt") + 1]
    assert "You are a pirate." in ap
    assert "Octopus in-app tools" in ap
    # Supported deny = always-on AskUserQuestion + agent deny.
    dis = argv[argv.index("--disallowedTools") + 1]
    assert "AskUserQuestion" in dis and "Bash" in dis
    assert "--allowedTools" not in argv
    # only the selected MCP server is registered
    cfg = _json.loads(argv[argv.index("--mcp-config") + 1])
    assert set(cfg["mcpServers"].keys()) == {"ask"}


@pytest.mark.asyncio
async def test_make_run_dispatches_on_backend(manager):
    """_make_run builds a HarnessRun whose profile matches session.backend
    (codex-backend.md §5.5). Codex must not inherit the Claude premature-exit
    recovery (a Claude-CLI bug workaround)."""
    from server.harness import HarnessRun, get_harness

    agent = await manager.db.get_system_agent()
    claude_s = await manager.create_session(agent["id"], name="C", backend="claude-code")
    codex_s = await manager.create_session(agent["id"], name="X", backend="codex")

    claude_run = manager._make_run(claude_s, agent)
    codex_run = manager._make_run(codex_s, agent)
    assert isinstance(claude_run, HarnessRun) and isinstance(codex_run, HarnessRun)
    assert claude_run.profile.backend == "claude-code"
    assert codex_run.profile.backend == "codex"

    # Premature-exit recovery is a harness property, not a per-run flag.
    assert get_harness("claude-code").premature_exit_recovery is True
    assert get_harness("codex").premature_exit_recovery is False


@pytest.mark.asyncio
async def test_run_backend_translates_events_end_to_end(manager):
    """A request publishes the Harness prefix with its original attempt ID."""
    from server.harness import HarnessEvent

    session = await _new(manager,"E2E")

    events_to_emit = [
        HarnessEvent(type="text", content="hello"),
        HarnessEvent(
            type="tool_use",
            tool_name="Bash",
            tool_input={"command": "ls"},
            tool_use_id="t1",
        ),
        HarnessEvent(
            type="tool_result", content="out", tool_use_id="t1", is_error=False
        ),
        HarnessEvent(
            type="result", session_id="claude-sid-1", cost=0.01, num_turns=1
        ),
    ]

    class ScriptedBackend(FakeRunBase):
        name = "scripted"

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            pass

        def stream(self):
            async def _gen():
                for e in events_to_emit:
                    yield e
            return _gen()

        async def stop(self):
            pass

    # Only the external Harness boundary is scripted.
    def fake_factory(s, agent=None, connectors=None):
        return ScriptedBackend()

    manager._make_run = fake_factory  # type: ignore[method-assign]

    ws_msgs = await _request_events(manager, session, "go")
    assert ws_msgs[0]["type"] == "attempt_started"
    attempt_id = ws_msgs[0]["attempt_id"]
    ws_msgs = ws_msgs[1:]
    assert all(m["attempt_id"] == attempt_id for m in ws_msgs)
    types = [m["type"] for m in ws_msgs]
    assert types == ["assistant_text", "tool_use", "tool_result", "result"]
    assert ws_msgs[0]["content"] == "hello"
    assert ws_msgs[1]["tool"] == "Bash"
    assert ws_msgs[2]["output"] == "out"
    assert ws_msgs[3]["claude_session_id"] == "claude-sid-1"

    # Resume id was persisted
    assert session.claude_session_id == "claude-sid-1"


# ---------------------------------------------------------------------------
# Auto-respawn on CLI premature exit
# (post-mortem in docs/post-mortems/2026-05-18-bg-pipeline-hardening.md §2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_backend_auto_respawns_on_premature_exit_after_tool(manager):
    """If the CLI exits after a tool roundtrip without ever emitting a
    `result` event, the request should respawn the backend exactly
    once with prompt='continue' and the captured resume id, then
    surface the events from the recovery turn."""
    from server.harness import HarnessEvent

    session = await _new(manager,"Recovery")

    # Invocation 1: init → tool_use → tool_result, then CLI dies
    # (no `result`). Invocation 2: text → result. The model "finishes"
    # what it owed us after the continue nudge.
    invocations: list[dict[str, Any]] = []

    class FlakyBackend(FakeRunBase):
        name = "flaky"

        def __init__(self, events: list[HarnessEvent]) -> None:
            self._events = events
            self.started_with: dict[str, Any] | None = None

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            self.started_with = {
                "prompt": prompt,
                "working_dir": working_dir,
                "resume_id": resume_id,
            }
            invocations.append(self.started_with)

        def stream(self):
            async def _gen():
                for e in self._events:
                    yield e
            return _gen()

        async def stop(self):
            pass

    backends_iter = iter([
        FlakyBackend([
            HarnessEvent(type="session_started", session_id="claude-sid-recover"),
            HarnessEvent(
                type="tool_use",
                tool_name="Read",
                tool_input={"file_path": "/big.md"},
                tool_use_id="tu1",
            ),
            HarnessEvent(
                type="tool_result", content="...", tool_use_id="tu1", is_error=False
            ),
            # No `result` — this is the bug.
        ]),
        FlakyBackend([
            HarnessEvent(type="session_started", session_id="claude-sid-recover"),
            HarnessEvent(type="text", content="continuing where I left off"),
            HarnessEvent(
                type="result",
                session_id="claude-sid-recover",
                cost=0.02,
                num_turns=2,
            ),
        ]),
    ])

    manager._make_run = lambda s, agent=None, connectors=None: next(backends_iter)  # type: ignore[method-assign,assignment]

    ws_msgs: list[dict[str, Any]] = await _request_events(manager, session, "go")
    attempts = [m for m in ws_msgs if m["type"] == "attempt_started"]
    assert len(attempts) == 2 and attempts[0]["attempt_id"] != attempts[1]["attempt_id"]
    ws_msgs = [m for m in ws_msgs if m["type"] != "attempt_started"]
    types = [m["type"] for m in ws_msgs]

    # First invocation's events, then a recovery marker, then the
    # second invocation's events. session_started is internal-only and
    # does not appear in the broadcast stream.
    assert types == ["tool_use", "tool_result", "recovery", "assistant_text", "result"]
    assert ws_msgs[2]["message"] == "(auto-resumed after CLI exited mid-turn)"
    assert ws_msgs[3]["content"] == "continuing where I left off"
    assert ws_msgs[4]["claude_session_id"] == "claude-sid-recover"

    # Exactly two CLI invocations, the second with prompt="continue"
    # and the resume id captured from the first invocation's init.
    assert len(invocations) == 2
    assert invocations[0]["prompt"] == "go"
    assert invocations[0]["resume_id"] is None
    assert invocations[1]["prompt"] == "continue"
    assert invocations[1]["resume_id"] == "claude-sid-recover"
    assert session.claude_session_id == "claude-sid-recover"


@pytest.mark.asyncio
async def test_run_backend_bounds_recovery_to_single_retry(manager):
    """If the second CLI invocation also exits prematurely after a
    tool roundtrip, give up rather than loop forever. The retry
    budget is one — after that the request publishes one failed terminal."""
    from server.harness import HarnessEvent

    session = await _new(manager,"BoundedRecovery")

    invocations: list[dict[str, Any]] = []

    def make_flaky_events() -> list[HarnessEvent]:
        return [
            HarnessEvent(type="session_started", session_id="claude-sid-stuck"),
            HarnessEvent(
                type="tool_use",
                tool_name="Read",
                tool_input={"file_path": "/big.md"},
                tool_use_id="tu",
            ),
            HarnessEvent(
                type="tool_result", content="...", tool_use_id="tu", is_error=False
            ),
            # No `result` — bug fires every time.
        ]

    class AlwaysFlakyBackend(FakeRunBase):
        name = "always-flaky"

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            invocations.append({"prompt": prompt, "resume_id": resume_id})

        def stream(self):
            async def _gen():
                for e in make_flaky_events():
                    yield e
            return _gen()

        async def stop(self):
            pass

    manager._make_run = lambda s, agent=None, connectors=None: AlwaysFlakyBackend()  # type: ignore[method-assign,assignment]

    ws_msgs: list[dict[str, Any]] = await _request_events(manager, session, "go")

    # Exactly 2 invocations: original + 1 retry. No third attempt.
    assert len(invocations) == 2
    assert invocations[0]["prompt"] == "go"
    assert invocations[1]["prompt"] == "continue"

    # Events from both invocations are surfaced, with the recovery
    # marker between them and one final failed logical result because the
    # recovery also failed.
    assert len([m for m in ws_msgs if m["type"] == "attempt_started"]) == 2
    ws_msgs = [m for m in ws_msgs if m["type"] != "attempt_started"]
    types = [m["type"] for m in ws_msgs]
    assert types == ["tool_use", "tool_result", "recovery", "tool_use", "tool_result", "result"]
    assert ws_msgs[2]["message"] == "(auto-resumed after CLI exited mid-turn)"


@pytest.mark.asyncio
async def test_run_backend_does_not_respawn_on_clean_exit(manager):
    """A turn that ends with a `result` event is healthy — no
    recovery should fire even if it included tool calls."""
    from server.harness import HarnessEvent

    session = await _new(manager,"CleanExit")

    invocations: list[str] = []

    class CleanBackend(FakeRunBase):
        name = "clean"

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            invocations.append(prompt)

        def stream(self):
            async def _gen():
                yield HarnessEvent(type="session_started", session_id="sid-clean")
                yield HarnessEvent(
                    type="tool_use",
                    tool_name="Read",
                    tool_input={"file_path": "/x"},
                    tool_use_id="t",
                )
                yield HarnessEvent(
                    type="tool_result", content="ok", tool_use_id="t", is_error=False
                )
                yield HarnessEvent(type="text", content="done")
                yield HarnessEvent(
                    type="result", session_id="sid-clean", cost=0.01, num_turns=1
                )
            return _gen()

        async def stop(self):
            pass

    manager._make_run = lambda s, agent=None, connectors=None: CleanBackend()  # type: ignore[method-assign,assignment]

    _ = await _request_events(manager, session, "go")
    assert invocations == ["go"]  # exactly one — no retry


@pytest.mark.asyncio
async def test_run_backend_does_not_respawn_when_no_tool_use(manager):
    """If the CLI dies without ever emitting a tool_use, that's not
    the documented bug — could be auth failure, network drop, etc.
    Don't retry; surface the incomplete turn as-is."""
    from server.harness import HarnessEvent

    session = await _new(manager,"DiesEarly")
    invocations: list[str] = []

    class CrashEarlyBackend(FakeRunBase):
        name = "crash-early"

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            invocations.append(prompt)

        def stream(self):
            async def _gen():
                yield HarnessEvent(type="session_started", session_id="sid-early")
                # No tool_use. No result. CLI just died.
            return _gen()

        async def stop(self):
            pass

    manager._make_run = lambda s, agent=None, connectors=None: CrashEarlyBackend()  # type: ignore[method-assign,assignment]

    _ = await _request_events(manager, session, "go")
    assert invocations == ["go"]  # no retry — not the bug we recover from


@pytest.mark.asyncio
async def test_delete_session_clears_queue(manager, monkeypatch):
    session = await _new(manager,"Del")
    starts = []
    backend = _QueuedRun(starts)
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: backend)

    active = await manager.start_message(session.id, "first")
    await asyncio.wait_for(backend.started.wait(), 2)
    queued = await manager.start_message(session.id, "second")
    assert [item["summary"] for item in manager.dispatch_snapshot(session.id)["items"]] == ["second"]

    await manager.delete_session(session.id)
    active_done, queued_done = await asyncio.wait_for(
        asyncio.gather(active.wait(), queued.wait()), 2,
    )
    assert manager.get_session(session.id) is None
    assert active_done.outcome.status == "interrupted"
    assert queued_done.state == "deleted"
    assert starts == ["first"] and backend.stops == ["first"]
    assert not any(row["id"] == session.id for row in await manager.db.load_sessions())


# ---------------------------------------------------------------------------
# /archive feature — hide old history, fresh session with same settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_creates_new_session_with_same_settings(manager):
    owner = (await manager.db.get_system_agent())["user_id"]
    directory = paths_for(owner).workspace / "work"
    directory.mkdir()
    await manager.db.save_credential(
        credential_id="c-1", backend="claude-code", label="synthetic",
        auth_type="api_key", secret_encrypted="not-used-by-this-test",
        created_at="2026-10-17T00:00:00+00:00",
        user_id=owner,
    )
    old = await _new(manager,
        name="Work",
        working_dir=str(directory),
        credential_id="c-1",
    )
    # Simulate prior conversation: 3 persisted messages, a resume id.
    old._message_count = 3
    old.claude_session_id = "claude-abc"
    await manager.db.update_session_field(
        old.id, claude_session_id="claude-abc"
    )

    new = await manager.archive_session(old.id)

    assert new.id != old.id
    assert new.name == "Work"
    assert new.working_dir == str(directory)
    assert new.credential_id == "c-1"
    # Brand-new conversation — no resume id, no message history.
    assert new.claude_session_id is None
    assert new._message_count == 0


@pytest.mark.asyncio
async def test_archive_hides_old_session_from_list_but_keeps_db_row(manager):
    old = await _new(manager,name="Hide Me", working_dir=None)
    new = await manager.archive_session(old.id)

    listed = [s.id for s in manager.list_sessions()]
    assert old.id not in listed
    assert new.id in listed

    # DB row still present (archived=1), available via include_archived.
    all_rows = await manager.db.load_sessions(include_archived=True)
    archived_ids = [r["id"] for r in all_rows if r["archived"]]
    assert old.id in archived_ids


@pytest.mark.asyncio
async def test_archive_leaves_agent_schedules_alone(manager):
    """Schedule *ownership* is agent-scoped (agent-refactor.md §5.2), so
    archiving a session doesn't change a schedule's owning agent. The schedule
    here has no origin session, so it isn't repointed either — that path is
    covered by test_archive_repoints_origin_session_schedules."""
    old = await _new(manager, name="Auto", working_dir=None)
    agent_id = old.agent_id
    await manager.db.save_schedule(
        schedule_id="s-1",
        agent_id=agent_id,
        name="ping",
        prompt="hi",
        interval_seconds=300,
        created_at="2026-01-01T00:00:00+00:00",
    )
    new = await manager.archive_session(old.id)

    # Schedule still owned by the same agent, untouched.
    schedules = await manager.db.load_schedules()
    assert schedules[0]["agent_id"] == agent_id

    # The replacement thread is under the same agent.
    assert new.agent_id == agent_id


@pytest.mark.asyncio
@pytest.mark.parametrize("closing", [False, True])
async def test_archive_repoints_origin_session_schedules(manager, monkeypatch, closing):
    """An admitted archive retains its origin update through Runner shutdown.

    The original trigger keeps cadence; a real later fire reads its successor
    from the DB, including after the Runner was restarted.
    """
    from server.knowledge import encode, for_account
    from server.scheduler import ScheduleRunner

    old = await _new(manager, name="Origin", working_dir=None)
    agent_id = old.agent_id
    runner = ScheduleRunner(manager, manager.db)
    context = for_account(session_id=old.id, request_id="schedule", user_id=old.user_id, prompt="summarize")

    await manager.db.save_schedule(
        schedule_id="s-origin",
        agent_id=agent_id,
        name="digest",
        prompt="summarize",
        interval_seconds=300,
        created_at="2026-01-01T00:00:00+00:00",
        origin_session_id=old.id,
        knowledge_context=encode(context),
    )
    # An unrelated schedule (no origin) must be left alone.
    await manager.db.save_schedule(
        schedule_id="s-other",
        agent_id=agent_id,
        name="other",
        prompt="ping",
        interval_seconds=300,
        created_at="2026-01-01T00:00:00+00:00",
        knowledge_context=encode(context),
    )
    await runner.initialize(paused=True)
    original_next = runner.next_run_at("s-origin")
    cleaning, release, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    started_sessions = []

    class Run(_QueuedRun):
        async def start(self, *args, **kwargs):
            await super().start(*args, **kwargs)
            started.set()

        async def stop(self):
            cleaning.set()
            await release.wait()
            await super().stop()

    def make_run(session, *args, **kwargs):
        started_sessions.append(session.id)
        return Run([]) if session.id == old.id else _QueuedRun([], release=release)

    monkeypatch.setattr(manager, "_make_run", make_run)
    original = await manager.start_message(old.id, "held")
    await asyncio.wait_for(started.wait(), 3)
    archiving = asyncio.create_task(manager.archive_session(old.id))
    try:
        await asyncio.wait_for(cleaning.wait(), 3)
        assert not archiving.done()
        if closing:
            runner.begin_shutdown()
            await runner.shutdown()
        release.set()
        new = await archiving
        await original.wait()
        rows = {r["id"]: r for r in await manager.db.load_schedules()}
        assert rows["s-origin"]["origin_session_id"] == new.id
        assert rows["s-other"]["origin_session_id"] is None
        if closing:
            runner = ScheduleRunner(manager, manager.db)
            await runner.initialize(paused=True)
        else:
            assert runner.next_run_at("s-origin") == original_next
        await runner._fire("s-origin")
        await manager.await_session_execution(new.id)
        assert new.id in started_sessions
        messages = await manager.db.load_messages(new.id)
        assert any(message["role"] == "user" and message["content"].endswith("summarize") for message in messages)
    finally:
        release.set()
        await asyncio.gather(archiving, return_exceptions=True)
        await runner.shutdown()


@pytest.mark.asyncio
async def test_archive_repoints_origin_schedules_without_runner(manager):
    """Origin updates belong to archive even without a wired ScheduleRunner."""
    old = await _new(manager, name="Origin2", working_dir=None)
    await manager.db.save_schedule(
        schedule_id="s-db-only",
        agent_id=old.agent_id,
        name="digest",
        prompt="summarize",
        interval_seconds=300,
        created_at="2026-01-01T00:00:00+00:00",
        origin_session_id=old.id,
    )

    new = await manager.archive_session(old.id)

    rows = await manager.db.load_schedules()
    assert rows[0]["origin_session_id"] == new.id


@pytest.mark.asyncio
async def test_archive_unknown_session_raises(manager):
    with pytest.raises(ValueError):
        await manager.archive_session("does-not-exist")


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    ("gpt-5.4", "high", "fast"),
    (None, None, None),
])
async def test_archive_broadcasts_session_archived_event(manager, overrides):
    received: list[dict] = []
    manager.on_broadcast("test", lambda m: asyncio.sleep(0, result=received.append(m)))

    old = await _new(manager,name="X", working_dir=None)
    old.model, old.reasoning_effort, old.service_tier = overrides
    await manager.db.update_session_field(
        old.id, model=old.model, reasoning_effort=old.reasoning_effort,
        service_tier=old.service_tier,
    )
    new = await manager.archive_session(old.id)

    archived_evts = [m for m in received if m.get("type") == "session_archived"]
    assert len(archived_evts) == 1
    assert archived_evts[0]["old_session_id"] == old.id
    assert archived_evts[0]["new_session_id"] == new.id
    assert archived_evts[0]["name"] == "X"
    saved = next(row for row in await manager.db.load_sessions() if row["id"] == new.id)
    for field, expected in zip(("model", "reasoning_effort", "service_tier"), overrides, strict=True):
        assert archived_evts[0][field] == getattr(new, field) == saved[field] == expected


# --------------------------------------------------------------- auth-expiry detection
# Reactive mid-turn 401 → flag the bound credential needs_reconnect and emit a
# re-authorize prompt (harness-credential-reauth.md §4).


class _FakeBackend(FakeRunBase):
    """Stand-in HarnessRun: yields a scripted event list and exposes a fixed
    stderr_text, so the request's auth-expiry classifier can be exercised
    without a real CLI subprocess."""

    def __init__(self, events, stderr_text="", reusable=False):
        self._events = list(events)
        self.stderr_text = stderr_text
        # Opt-in: whether this stand-in claims its process survives the turn.
        self.reusable = reusable
        self.sent_turns: list[str] = []

    async def start(self, *args, **kwargs):
        pass

    def stream(self):
        async def _gen():
            for e in self._events:
                yield e
        return _gen()

    async def stop(self):
        pass


async def _bind_credential(manager, backend, secret="sk-ant-x"):
    from datetime import datetime

    from server.crypto import encrypt

    owner = (await manager.db.get_system_agent())["user_id"]
    cid = f"cred-{backend}"
    await manager.db.save_credential(
        credential_id=cid,
        backend=backend,
        label="Bound",
        auth_type="api_key" if backend == "claude-code" else "oauth",
        secret_encrypted=encrypt(secret, await UserManager(manager.db).data_key_for(owner)),
        created_at=datetime.now(UTC).isoformat(),
        user_id=owner,
    )
    if backend == "codex":
        from pathlib import Path

        from server.codex_login import codex_home_for

        home = Path(codex_home_for(cid))
        home.mkdir(parents=True, exist_ok=True)
        (home / "auth.json").write_text("{}")
    return cid


@pytest.mark.asyncio
async def test_unattributed_auth_stderr_does_not_invalidate_current_credential(manager, monkeypatch):
    """A process diagnostic without a request identity is not a rejection receipt."""
    cid = await _bind_credential(manager, "claude-code")
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(
        agent["id"], "Auth", None, credential_id=cid, backend="claude-code"
    )
    fake = _FakeBackend(
        events=[],
        stderr_text="Failed to authenticate. API Error: 401 Invalid authentication credentials",
    )
    monkeypatch.setattr(manager, "_make_run", lambda *a, **k: fake)

    events = await _request_events(manager, session, "hi")

    auth = [e for e in events if e.get("code") == "auth_expired"]
    assert auth == []
    row = await manager.db.get_credential(cid)
    assert row["needs_reconnect"] is False
    assert row["status"] == "active"


@pytest.mark.asyncio
async def test_codex_auth_error_without_material_proof_keeps_current_login(manager, monkeypatch):
    """Codex surfaces auth failure as a `turn.failed` → is_error result whose
    content carries the message; detection reads it from the event stream."""
    from server.harness import HarnessEvent

    cid = await _bind_credential(manager, "codex", secret="/tmp/home")
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(
        agent["id"], "AuthCodex", None, credential_id=cid, backend="codex"
    )
    fake = _FakeBackend(
        events=[
            HarnessEvent(
                type="result", is_error=True, content="stream error: 401 Unauthorized"
            )
        ]
    )
    monkeypatch.setattr(manager, "_make_run", lambda *a, **k: fake)

    events = await _request_events(manager, session, "hi")

    assert any(e.get("code") == "auth_failed" for e in events)
    row = await manager.db.get_credential(cid)
    assert row["needs_reconnect"] is False


@pytest.mark.asyncio
async def test_clean_turn_does_not_flag_credential(manager, monkeypatch):
    """A successful turn must never flag the credential, even if stderr happens
    to mention a 401 (e.g. a tool the model ran hit one)."""
    from server.harness import HarnessEvent

    cid = await _bind_credential(manager, "claude-code")
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(
        agent["id"], "Clean", None, credential_id=cid, backend="claude-code"
    )
    fake = _FakeBackend(
        events=[HarnessEvent(type="result", is_error=False, session_id="s1")],
        stderr_text="a tool logged: API Error: 401 (from some third-party API)",
    )
    monkeypatch.setattr(manager, "_make_run", lambda *a, **k: fake)

    events = await _request_events(manager, session, "hi")

    assert not any(e.get("code") == "auth_expired" for e in events)
    row = await manager.db.get_credential(cid)
    assert row["needs_reconnect"] is False


@pytest.mark.asyncio
async def test_failed_codex_turn_with_tool_unauthorized_does_not_flag(manager, monkeypatch):
    """Vera review: a Codex turn that FAILS for a non-auth reason whose text
    merely contains "Unauthorized" (e.g. an MCP/connector 401 bubbling up) must
    NOT flag the harness credential — only auth-specific phrases do."""
    from server.harness import HarnessEvent

    cid = await _bind_credential(manager, "codex", secret="/tmp/home")
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(
        agent["id"], "ToolFail", None, credential_id=cid, backend="codex"
    )
    fake = _FakeBackend(
        events=[
            HarnessEvent(
                type="result",
                is_error=True,
                content="turn failed: MCP server github returned Unauthorized",
            )
        ]
    )
    monkeypatch.setattr(manager, "_make_run", lambda *a, **k: fake)

    events = await _request_events(manager, session, "hi")

    assert not any(e.get("code") == "auth_expired" for e in events)
    row = await manager.db.get_credential(cid)
    assert row["needs_reconnect"] is False


# --------------------------------------------------------------- transient retry
# Provider-reliability failures require evidence before bounded recovery
# (harness-transient-retry.md §4).


class _SeqBackend(FakeRunBase):
    """One scripted attempt: records the prompt it was started with, yields a
    fixed event list, exposes stderr_text."""

    def __init__(self, events, stderr_text="", *, preparation_error=None):
        self._events = list(events)
        self.stderr_text = stderr_text
        self.started_with = None
        self.started_resume = None
        self.preparation_error = preparation_error

    async def start(self, prompt, working_dir=None, resume_id=None, **kwargs):
        self.started_with = prompt
        self.started_resume = resume_id
        if self.preparation_error:
            from server.harness.events import HarnessPreparationError
            raise HarnessPreparationError(self.preparation_error)

    def stream(self):
        async def _gen():
            for e in self._events:
                yield e
        return _gen()

    async def stop(self):
        pass


def _seq_factory(backends):
    it = iter(backends)

    def _factory(*args, **kwargs):
        return next(it)

    return _factory


@pytest.mark.asyncio
async def test_transient_error_without_submission_proof_stops(manager, monkeypatch):
    from server.harness import HarnessEvent

    monkeypatch.setattr(type(manager), "_TRANSIENT_RETRY_BASE_DELAY", 0.0)
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(
        agent["id"], "Transient", None, backend="claude-code"
    )
    attempt1 = _SeqBackend(
        events=[HarnessEvent(type="result", is_error=True,
                             content="API Error: 529 Overloaded")]
    )
    attempt2 = _SeqBackend(
        events=[
            HarnessEvent(type="text", content="hello at last"),
            HarnessEvent(type="result", is_error=False, session_id="s1"),
        ]
    )
    monkeypatch.setattr(manager, "_make_run", _seq_factory([attempt1, attempt2]))

    events = await _request_events(manager, session, "hi")

    assert not any(e.get("code") == "transient_retry" for e in events)
    assert any(e.get("code") == "transient_exhausted" for e in events)
    assert attempt2.started_with is None


@pytest.mark.asyncio
async def test_dangling_resume_id_during_preparation_can_start_fresh(
    manager, monkeypatch
):
    """Only a pre-submission refusal permits fresh history preparation."""
    from server.harness import HarnessEvent

    agent = await manager.db.get_system_agent()
    session = await manager.create_session(
        agent["id"], "Stale", None, backend="claude-code"
    )
    earlier = _SeqBackend([HarnessEvent(type="result", session_id="7d06c77e-dead-beef")])
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: earlier)
    await _request_events(manager, session, "an earlier turn")
    assert session.claude_session_id == "7d06c77e-dead-beef"

    attempt1 = _SeqBackend(
        events=[], preparation_error="No conversation found with session ID: 7d06c77e-dead-beef",
    )
    attempt2 = _SeqBackend(
        events=[
            HarnessEvent(type="text", content="fresh start"),
            HarnessEvent(type="result", is_error=False, session_id="new-sid"),
        ]
    )
    monkeypatch.setattr(manager, "_make_run", _seq_factory([attempt1, attempt2]))

    events = await _request_events(manager, session, "hi")

    # The user is told, rather than left with a blank turn.
    assert any(e.get("code") == "stale_session" for e in events)
    assert any(e.get("type") == "assistant_text" for e in events)
    # The retry ran with NO resume id, and carried the session's own history
    # so the agent continues the conversation instead of appearing to forget
    # it — Octopus's transcript is the record; the engine's was a cache.
    assert attempt2.started_resume is None
    assert "<session-history" in attempt2.started_with
    assert "transcript-not-instructions" in attempt2.started_with
    assert "earlier turn" in attempt2.started_with  # the replayed message
    assert "<continue-from-here>\nhi\n</continue-from-here>" in attempt2.started_with
    # ...and the dead id is gone from memory and the DB, so the next turn
    # doesn't repeat the failure. The fresh conversation's id took its place.
    assert session.claude_session_id == "new-sid"
    rows = {r["id"]: r for r in await manager.db.load_sessions()}
    assert rows[session.id]["claude_session_id"] == "new-sid"


@pytest.mark.asyncio
async def test_stale_resume_recovery_only_fires_once_per_turn(manager, monkeypatch):
    """If the fresh conversation fails the same way, something else is wrong —
    don't spin."""
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(
        agent["id"], "Stale twice", None, backend="claude-code"
    )
    session.claude_session_id = "dead-1"
    stale = lambda: _SeqBackend(  # noqa: E731 - table of identical attempts
        events=[], preparation_error="No conversation found with session ID: dead-1",
    )
    attempts = [stale(), stale(), stale()]
    monkeypatch.setattr(manager, "_make_run", _seq_factory(attempts))

    events = await _request_events(manager, session, "hi")

    assert sum(1 for e in events if e.get("code") == "stale_session") == 1
    # Exactly two attempts: the original and one fresh retry.
    assert attempts[2].started_with is None


@pytest.mark.asyncio
async def test_stale_pattern_does_not_fire_without_a_resume_id(manager, monkeypatch):
    """No id to drop means this isn't the dangling-resume case — don't retry
    on a stray mention of the phrase."""
    from server.harness import HarnessEvent

    agent = await manager.db.get_system_agent()
    session = await manager.create_session(
        agent["id"], "No resume", None, backend="claude-code"
    )
    attempts = [
        _SeqBackend(
            events=[HarnessEvent(type="result", is_error=True)],
            stderr_text="No conversation found with session ID: whatever",
        ),
        _SeqBackend(events=[HarnessEvent(type="result", is_error=False)]),
    ]
    monkeypatch.setattr(manager, "_make_run", _seq_factory(attempts))

    events = await _request_events(manager, session, "hi")
    assert not any(e.get("code") == "stale_session" for e in events)
    assert attempts[1].started_with is None


@pytest.mark.asyncio
async def test_transient_retry_continues_captured_conversation_without_output(manager, monkeypatch):
    """A captured conversation permits continuation, never original replay."""
    from server.harness import HarnessEvent

    monkeypatch.setattr(type(manager), "_TRANSIENT_RETRY_BASE_DELAY", 0.0)
    agent = await manager.db.get_system_agent()
    # Fresh session: no resume id at turn start.
    session = await manager.create_session(
        agent["id"], "TransientResume", None, backend="claude-code"
    )
    assert session.claude_session_id is None
    attempt1 = _SeqBackend(
        events=[
            HarnessEvent(type="session_started", session_id="failed-new-id"),
            HarnessEvent(type="result", is_error=True,
                         content="API Error: 529 Overloaded"),
        ]
    )
    attempt2 = _SeqBackend(
        events=[HarnessEvent(type="result", is_error=False, session_id="good-id")]
    )
    monkeypatch.setattr(manager, "_make_run", _seq_factory([attempt1, attempt2]))

    events = await _request_events(manager, session, "hi")

    assert any(e.get("code") == "transient_retry" for e in events)
    assert attempt2.started_resume == "failed-new-id"
    assert attempt2.started_with.startswith("Continue the user's")


@pytest.mark.asyncio
async def test_transient_error_bounded_then_surfaces(manager, monkeypatch):
    from server.harness import HarnessEvent

    monkeypatch.setattr(type(manager), "_TRANSIENT_RETRY_BASE_DELAY", 0.0)
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(
        agent["id"], "TransientBounded", None, backend="claude-code"
    )
    # initial + 2 retries all fail transiently → exhausted on the 3rd.
    fails = [
        _SeqBackend(events=[HarnessEvent(type="session_started", session_id="saved-thread"),
                            HarnessEvent(type="result", is_error=True,
                                         content="API Error: 503 Service Unavailable")])
        for _ in range(3)
    ]
    monkeypatch.setattr(manager, "_make_run", _seq_factory(fails))

    events = await _request_events(manager, session, "hi")

    retries = [e for e in events if e.get("code") == "transient_retry"]
    assert len(retries) == manager._MAX_TRANSIENT_RETRIES
    assert any(e.get("code") == "transient_exhausted" for e in events)


@pytest.mark.asyncio
async def test_transient_error_after_output_resumes_with_continue(manager, monkeypatch):
    """A transient failure AFTER output (the common mid-turn throttle) must
    retry by RESUMING with "continue" from the captured resume id — not re-run
    the prompt (which would duplicate) and not stop the session."""
    from server.harness import HarnessEvent

    monkeypatch.setattr(type(manager), "_TRANSIENT_RETRY_BASE_DELAY", 0.0)
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(
        agent["id"], "TransientAfterOutput", None, backend="claude-code"
    )
    attempt1 = _SeqBackend(events=[
        HarnessEvent(type="session_started", session_id="sid1"),
        HarnessEvent(type="tool_use", tool_name="Bash", tool_input={}, tool_use_id="t1"),
        HarnessEvent(type="result", is_error=True,
                     content="API Error: Server is temporarily limiting requests "
                             "(not your usage limit) · Rate limited"),
    ])
    attempt2 = _SeqBackend(events=[HarnessEvent(type="result", is_error=False, session_id="sid1")])
    monkeypatch.setattr(manager, "_make_run", _seq_factory([attempt1, attempt2]))

    events = await _request_events(manager, session, "hi")

    assert any(e.get("code") == "transient_retry" for e in events)
    assert not any(e.get("code") == "transient_exhausted" for e in events)
    # The retry RESUMED the conversation: "continue" against the captured id,
    # not a re-run of the original prompt.
    assert attempt2.started_with.startswith("Continue the user")
    assert attempt2.started_resume == "sid1"


# --------------------------------------------------------------- turn watchdog
# turn-safety.md §3: a turn that goes silent (idle) or runs too long is stopped
# and surfaced, instead of hanging forever (the deep-research wedge).


class _StallBackend(FakeRunBase):
    """Yields `pre` events then blocks forever — until stop() unblocks it.
    Models a wedged turn (no terminal result)."""

    def __init__(self, pre=None):
        self.pre = list(pre or [])
        self.stopped = False
        self.stderr_text = ""
        self._unblock = asyncio.Event()

    async def start(self, prompt, working_dir=None, resume_id=None, **kwargs):
        self.started_with = prompt
        self.started_resume = resume_id

    def stream(self):
        async def _gen():
            for e in self.pre:
                yield e
            await self._unblock.wait()  # stall until stop()
        return _gen()

    async def stop(self):
        self.stopped = True
        self._unblock.set()


@pytest.mark.asyncio
async def test_turn_idle_timeout_stops_and_surfaces(manager, monkeypatch):
    from server.config import settings as cfg

    monkeypatch.setattr(cfg, "turn_idle_timeout_seconds", 0.2)
    monkeypatch.setattr(cfg, "turn_max_seconds", 0)
    monkeypatch.setattr(cfg, "turn_idle_max_retries", 0)
    session = await _new(manager, "Stall")
    backend = _StallBackend()
    monkeypatch.setattr(manager, "_make_run", lambda *a, **k: backend)

    events = await _request_events(manager, session, "hi")

    assert any(e.get("code") == "turn_timeout" for e in events)
    assert backend.stopped is True


@pytest.mark.asyncio
async def test_fast_turn_not_killed_by_watchdog(manager, monkeypatch):
    from server.config import settings as cfg
    from server.harness import HarnessEvent

    monkeypatch.setattr(cfg, "turn_idle_timeout_seconds", 5)
    session = await _new(manager, "Fast")
    backend = _FakeBackend(
        events=[HarnessEvent(type="result", is_error=False, session_id="s1")]
    )
    monkeypatch.setattr(manager, "_make_run", lambda *a, **k: backend)

    events = await _request_events(manager, session, "hi")

    assert not any(e.get("code") == "turn_timeout" for e in events)


async def test_disabled_overall_cap_does_not_stop_a_long_active_turn(manager, monkeypatch):
    import time
    from types import SimpleNamespace

    from server.config import settings as cfg
    from server.sessions import owner_runtime, runtime_types, turns

    monkeypatch.setattr(cfg, "turn_idle_timeout_seconds", 0.3)
    monkeypatch.setattr(cfg, "turn_max_seconds", 0)
    clock = {"now": time.monotonic()}
    for module in (turns, owner_runtime, runtime_types):
        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    backend = _DripBackend(interval=0.02)
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: backend)
    session = await _new(manager, "Active for an hour")
    activity = asyncio.Queue()

    async def observe(event):
        if event.get("session_id") == session.id and event["type"] == "assistant_text":
            activity.put_nowait(event)

    manager.on_broadcast("long-active-turn", observe)
    receipt = await manager.start_message(session.id, "keep working")
    try:
        await asyncio.wait_for(activity.get(), 2)
        clock["now"] += 3600.0
        await asyncio.wait_for(activity.get(), 2)
        await asyncio.sleep(0.1)
        assert not backend.stopped
        assert manager.dispatch_snapshot(session.id)["active_request_id"] == receipt.request_id
    finally:
        await manager.interrupt(session.id)
    assert (await asyncio.wait_for(receipt.wait(), 2)).outcome.status == "interrupted"


@pytest.mark.asyncio
async def test_turn_timeout_does_not_trigger_premature_exit_respawn(manager, monkeypatch):
    """A timed-out turn must return BEFORE the premature-exit "continue" respawn
    — even with the respawn preconditions (tool_use seen + a resume id) met."""
    from server.config import settings as cfg
    from server.harness import HarnessEvent

    monkeypatch.setattr(cfg, "turn_idle_timeout_seconds", 0.2)
    monkeypatch.setattr(cfg, "turn_max_seconds", 0)
    monkeypatch.setattr(cfg, "turn_idle_max_retries", 0)
    session = await _new(manager, "StallTool")  # claude-code → premature recovery on
    backend = _StallBackend(pre=[
        HarnessEvent(type="session_started", session_id="resume-1"),
        HarnessEvent(type="tool_use", tool_name="Bash", tool_input={}, tool_use_id="t1"),
    ])
    calls = {"n": 0}

    def factory(*a, **k):
        calls["n"] += 1
        return backend

    monkeypatch.setattr(manager, "_make_run", factory)

    events = await _request_events(manager, session, "hi")

    assert any(e.get("code") == "turn_timeout" for e in events)
    assert calls["n"] == 1  # not respawned with "continue"


@pytest.mark.parametrize("backend_kind", ["claude-code", "codex"])
@pytest.mark.parametrize("original_resume", [None, "original-thread"])
async def test_idle_recovery_continues_without_duplicate_user_messages(
    manager, monkeypatch, backend_kind, original_resume
):
    from server.config import settings as cfg
    from server.harness import HarnessEvent

    monkeypatch.setattr(cfg, "turn_idle_timeout_seconds", 0.1)
    monkeypatch.setattr(cfg, "turn_max_seconds", 0)
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "Recover", backend=backend_kind)
    session.claude_session_id = original_resume
    first = _StallBackend([HarnessEvent(type="session_started", session_id="failed-thread")])
    second = _SeqBackend([
        HarnessEvent(type="text", content="recovered"),
        HarnessEvent(type="result", session_id="new-thread"),
    ])
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first, second]))

    events = await _request_events(manager, session, "original request")

    retry = next(e for e in events if e.get("code") == "turn_timeout_retry")
    assert retry["retrying"] is True
    assert not any(e.get("code") == "turn_timeout" for e in events)
    assert "Continue the user's unfinished request" in second.started_with
    assert second.started_resume == "failed-thread"
    assert first.stopped
    messages = await manager.db.load_messages(session.id)
    assert [m["content"] for m in messages if m["role"] == "user"] == ["original request"]
    assert any(m["type"] == "recovery" and m["content"]["message"] == retry["message"] for m in messages)


@pytest.mark.parametrize("backend_kind", ["claude-code", "codex"])
@pytest.mark.parametrize("output_kind", ["tool_use", "text_delta"])
async def test_idle_recovery_resumes_after_output(
    manager, monkeypatch, backend_kind, output_kind
):
    from server.config import settings as cfg
    from server.harness import HarnessEvent

    monkeypatch.setattr(cfg, "turn_idle_timeout_seconds", 0.1)
    monkeypatch.setattr(cfg, "turn_max_seconds", 0)
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "Resume", backend=backend_kind)
    first = _StallBackend([
        HarnessEvent(type="session_started", session_id="saved-thread"),
        HarnessEvent(type=output_kind, content="partial", tool_name="Bash", tool_use_id="t1"),
    ])
    second = _SeqBackend([HarnessEvent(type="result", session_id="saved-thread")])
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first, second]))

    events = await _request_events(manager, session, "original request")

    assert any(e.get("code") == "turn_timeout_retry" for e in events)
    assert second.started_resume == "saved-thread"
    assert "Continue the user's unfinished request" in second.started_with
    assert "Check existing files and tool outcomes" in second.started_with
    assert second.started_with != "original request"


async def test_idle_recovery_has_a_per_turn_budget(manager, monkeypatch):
    from server.config import settings as cfg
    from server.harness import HarnessEvent

    monkeypatch.setattr(cfg, "turn_idle_timeout_seconds", 0.1)
    monkeypatch.setattr(cfg, "turn_max_seconds", 0)
    monkeypatch.setattr(cfg, "turn_idle_max_retries", 2)
    session = await _new(manager, "Budget")
    stalls = [_StallBackend([HarnessEvent(type="session_started", session_id="saved-thread")]) for _ in range(3)]
    success = _SeqBackend([HarnessEvent(type="result", session_id="done")])
    monkeypatch.setattr(manager, "_make_run", _seq_factory([*stalls, success]))

    events = await _request_events(manager, session, "first request")

    assert len([e for e in events if e.get("code") == "turn_timeout_retry"]) == 2
    final = [e for e in events if e.get("code") == "turn_timeout"]
    assert len(final) == 1 and not final[0].get("retrying")
    assert "after 2 retries" in final[0]["message"]
    assert all(b.stopped for b in stalls)
    # A failed logical turn leaves the session usable for the next request.
    next_receipt = await manager.start_message(session.id, "next request")
    assert (await asyncio.wait_for(next_receipt.wait(), 2)).outcome.status == "success"
    assert success.started_with == "next request"


@pytest.mark.parametrize("second_failure", ["idle", "transient"])
async def test_recovery_remembers_output_from_earlier_attempts(manager, monkeypatch, second_failure):
    from server.config import settings as cfg
    from server.harness import HarnessEvent

    monkeypatch.setattr(cfg, "turn_idle_timeout_seconds", 0.1)
    monkeypatch.setattr(cfg, "turn_max_seconds", 0)
    monkeypatch.setattr(type(manager), "_TRANSIENT_RETRY_BASE_DELAY", 0.0)
    session = await _new(manager, "MixedRecovery")
    first = _StallBackend([
        HarnessEvent(type="session_started", session_id="saved-thread"),
        HarnessEvent(type="tool_use", tool_name="Write", tool_use_id="write-1"),
    ])
    second = _StallBackend() if second_failure == "idle" else _SeqBackend([
        HarnessEvent(type="result", is_error=True, content="API Error: 529 Overloaded")
    ])
    final = _SeqBackend([HarnessEvent(type="result", session_id="saved-thread")])
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first, second, final]))

    events = await _request_events(manager, session, "original request")

    assert not any(e.get("code") == "turn_timeout" for e in events)
    assert final.started_resume == "saved-thread"
    assert final.started_with != "original request"


async def test_idle_recovery_does_not_replay_output_without_resume_id(manager, monkeypatch):
    from server.config import settings as cfg
    from server.harness import HarnessEvent

    monkeypatch.setattr(cfg, "turn_idle_timeout_seconds", 0.1)
    monkeypatch.setattr(cfg, "turn_max_seconds", 0)
    session = await _new(manager, "NoResume")
    first = _StallBackend([HarnessEvent(type="text_delta", content="partial")])
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first]))

    events = await _request_events(manager, session, "request")

    assert any(e.get("code") == "turn_timeout" for e in events)
    assert not any(e.get("code") == "turn_timeout_retry" for e in events)


async def test_overall_timeout_bounds_all_recovery_attempts(manager, monkeypatch):
    from server.config import settings as cfg
    from server.harness import HarnessEvent

    monkeypatch.setattr(cfg, "turn_idle_timeout_seconds", 0.1)
    monkeypatch.setattr(cfg, "turn_max_seconds", 0.35)
    monkeypatch.setattr(cfg, "turn_idle_max_retries", 10)
    session = await _new(manager, "TotalBudget")
    monkeypatch.setattr(manager, "_make_run", lambda *a, **k: _StallBackend([
        HarnessEvent(type="session_started", session_id="saved-thread")]))

    events = await _request_events(manager, session, "request")

    retries = [e for e in events if e.get("code") == "turn_timeout_retry"]
    assert 0 < len(retries) < 10
    final = next(e for e in events if e.get("code") == "turn_timeout")
    assert "maximum duration" in final["message"]


async def test_user_interrupt_stops_idle_recovery(manager, monkeypatch):
    from server.config import settings as cfg
    from server.harness import HarnessEvent

    monkeypatch.setattr(cfg, "turn_idle_timeout_seconds", 0.2)
    monkeypatch.setattr(cfg, "turn_max_seconds", 0)
    session = await _new(manager, "CancelRecovery")
    started_second = asyncio.Event()
    first = _StallBackend([HarnessEvent(type="session_started", session_id="saved-thread")])
    second = _StallBackend([HarnessEvent(type="text", content="second attempt")])
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first, second]))

    async def watch(event):
        if event.get("content") == "second attempt":
            started_second.set()

    manager.on_broadcast("second-attempt", watch)
    receipt = await manager.start_message(session.id, "request")
    await asyncio.wait_for(started_second.wait(), timeout=1)
    assert await manager.interrupt(session.id)
    assert (await asyncio.wait_for(receipt.wait(), 2)).outcome.status == "interrupted"
    messages = await manager.db.load_messages(session.id)
    assert sum(m["type"] == "result" and m["content"]["status"] == "interrupted" for m in messages) == 1
    assert session.status == SessionStatus.idle


class _DripBackend(FakeRunBase):
    """Yields a text event every `interval`s forever (so idle keeps resetting),
    until stop() unblocks it — to prove the OVERALL cap trips on a steadily-
    alive turn."""

    def __init__(self, interval=0.05):
        self.interval = interval
        self.stopped = False
        self.stderr_text = ""
        self._unblock = asyncio.Event()

    async def start(self, *args, **kwargs):
        pass

    def stream(self):
        from server.harness import HarnessEvent

        async def _gen():
            while not self._unblock.is_set():
                yield HarnessEvent(type="text", content="…")
                try:
                    await asyncio.wait_for(self._unblock.wait(), timeout=self.interval)
                except TimeoutError:
                    pass
        return _gen()

    async def stop(self):
        self.stopped = True
        self._unblock.set()


@pytest.mark.asyncio
async def test_turn_overall_cap_trips_even_with_steady_events(manager, monkeypatch):
    from server.config import settings as cfg

    monkeypatch.setattr(cfg, "turn_idle_timeout_seconds", 5)   # idle never trips
    monkeypatch.setattr(cfg, "turn_max_seconds", 0.3)          # overall does
    session = await _new(manager, "Drip")
    backend = _DripBackend()
    monkeypatch.setattr(manager, "_make_run", lambda *a, **k: backend)

    events = await _request_events(manager, session, "hi")

    timeout = next((e for e in events if e.get("code") == "turn_timeout"), None)
    assert timeout is not None
    assert "maximum duration" in timeout["message"]  # the overall-cap message
    assert backend.stopped is True


# --------------------------------------------------------------------------- #
# Streamed token deltas (inline-steering.md §4 S1)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("field, before, after", [
    ("reasoning_effort", "low", "high"), ("service_tier", "default", "fast"),
])
async def test_setting_saved_during_recovery_applies_on_the_next_turn(manager, monkeypatch, field, before, after):
    from server.harness import HarnessEvent

    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", 0)
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "Reasoning", backend="codex")
    settings_seen = []
    attempt_count = 0

    class Scripted(_FakeBackend):
        def stream(self):
            async def events():
                nonlocal attempt_count
                attempt_count += 1
                if attempt_count == 1:
                    changed = await client.patch(f"/api/sessions/{session.id}", json={field: after})
                    assert changed.status_code == 200, changed.text
                    yield HarnessEvent(type="session_started", session_id="saved-thread")
                    yield HarnessEvent(type="error", content="stream disconnected", is_error=True)
                    yield HarnessEvent(type="result", is_error=True)
                else:
                    yield HarnessEvent(type="text", content="done")
                    yield HarnessEvent(type="result", is_error=False)
            return events()

    def factory(configured, agent=None, connectors=None):
        settings_seen.append(getattr(configured, field))
        return Scripted([])

    monkeypatch.setattr(manager, "_make_run", factory)
    async with await _session_client(manager, session) as client:
        configured = await client.patch(f"/api/sessions/{session.id}", json={field: before})
        assert configured.status_code == 200, configured.text
        events = await _request_events(manager, session, "work")
        assert settings_seen == [before, before], events
        _ = await _request_events(manager, session, "next")
        assert settings_seen == [before, before, after]


def test_text_delta_broadcasts_but_never_persists():
    """Deltas are a UI affordance, not history. The completed block that
    follows is the message; persisting deltas too would duplicate every answer
    in the transcript."""
    from server.harness.events import HarnessEvent

    mgr = SessionManager()
    ev = HarnessEvent(type="text_delta", content="partial")
    assert mgr._event_to_message_content(ev) is None
    assert mgr._event_to_ws_message("s1", ev) == {
        "type": "assistant_delta",
        "session_id": "s1",
        "content": "partial",
    }


def test_empty_text_delta_sends_no_frame():
    from server.harness.events import HarnessEvent

    mgr = SessionManager()
    assert mgr._event_to_ws_message("s1", HarnessEvent(type="text_delta", content="")) is None


def test_flush_text_deltas_joins_and_drains():
    """The coalescing buffer batches tokens into one frame and empties itself,
    so a caller can flush unconditionally without checking first."""
    mgr = SessionManager()
    buf: list[str] = []
    assert mgr._flush_text_deltas("s1", buf) is None  # nothing buffered

    buf.extend(["he", "llo", " there"])
    frame = mgr._flush_text_deltas("s1", buf)
    assert frame == {
        "type": "assistant_delta",
        "session_id": "s1",
        "content": "hello there",
    }
    assert buf == []                                   # drained in place
    assert mgr._flush_text_deltas("s1", buf) is None   # and stays drained


def test_flush_uses_one_wire_shape():
    """The flush helper must not hand-roll the frame — one definition of the
    wire format, or the two drift."""
    from server.harness.events import HarnessEvent

    mgr = SessionManager()
    buf = ["x"]
    assert mgr._flush_text_deltas("s1", buf) == mgr._event_to_ws_message(
        "s1", HarnessEvent(type="text_delta", content="x")
    )



# --------------------------------------------------------------------------- #
# Held CLI processes (inline-steering.md §7)
# --------------------------------------------------------------------------- #


class _HeldRun(_FakeBackend):
    def __init__(self, *, reusable=True, signature="same-config"):
        from server.harness import HarnessEvent

        super().__init__([HarnessEvent(type="result", session_id="held-thread")], reusable=reusable)
        self.signature = signature
        self.starts = []
        self.alive = True
        self.stops = 0
        self.retired = asyncio.Event()

    async def start(self, prompt, *args, **kwargs):
        self.starts.append(prompt)

    def spawn_signature(self, working_dir, credential):
        return self.signature

    def is_alive(self):
        return self.alive

    async def interrupt(self):
        pass

    async def stop(self):
        self.stops += 1
        self.alive = False
        self.retired.set()


async def test_non_reusable_backend_is_never_reused(manager, monkeypatch):
    session = await _new(manager, "One process per turn")
    first, second = _HeldRun(reusable=False), _HeldRun(reusable=False)
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first, second]))
    await _request_events(manager, session, "first")
    await _request_events(manager, session, "second")
    assert first.starts == ["first"] and second.starts == ["second"]
    assert first.sent_turns == [] and first.stops == 1


async def test_dead_process_is_not_reused(manager, monkeypatch):
    session = await _new(manager, "Dead held process")
    first, second = _HeldRun(), _HeldRun()
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first, second]))
    await _request_events(manager, session, "first")
    first.alive = False
    await _request_events(manager, session, "second")
    assert first.sent_turns == [] and second.starts == ["second"]


async def test_reuse_refused_when_the_spawn_config_changed(manager, monkeypatch):
    session = await _new(manager, "Changed spawn configuration")
    first, second = _HeldRun(signature="old-config"), _HeldRun(signature="new-config")
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first, second]))
    await _request_events(manager, session, "first")
    await _request_events(manager, session, "second")
    assert first.sent_turns == [] and first.stops == 1
    assert second.starts == ["second"]


async def test_reuse_accepted_when_the_config_is_unchanged(manager, monkeypatch):
    session = await _new(manager, "Unchanged spawn configuration")
    first, candidate = _HeldRun(), _HeldRun()
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first, candidate]))
    await _request_events(manager, session, "first")
    await _request_events(manager, session, "second")
    assert first.starts == ["first"] and first.sent_turns == ["second"]
    assert first.stops == 0 and candidate.starts == []


async def test_reaper_drops_idle_processes_and_keeps_fresh_ones(manager, monkeypatch):
    from types import SimpleNamespace

    from server.sessions import processes

    first, second, replacement = _HeldRun(), _HeldRun(), _HeldRun()
    stale, fresh = await _new(manager, "Stale"), await _new(manager, "Fresh")
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first, second, replacement]))
    await _request_events(manager, stale, "first")
    await _request_events(manager, fresh, "second")
    assert stale._run.held_since < fresh._run.held_since
    cutoff = (stale._run.held_since + fresh._run.held_since) / 2
    monkeypatch.setattr(processes, "time", SimpleNamespace(
        monotonic=lambda: cutoff + processes._HELD_PROCESS_IDLE_SECONDS,
    ))

    assert await manager.reap_held_processes() == 1
    assert first.stops == 1 and second.stops == 0
    assert stale._run.backend is None and fresh._run.backend is second
    await _request_events(manager, stale, "after reaping")
    assert replacement.starts == ["after reaping"]


async def test_reaper_caps_total_held_processes(manager, monkeypatch):
    from server.sessions import processes

    runs = [_HeldRun() for _ in range(processes._MAX_HELD_PROCESSES)]
    monkeypatch.setattr(manager, "_make_run", _seq_factory(runs))
    for index in range(len(runs)):
        session = await _new(manager, f"Held {index}")
        await _request_events(manager, session, f"work {index}")
    # Changing the configured pool budget exercises reaping a genuine
    # over-budget pool without constructing impossible Session/driver state.
    monkeypatch.setattr(processes, "_MAX_HELD_PROCESSES", len(runs) - 2)
    assert await manager.reap_held_processes() == 2
    assert [run.stops for run in runs] == [1, 1, 0, 0]


async def test_reaper_leaves_a_running_turn_alone(manager, monkeypatch):
    session = await _new(manager, "Active process")
    backend = _QueuedRun([])
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: backend)
    receipt = await manager.start_message(session.id, "working")
    await asyncio.wait_for(backend.started.wait(), 2)
    assert await manager.reap_held_processes() == 0
    assert backend.stops == [] and manager.dispatch_snapshot(session.id)["active_request_id"] == receipt.request_id
    await manager.interrupt(session.id)
    assert (await asyncio.wait_for(receipt.wait(), 2)).outcome.status == "interrupted"


async def test_held_cap_is_enforced_without_the_reaper(manager, monkeypatch):
    from server.session_manager import _MAX_HELD_PROCESSES

    runs = [_HeldRun() for _ in range(_MAX_HELD_PROCESSES + 4)]
    monkeypatch.setattr(manager, "_make_run", _seq_factory(runs))
    sessions = []
    for index in range(len(runs)):
        session = await _new(manager, f"Pool {index}")
        sessions.append(session)
        await _request_events(manager, session, f"request {index}")

    # Completion of this request does not stand in for another Session's
    # automatic retirement. Observe the actual external stop acknowledgments.
    await asyncio.wait_for(asyncio.gather(
        *(run.retired.wait() for run in runs[:-_MAX_HELD_PROCESSES]),
    ), 2)
    assert sum(run.is_alive() for run in runs) == _MAX_HELD_PROCESSES
    assert runs[-1].stops == 0 and sessions[-1]._run.backend is runs[-1]
    assert runs[0].stops == 1 and sessions[0]._run.backend is None


async def test_held_cap_never_touches_a_running_turn(manager, monkeypatch):
    from server.session_manager import _MAX_HELD_PROCESSES

    busy = _QueuedRun([])
    idle = [_HeldRun() for _ in range(_MAX_HELD_PROCESSES + 2)]
    monkeypatch.setattr(manager, "_make_run", _seq_factory([busy, *idle]))
    session = await _new(manager, "Busy")
    receipt = await manager.start_message(session.id, "working")
    await asyncio.wait_for(busy.started.wait(), 2)
    for index in range(len(idle)):
        other = await _new(manager, f"Idle {index}")
        await _request_events(manager, other, f"request {index}")
    assert busy.stops == [] and manager.dispatch_snapshot(session.id)["active_request_id"] == receipt.request_id
    await manager.interrupt(session.id)
    assert (await asyncio.wait_for(receipt.wait(), 2)).outcome.status == "interrupted"


async def test_shutdown_sweep_is_bounded_by_a_hung_process(manager, monkeypatch):
    """Bounded shutdown reports an unconfirmed exit and retains its owner."""
    import time as _time

    from server.session_manager import _HELD_STOP_TIMEOUT

    release = asyncio.Event()

    class _HangingStop(_HeldRun):
        async def stop(self):
            await release.wait()
            await super().stop()

    backend = _HangingStop()
    session = await _new(manager, "Held shutdown")
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: backend)
    await _request_events(manager, session, "completed request")

    try:
        t0 = _time.monotonic()
        with pytest.raises(RuntimeError, match="retirement.*unconfirmed"):
            await manager.stop_all_held_processes()
        assert _time.monotonic() - t0 < _HELD_STOP_TIMEOUT + 1.0
        assert session._run.backend is None
        assert session._run.retirements
    finally:
        release.set()
        await manager.drain_turns()
    assert backend.stops == 1


async def test_interrupt_releases_the_session_handle_immediately(manager, monkeypatch):
    """An interrupted process is being torn down in the background, so the
    session must let go of it at once. With process reuse, a next turn that
    found it still briefly alive would hand its prompt to a dying CLI."""
    session = await _new(manager, "InterruptHandle")

    release_stop = asyncio.Event()
    retiring = asyncio.Event()

    class _SlowStop(_QueuedRun):
        reusable = True
        stopped = False

        async def stop(self):
            retiring.set()
            await release_stop.wait()
            _SlowStop.stopped = True
            await super().stop()

        async def interrupt(self):
            retiring.set()
            await release_stop.wait()

    backend = _SlowStop([])
    successor = _HeldRun()
    monkeypatch.setattr(manager, "_make_run", _seq_factory([backend, successor]))
    receipt = await manager.start_message(session.id, "interrupt this")
    await asyncio.wait_for(backend.started.wait(), 2)
    try:
        assert await asyncio.wait_for(manager.interrupt(session.id), 1)
        await asyncio.wait_for(retiring.wait(), 1)
        # Released synchronously, before the teardown finishes.
        assert session._run.backend is None
        assert session._run.held_since is None
        assert not _SlowStop.stopped
    finally:
        release_stop.set()
    assert (await asyncio.wait_for(receipt.wait(), 2)).outcome.status == "interrupted"
    await _request_events(manager, session, "next request")
    assert successor.starts == ["next request"] and _SlowStop.stopped


# --------------------------------------------------------------------------- #
# Inline steering (inline-steering.md §8-§9)
# --------------------------------------------------------------------------- #


class _SteerRun(_QueuedRun):
    reusable = True
    can_steer = True

    def __init__(self, session):
        super().__init__([])
        self.session_id, self.user_id = session.id, session.user_id
        self.run_lease_id, self.work_id = uuid.uuid4().hex, None
        self.frames = []
        self.frame_entered, self.frame_delivered = asyncio.Event(), asyncio.Event()
        self.release_frame = asyncio.Event()
        self.release_frame.set()
        self.sent_turns = []

    async def start(self, *args, **kwargs):
        from server.mcp_identity import register_run

        register_run(self.run_lease_id, self.session_id, self.user_id, self.work_id)
        await super().start(*args, **kwargs)

    async def stream(self):
        from server.harness import HarnessEvent

        await self.started.wait()
        yield HarnessEvent(type="text", content="Ready for a correction")
        await self.release.wait()
        yield HarnessEvent(type="result", session_id="steer-thread")

    async def send_input(self, text, input_id=None):
        self.frame_entered.set()
        await self.release_frame.wait()
        self.frames.append(text)
        self.frame_delivered.set()
        return "frame-uuid"

    async def stop(self):
        from server.mcp_identity import revoke_run

        revoke_run(self.run_lease_id)
        await super().stop()


async def _start_steerable(manager, monkeypatch, session, backend=None):
    backend = backend or _SteerRun(session)
    ready = asyncio.Event()
    key = uuid.uuid4().hex

    async def observe(event):
        if event.get("session_id") == session.id and event.get("content") == "Ready for a correction":
            ready.set()

    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: backend)
    manager.on_broadcast(key, observe)
    try:
        receipt = await manager.start_message(session.id, "original request")
        await asyncio.wait_for(ready.wait(), 2)
        return backend, receipt
    finally:
        manager.remove_broadcast(key)


async def test_steer_accepted_while_the_window_is_open(manager, monkeypatch):
    session = await _new(manager, "Steer")
    backend, original = await _start_steerable(manager, monkeypatch, session)
    correction = await manager.start_message(session.id, "not that file", steerable=True)
    assert correction.disposition == "steered"
    await asyncio.wait_for(backend.frame_delivered.wait(), 2)
    assert backend.frames == ["not that file"]
    backend.release.set()
    first_done, correction_done = await asyncio.wait_for(
        asyncio.gather(original.wait(), correction.wait()), 2,
    )
    assert first_done.outcome.status == correction_done.outcome.status == "success"
    assert correction_done.request_id == original.request_id


async def test_steer_refused_once_the_window_shuts(manager, monkeypatch):
    session = await _new(manager, "Steer after terminal")
    first, next_run = _HeldRun(), _HeldRun()
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first, next_run]))
    await _request_events(manager, session, "original request")
    late = await manager.start_message(session.id, "too late", steerable=True)
    assert late.disposition == "accepted"
    done = await asyncio.wait_for(late.wait(), 2)
    assert done.request_id == late.request_id and done.outcome.status == "success"
    assert first.sent_turns == ["too late"]


async def test_steer_backlog_is_bounded(manager, monkeypatch):
    from server.session_manager import _MAX_PENDING_STEERS

    session = await _new(manager, "Steer capacity")
    backend, original = await _start_steerable(manager, monkeypatch, session)
    entered, release = asyncio.Event(), asyncio.Event()
    spawn = asyncio.create_subprocess_exec
    gate_next = True
    processes = []

    async def capture_before_frame(*arguments, **kwargs):
        nonlocal gate_next
        process = await spawn(*arguments, **kwargs)
        if gate_next and arguments[:3] == ("git", "rev-parse", "HEAD"):
            gate_next = False
            processes.append(process)
            entered.set()
            await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_before_frame)
    corrections = []
    try:
        for index in range(_MAX_PENDING_STEERS):
            receipt = await manager.start_message(session.id, f"m{index}", steerable=True)
            assert receipt.disposition == "steered"
            corrections.append(receipt)
            if index == 0:
                await asyncio.wait_for(entered.wait(), 2)
        overflow = await manager.start_message(session.id, "overflow", steerable=True)
        assert overflow.disposition == "accepted"
        assert [item["summary"] for item in manager.dispatch_snapshot(session.id)["items"]] == ["overflow"]
        assert backend.frames == []
    finally:
        release.set()
        await manager.delete_session(session.id)
        await asyncio.gather(*(process.wait() for process in processes))
    completions = await asyncio.wait_for(asyncio.gather(
        original.wait(), *(receipt.wait() for receipt in corrections), overflow.wait(),
    ), 2)
    assert len(completions) == _MAX_PENDING_STEERS + 2


async def test_a_message_with_attachments_is_never_steered(manager, monkeypatch):
    from server.attachments import save_upload

    session = await _new(manager, "File correction")
    backend, original = await _start_steerable(manager, monkeypatch, session)
    attachment = save_upload(session.id, "evidence.txt", b"Original attachment bytes", user_id=session.user_id)
    receipt = await manager.start_message(
        session.id, "look at this", [attachment.id], steerable=True,
    )
    assert receipt.disposition == "accepted"
    assert [item["summary"] for item in manager.dispatch_snapshot(session.id)["items"]] == ["look at this"]
    assert backend.frames == []
    await manager.delete_session(session.id)
    assert (await asyncio.wait_for(original.wait(), 2)).outcome.status == "interrupted"
    assert (await asyncio.wait_for(receipt.wait(), 2)).state == "deleted"


async def test_writer_confirms_steers_before_handing_them_to_the_backend(manager, monkeypatch):
    session = await _new(manager, "Confirmed correction")
    observed = []
    steered = asyncio.Event()

    class ConfirmedRun(_SteerRun):
        async def send_input(self, text, input_id=None):
            rows = await manager.db.load_messages(session.id)
            assert [row["content"] for row in rows if row["role"] == "user"] == ["original request", text]
            assert rows[-1]["tool_input"]["request_context"]["source"] == "human"
            return await super().send_input(text, input_id)

    async def observe(event):
        if event.get("session_id") == session.id and event["type"] == "steered":
            observed.append(copy.deepcopy(event))
            steered.set()

    manager.on_broadcast("confirmed-steer", observe)
    backend, original = await _start_steerable(manager, monkeypatch, session, ConfirmedRun(session))
    correction = await manager.start_message(session.id, "stop that", steerable=True)
    assert correction.disposition == "steered"
    await asyncio.wait_for(steered.wait(), 2)
    assert backend.frames == ["stop that"]
    assert len(observed) == 1 and observed[0]["tool_input"]["request_context"]["source"] == "human"
    backend.release.set()
    assert (await asyncio.wait_for(original.wait(), 2)).outcome.status == "success"
    assert (await asyncio.wait_for(correction.wait(), 2)).request_id == original.request_id


async def test_writer_wakes_without_waiting_for_an_event(manager, monkeypatch):
    session = await _new(manager, "Silent stream correction")
    backend, original = await _start_steerable(manager, monkeypatch, session)
    # The stream remains silent after its readiness frame. No synthetic
    # backend event is used to wake the pending correction's writer.
    correction = await manager.start_message(session.id, "stop", steerable=True)
    assert correction.disposition == "steered"
    await asyncio.wait_for(backend.frame_delivered.wait(), 2)
    assert backend.frames == ["stop"] and not backend.release.is_set()
    backend.release.set()
    assert (await asyncio.wait_for(original.wait(), 2)).outcome.status == "success"
    assert (await asyncio.wait_for(correction.wait(), 2)).request_id == original.request_id


async def test_a_process_mid_turn_is_never_reused(manager, monkeypatch):
    session = await _new(manager, "One stdin writer")
    backend, original = await _start_steerable(manager, monkeypatch, session)
    candidate = _HeldRun(signature="fake-signature")
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: candidate)
    next_receipt = await manager.start_message(session.id, "next request")
    assert next_receipt.disposition == "accepted"
    assert backend.sent_turns == [] and candidate.starts == []
    assert [item["summary"] for item in manager.dispatch_snapshot(session.id)["items"]] == ["next request"]
    backend.release.set()
    assert (await asyncio.wait_for(original.wait(), 2)).outcome.status == "success"
    assert (await asyncio.wait_for(next_receipt.wait(), 2)).outcome.status == "success"
    assert backend.sent_turns == ["next request"] and candidate.starts == []


async def test_declining_reuse_stops_the_old_process(manager, monkeypatch):
    """Declining to reuse must not just overwrite the handle. The old process
    is live and ~255MB; if nothing points at it, neither the reaper nor
    shutdown can reach it, and a suite of such turns exhausts the machine —
    which is exactly how the backend suite got OOM-killed twice."""
    session = await _new(manager, "Respawn")

    stopped: list[str] = []

    class _Old(_HeldRun):
        async def stop(self):
            stopped.append("old")
            await super().stop()

    class _New(FakeRunBase):
        async def start(self, *a, **k):
            pass

        def stream(self):
            async def _gen():
                from server.harness import HarnessEvent
                yield HarnessEvent(type="result", session_id="sid")
            return _gen()

        async def stop(self):
            stopped.append("new")

    first = _Old(signature="old-config")
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first, _New()]))
    await _request_events(manager, session, "before config change")
    await _request_events(manager, session, "hello")

    assert "old" in stopped, "the orphaned process was never stopped"


async def test_injected_turns_are_never_steered(manager, monkeypatch):
    """bg-task results, delegation replies, schedules, application builds and
    research reports all arrive through `start_message`. Each is a turn in its
    own right, carrying a marker prefix the model reads as a fresh instruction
    — steering one into the middle of an unrelated turn delivers it somewhere
    it was never meant to land. Only the composer opts in."""
    session = await _new(manager, "Injected")
    backend, original = await _start_steerable(manager, monkeypatch, session)
    try:
        injected = await manager.start_message(session.id, "[bg-task-result] output here", source="bg")
        assert injected.disposition == "accepted" and backend.frames == []
        assert [item["summary"] for item in manager.dispatch_snapshot(session.id)["items"]] == ["[bg-task-result] output here"]

        # The composer's own path does steer.
        correction = await manager.start_message(session.id, "actually, stop", steerable=True)
        assert correction.disposition == "steered"
        await asyncio.wait_for(backend.frame_delivered.wait(), 2)
        assert backend.frames == ["actually, stop"]
    finally:
        await manager.delete_session(session.id)
    assert (await asyncio.wait_for(original.wait(), 2)).outcome.status == "interrupted"
    assert (await asyncio.wait_for(injected.wait(), 2)).state == "deleted"
    assert (await asyncio.wait_for(correction.wait(), 2)).request_id == original.request_id


@pytest.mark.asyncio
async def test_a_spawn_that_raises_surfaces_its_own_error(manager):
    """A CLI start failure keeps its cause in the original request outcome."""
    session = await _new(manager, "SpawnFails")

    class ExplodingBackend(FakeRunBase):
        name = "exploding"
        stopped = False

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            raise RuntimeError("no such binary")

        def stream(self):  # pragma: no cover - never reached
            async def _gen():
                yield None
            return _gen()

        async def stop(self):
            ExplodingBackend.stopped = True

    manager._make_run = lambda s, agent=None, connectors=None: ExplodingBackend()  # type: ignore[method-assign]

    events = await _request_events(manager, session, "go")
    result = next(event for event in events if event["type"] == "result")
    assert result["content"]["status"] == "failed"
    assert "no such binary" in result["content"]["message"]
    assert "UnboundLocalError" not in result["content"]["message"]
    assert [row["content"] for row in await manager.db.load_messages(session.id)
            if row["role"] == "user"] == ["go"]

    # And the teardown still ran: a spawn failure must not leak the handle.
    assert ExplodingBackend.stopped is True


@pytest.mark.asyncio
async def test_a_session_model_overrides_the_agents(manager):
    """What `/model` is for: this conversation on a different model, without
    repointing the agent every other session runs on.

    Resolution order is session, then agent, then nothing at all — and
    "nothing" has to stay possible, because that is how the CLI is told to use
    its own default rather than a name Octopus invented.
    """
    import uuid as _uuid
    from datetime import datetime

    now = datetime.now(UTC).isoformat()
    aid = _uuid.uuid4().hex[:12]
    await manager.db.save_agent(
        agent_id=aid,
        name="Tiered",
        user_id=(await manager.db.get_system_agent())["user_id"],
        created_at=now,
        updated_at=now,
        model="sonnet",
    )
    agent = await manager.db.get_agent(aid)
    session = await manager.create_session(aid, "S")

    argv, _ = manager._make_run(session, agent).build_argv("hi", session.working_dir, None)
    assert argv[argv.index("--model") + 1] == "sonnet", "the agent's model"

    session.model = "opus"
    argv, _ = manager._make_run(session, agent).build_argv("hi", session.working_dir, None)
    assert argv[argv.index("--model") + 1] == "opus", "the session's wins"

    # Neither set: no --model at all, so the CLI uses its own default.
    session.model = None
    bare = await manager.db.get_agent(aid)
    bare["model"] = None
    argv, _ = manager._make_run(session, bare).build_argv("hi", session.working_dir, None)
    assert "--model" not in argv


@pytest.mark.asyncio
async def test_a_persisted_message_records_when_it_was_written(manager, monkeypatch):
    """Hovering a message shows its time, which needs the row to carry one.

    Asserted on both paths that matter: the stored row (so a reload has it) and
    the `MessageContent` the caller goes on to broadcast (so the live event has
    it too, without a refetch).
    """
    from server.harness import HarnessEvent

    session = await _new(manager, "Timed")
    backend = _FakeBackend([HarnessEvent(type="result")])
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: backend)
    messages = []

    async def observe(event):
        if event.get("session_id") == session.id and event["type"] == "user_message":
            messages.append(copy.deepcopy(event))

    manager.on_broadcast("created-at", observe)
    await _request_events(manager, session, "hello")
    assert len(messages) == 1
    event = messages[0]
    assert event["created_at"], "the live user message carries the stored timestamp"

    rows = await manager.db.load_messages(session.id)
    stored = next(row for row in rows if row["role"] == "user")
    assert stored["content"] == "hello" and stored["seq"] == event["seq"]
    assert stored["created_at"] == event["created_at"]

    # Parses, and is UTC-aware rather than a bare local string.
    from datetime import datetime as _dt

    parsed = _dt.fromisoformat(stored["created_at"])
    assert parsed.tzinfo is not None


@pytest.mark.asyncio
async def test_the_ws_event_carries_the_row_it_was_persisted_as(manager, monkeypatch):
    """Published text identifies its original stored row; deltas invent none."""
    from server.harness import HarnessEvent

    session = await _new(manager, "Tagged")
    backend = _FakeBackend([
        HarnessEvent(type="text_delta", content="h"),
        HarnessEvent(type="text", content="hi"), HarnessEvent(type="result"),
    ])
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: backend)
    events = await _request_events(manager, session, "greet me")
    event = next(item for item in events if item["type"] == "assistant_text")
    stored = next(row for row in await manager.db.load_messages(session.id)
                  if row["role"] == "assistant" and row["type"] == "text")
    assert stored["content"] == event["content"] == "hi"
    assert event["seq"] == stored["seq"]
    assert event["created_at"] == stored["created_at"]
    delta = next(item for item in events if item["type"] == "assistant_delta")
    assert "seq" not in delta and "created_at" not in delta


@pytest.mark.asyncio
async def test_hidden_activity_and_true_silence_keep_cumulative_recovery_budget(manager, monkeypatch):
    """Drive the actual parser, attempt clock, watchdog and logical recovery with a controlled clock."""
    import importlib
    import json
    import threading
    import time
    from types import SimpleNamespace

    from server.harness.codex import CODEX_WEB_LEAF as CODEX
    from server.harness.run import HarnessRun

    class Run(HarnessRun):
        def __init__(self):
            super().__init__(CODEX)
            self.timeout_stops = 0
            self.started = asyncio.Event()

        async def start(self, prompt, working_dir, resume_id=None, **kwargs):
            self.prompt = prompt
            self.started.set()

        async def stop(self):
            self.invalidate_activity()
            self.timeout_stops += 1
            self._close_stream()

    clock = SimpleNamespace(now=time.monotonic())
    loop = asyncio.get_running_loop()
    original_loop_time = loop.time
    original_call_at = loop.call_at
    scheduled = asyncio.Queue()
    monkeypatch.setattr(loop, "time", lambda: clock.now)

    def observe_timer(when, callback, *args, **kwargs):
        handle = original_call_at(when, callback, *args, **kwargs)
        if 0 < when - clock.now <= 1.001:
            scheduled.put_nowait((when, handle))
        return handle

    monkeypatch.setattr(loop, "call_at", observe_timer)
    turns = importlib.import_module("server.sessions.turns")
    from server.config import settings as cfg
    monkeypatch.setattr(cfg, "turn_idle_timeout_seconds", 1.0)
    monkeypatch.setattr(cfg, "turn_max_seconds", 0)
    monkeypatch.setattr(cfg, "turn_idle_max_retries", 2)
    monkeypatch.setattr(turns, "time", SimpleNamespace(monotonic=lambda: clock.now))
    questions = importlib.import_module("server.sessions.questions")
    monkeypatch.setattr(questions, "time", SimpleNamespace(monotonic=lambda: clock.now))
    owner_questions = importlib.import_module("server.sessions.owner_questions")
    monkeypatch.setattr(owner_questions, "time", SimpleNamespace(monotonic=lambda: clock.now))
    runtime = importlib.import_module("server.sessions.owner_runtime")
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: clock.now))
    runtime_types = importlib.import_module("server.sessions.runtime_types")
    monkeypatch.setattr(runtime_types, "time", SimpleNamespace(monotonic=lambda: clock.now))
    runs = [Run() for _ in range(3)]
    monkeypatch.setattr(manager, "_make_run", _seq_factory(runs))
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "Hidden activity", backend="codex")

    async def collect():
        return await _request_events(manager, session, "original request")

    task = asyncio.create_task(collect())
    expired = threading.Event()

    def expire_test():
        expired.set()
        loop.call_soon_threadsafe(task.cancel)

    timer = threading.Timer(6, expire_test)
    timer.start()

    async def started(run):
        waiting = asyncio.create_task(run.started.wait())
        try:
            done, _ = await asyncio.wait({task, waiting}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
                pytest.fail("request ended before the next original attempt started")
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    async def timer_ready():
        # Observe the actual clock boundary rather than a private watchdog
        # task. Cancelled/replaced timers are not a new attempt's deadline.
        while True:
            waiting = asyncio.create_task(scheduled.get())
            try:
                done, _ = await asyncio.wait({task, waiting}, return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    await task
                    pytest.fail("request ended before the next timer was armed")
                when, handle = waiting.result()
                if not handle.cancelled() and when > clock.now:
                    return when
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)

    try:
        await started(runs[0])
        next_deadline = await timer_ready()
        for n in range(8):
            clock.now += 0.25
            await runs[0]._handle_line(json.dumps({"type": "item.completed", "item": {
                "id": f"item_{n}", "type": "reasoning", "text": "",
            }}))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if clock.now >= next_deadline:
                next_deadline = await timer_ready()
        assert not task.done() and runs[0].timeout_stops == 0
        for index in range(3):
            clock.now += 1.25
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if index < 2:
                await started(runs[index + 1])
                await timer_ready()
                # A retired attempt's late no-summary event cannot renew its successor.
                await runs[index]._handle_line(json.dumps({"type": "item.completed", "item": {
                    "id": "item_99", "type": "reasoning", "text": "",
                }}))
        events = await task
        assert len([e for e in events if e.get("code") == "turn_timeout_retry"]) == 2
        assert len([e for e in events if e.get("code") == "turn_timeout"]) == 1
        messages = await manager.db.load_messages(session.id)
        assert [m["content"] for m in messages if m["role"] == "user"] == ["original request"]
        assert not any(m["type"] == "thinking" for m in messages)
        assert all(run.timeout_stops >= 1 for run in runs)
    except asyncio.CancelledError:
        if expired.is_set():
            pytest.fail("controlled clock scenario did not complete within the real wall-clock budget")
        raise
    finally:
        timer.cancel()
        timer.join()
        monkeypatch.setattr(loop, "call_at", original_call_at)
        monkeypatch.setattr(loop, "time", original_loop_time)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_attempt_retirement_blocks_recovery_and_next_request(tmp_path):
    """A failed retirement blocks replacement and prevents successful close."""
    import json
    import sys

    artifacts = tmp_path / "failed-retirement"
    artifacts.mkdir()
    proof_file = artifacts / "proof.json"
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-s",
        str(Path(__file__).resolve()), "-o", "python_functions=_failed_retirement_child",
        "--basetemp", str(artifacts / "child-tests"),
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    output = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output), 15)
        assert process.returncode == 70, (stdout + stderr).decode(errors="replace")
        print("Actual Manager cleanup could not confirm the original FakeRun retirement; process exited 70.")
        assert proof_file.exists(), (stdout + stderr).decode(errors="replace")
        proof = json.loads(proof_file.read_text())
        assert proof["first_starts"] == 1 and proof["successor_starts"] == 0
        assert proof["successor_pending"] is True
        assert (artifacts / "first-started").read_text() == "recoverable request"
        assert (artifacts / "retirement-failed").read_text() == "writer exit unconfirmed"
        assert not (artifacts / "successor-started").exists()
        database = Database(proof["database"])
        await database.initialize()
        restored = SessionManager()
        try:
            await restored.initialize(database)
            rows = await database.load_messages(proof["session_id"])
            assert [row["content"] for row in rows if row["role"] == "user"] == ["recoverable request"]
            terminals = [row["content"] for row in rows if row["type"] == "result"]
            assert len(terminals) == 1
            assert terminals[0]["request_id"] == proof["request_id"]
            assert terminals[0]["status"] == "failed"
            assert "writer exit unconfirmed" in terminals[0]["message"]
            assert restored.dispatch_snapshot(proof["session_id"])["items"] == []
        finally:
            await database.close()
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(output, 5)


async def _failed_retirement_child(tmp_path, tmp_path_factory, monkeypatch):
    """Explicit child selection contains an intentionally unretired fake."""
    import json

    from server.harness import HarnessEvent

    artifacts = tmp_path_factory.getbasetemp().parent
    database_path = tmp_path / "original-writer.db"
    database = Database(str(database_path))
    await database.initialize()
    await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    retirement_error = RuntimeError("writer exit unconfirmed")
    started, release = asyncio.Event(), asyncio.Event()

    class FailingRetirement(_FakeBackend):
        def __init__(self):
            super().__init__([HarnessEvent(type="result", is_error=True,
                                         error_code="server_overloaded", content="server overloaded")])
            self.starts = 0

        async def start(self, *args, **kwargs):
            self.starts += 1
            (artifacts / "first-started").write_text("recoverable request")
            started.set()

        async def stream(self):
            await release.wait()
            async for event in super().stream():
                yield event

        async def interrupt(self):
            pass

        async def stop(self):
            (artifacts / "retirement-failed").write_text(str(retirement_error))
            raise retirement_error

    class Successor(_FakeBackend):
        def __init__(self):
            super().__init__([HarnessEvent(type="result")])
            self.starts = 0

        async def start(self, *args, **kwargs):
            self.starts += 1
            (artifacts / "successor-started").write_text("must not be spawned")

        async def interrupt(self):
            pass

    monkeypatch.setattr(type(manager), "_TRANSIENT_RETRY_BASE_DELAY", 0.0)
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "Failed retirement", backend="codex")
    first, successor = FailingRetirement(), Successor()
    monkeypatch.setattr(manager, "_make_run", _seq_factory([first, successor]))
    receipt = await manager.start_message(session.id, "recoverable request")
    await asyncio.wait_for(started.wait(), 2)
    assert first.starts == 1 and successor.starts == 0
    next_receipt = await manager.start_message(session.id, "human correction")
    assert next_receipt.accepted
    assert successor.starts == 0
    assert [item["request_id"] for item in manager.dispatch_snapshot(session.id)["items"]] == [next_receipt.request_id]
    (artifacts / "proof.json").write_text(json.dumps({
        "database": str(database_path), "session_id": session.id,
        "request_id": receipt.request_id,
        "first_starts": first.starts, "successor_starts": successor.starts,
        "successor_pending": True,
    }))
    release.set()
    # No extra fault is injected. The owner observes this same failed
    # retirement; Manager cannot confirm its cleanup and uses real exit70.
    await asyncio.wait_for(asyncio.Event().wait(), 8)


@pytest.mark.asyncio
async def test_manual_stop_and_attempt_finally_share_captured_retirement(manager, monkeypatch):
    started, retiring, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class HeldRetirement(_FakeBackend):
        def __init__(self):
            super().__init__([])
            self.interrupts = 0
            self.stops = 0

        async def start(self, *args, **kwargs):
            started.set()

        async def stream(self):
            await asyncio.Event().wait()
            yield None

        async def interrupt(self):
            self.interrupts += 1
            retiring.set()
            await release.wait()

        async def stop(self):
            self.stops += 1

    backend = HeldRetirement()
    session = await _new(manager, "Shared retirement")
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: backend)
    receipt = await manager.start_message(session.id, "held request")
    await started.wait()
    try:
        assert await asyncio.wait_for(manager.interrupt(session.id), 1)
        await retiring.wait()
        assert backend.interrupts == 1 and backend.stops == 0
    finally:
        release.set()
    completion = await asyncio.wait_for(receipt.wait(), 2)
    assert completion.outcome.status == "interrupted"
    assert backend.interrupts == 1 and backend.stops == 1

"""Startup and unarchive restore the same durable session identity/settings."""

import pytest

from server.app_agent import AppAgentError, AppAgentManager
from server.database import Database
from server.session_manager import SessionManager


@pytest.mark.parametrize("empty_settings", [False, True])
async def test_app_conversation_roundtrips_all_persisted_fields(tmp_path, empty_settings):
    db = Database(":memory:")
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    try:
        agent = await db.get_system_agent()
        await db.save_credential("synthetic-credential", "claude-code", "test", "api_key", "unused", "now")
        parent = await manager.create_session(agent["id"], "parent", str(tmp_path))
        session = await manager.create_session(agent["id"], "app conversation", str(tmp_path),
            origin="app", app_id="owned-app", parent_session_id=parent.id, delegation_request="original request",
            credential_id="synthetic-credential")
        settings = {
            "claude_session_id": "synthetic-thread",
            "model": "" if empty_settings else "model-id",
            "reasoning_effort": "" if empty_settings else "high",
            "service_tier": "" if empty_settings else "fast",
            "forked_from_session_id": parent.id, "fork_after_seq": 1,
            "fork_needs_replay": True, "fork_metadata": '{"prefilled_prompt":"original"}',
            "fork_revert_record": '{"status":"completed"}', "fork_status": "ready",
        }
        await db.update_session_field(session.id, **settings)
        await db.append_message(session.id, 0, "user", "text", "persisted input")
        app = AppAgentManager()
        app.session_mgr = manager
        assert app._require_conversation("owned-app", session.id).id == session.id
        await manager.archive_session(session.id, replace=False)
        expected_count, stored_next, _ = await db.message_sequence_state(session.id)
        expected_next = max(stored_next, settings["fork_after_seq"] + 1)
        restored = await manager.unarchive_session(session.id)
        assert app._require_conversation("owned-app", session.id) is restored
        with pytest.raises(AppAgentError) as error:
            app._require_conversation("other-app", session.id)
        assert error.value.status_code == 404
        for field, expected in settings.items():
            assert getattr(restored, field) == (None if expected == "" else expected)
        assert restored.parent_session_id == parent.id
        assert restored.delegation_request == "original request"
        assert restored.origin == "app" and restored.agent_id == agent["id"]
        for field in ("id", "name", "working_dir", "created_at", "credential_id", "app_id", "user_id", "backend"):
            assert getattr(restored, field) == getattr(session, field)
        assert restored._next_message_seq == expected_next and restored._message_count == expected_count
        await manager.await_session_execution(restored.id, timeout=1)
        snapshot = manager.dispatch_snapshot(restored.id)
        assert snapshot["active_request_id"] is None and snapshot["items"] == []
        assert manager.running_turns_for(restored.user_id) == 0
        assert not manager.session_is_executing(restored)
        await manager.drain_turns()
        # A fresh manager exercises the actual startup entry point too.
        restarted = SessionManager()
        await restarted.initialize(db)
        loaded = restarted.get_session(session.id)
        for field in [*settings, "app_id", "parent_session_id", "delegation_request", "user_id", "backend"]:
            assert getattr(loaded, field) == getattr(restored, field)
        assert loaded._next_message_seq == expected_next and loaded._message_count == expected_count
    finally:
        await db.close()

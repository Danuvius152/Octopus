"""Authenticated HTTP → original pull/codec → atomic local transcript."""

import asyncio
import io
import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from server.cli import build_parser, discover_sessions, do_handoff, do_pull
from server.harness.claude_code import _claude_project_dir
from tests.accounts import create_account
from tests.message_export_runtime import export_runtime as export_runtime


@pytest.mark.parametrize("root", ["default", "configured", "override"])
@pytest.mark.parametrize("cwd_kind", ["special", "long"])
async def test_pull_and_handoff_share_the_harness_native_directory(export_runtime, tmp_path, monkeypatch, capsys, root, cwd_kind):
    database, _, session, client, args, requests = export_runtime
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    await database.append_message(session.id, 0, "user", "text", "native history")
    if root == "default":
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "native-config"))
    cwd = tmp_path / "my.project_空" if cwd_kind == "special" else tmp_path.joinpath(*(["long.dir_空"] * 12))
    cwd.mkdir(parents=True)
    args.project_dir = str(tmp_path / "explicit-project") if root == "override" else None
    if cwd_kind == "special":
        args.cwd = str(cwd)
    else:
        await database.update_session_field(session.id, working_dir=str(cwd))
        args.cwd = None

    await asyncio.to_thread(do_pull, args)
    native_dir = _claude_project_dir(str(cwd))
    project_dir = Path(args.project_dir) if args.project_dir else native_dir
    expected = project_dir / "export-native.jsonl"
    assert expected.is_file()
    assert str(expected) in capsys.readouterr().out
    assert [row["session_id"] for row in discover_sessions(expected.parent)] == ["export-native"]
    lines = [json.loads(line) for line in expected.read_text().splitlines()]
    assert [line["cwd"] for line in lines] == [str(cwd)]
    if root == "override":
        assert not native_dir.exists()

    monkeypatch.chdir(cwd)
    options = ["handoff", "--server", "http://test", f"--token={args.token}"]
    if args.project_dir:
        options += ["--project-dir", args.project_dir]
    if cwd_kind == "special":
        options += ["--session-id", "export-native"]
    else:
        monkeypatch.setattr("builtins.input", lambda _: "1")
    handoff = build_parser().parse_args(options)
    await asyncio.to_thread(do_handoff, handoff)
    output = capsys.readouterr().out
    assert "Session imported:" in output
    imported_id = output.split("Session imported: ", 1)[1].splitlines()[0]
    imported = (await client.get(f"/api/sessions/{imported_id}")).json()
    assert imported["working_dir"] == str(cwd)
    assert imported["claude_session_id"] == "export-native"
    assert [message["content"] for message in imported["messages"]] == ["native history"]
    assert requests[-1] == "http://test/api/sessions/import"


@pytest.mark.parametrize("count", [0, 199, 200, 201, 601])
async def test_pull_exports_complete_members_in_original_order(export_runtime, tmp_path, capsys, count):
    database, _, session, _, args, requests = export_runtime
    for seq in range(count):
        await database.append_message(session.id, seq, "user", "text", f"message-{seq}")
    await asyncio.to_thread(do_pull, args)
    lines = [json.loads(line) for line in (tmp_path / "output/export-native.jsonl").read_text().splitlines()]
    assert [line["message"]["content"] for line in lines] == [f"message-{seq}" for seq in range(count)]
    assert not lines or lines[0]["parentUuid"] is None
    assert all(line["parentUuid"] == previous["uuid"] for previous, line in zip(lines[:-1], lines[1:], strict=True))
    assert f"Pulled session with {count} messages" in capsys.readouterr().out
    assert len(requests) == max(1, (count + 199) // 200)
    assert all("/message-export" in url for url in requests)


async def test_export_cursor_zero_gaps_and_late_commits_preserve_first_members_and_codec_metadata(export_runtime):
    database, _, session, client, _, _ = export_runtime
    for seq in (0, 2, 5):
        await database.append_message(session.id, seq, "user", "text", f"original-{seq}")
    url = f"/api/sessions/{session.id}/message-export"
    first = (await client.get(url, params={"limit": 1})).json()
    assert first["next_after_seq"] == 0 and first["has_more"]
    snapshot = first["snapshot"]
    assert snapshot["expected_count"] == 3 and snapshot["max_seq"] == 5
    await database.append_message(session.id, 1, "user", "text", "late reserved sequence")
    await database.append_message(session.id, 6, "user", "text", "new append")
    await database.update_session_field(session.id, backend="codex", claude_session_id="changed", working_dir="/changed")
    second = (await client.get(url, params={"limit": 1, "snapshot": json.dumps(snapshot), "after_seq": 0})).json()
    assert [item["seq"] for item in second["messages"]] == [2]
    assert second["snapshot"] == snapshot and second["next_after_seq"] == 2
    third = (await client.get(url, params={"limit": 1, "snapshot": json.dumps(snapshot), "after_seq": 2})).json()
    assert [item["seq"] for item in third["messages"]] == [5]
    assert third["snapshot"] == snapshot and not third["has_more"] and third["next_after_seq"] is None


async def test_initial_metadata_membership_and_page_share_one_sqlite_snapshot(export_runtime, tmp_path, monkeypatch):
    import aiosqlite

    database, _, session, client, _, _ = export_runtime
    await database.append_message(session.id, 0, "user", "text", "first")
    entered, release = asyncio.Event(), asyncio.Event()
    execute = database.conn._execute

    async def gated_execute(function, *args, **kwargs):
        if args and isinstance(args[0], str) and args[0].startswith("SELECT COALESCE(MAX(id)"):
            entered.set()
            await release.wait()
        return await execute(function, *args, **kwargs)

    monkeypatch.setattr(database.conn, "_execute", gated_execute)
    loading = asyncio.create_task(client.get(f"/api/sessions/{session.id}/message-export"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with aiosqlite.connect(tmp_path / "export.sqlite3") as writer:
            await writer.execute("INSERT INTO messages(session_id,seq,role,type,content) VALUES(?,1,'user','text','\"late\"')", (session.id,))
            await writer.execute("UPDATE sessions SET backend='codex', claude_session_id='changed' WHERE id=?", (session.id,))
            await asyncio.wait_for(writer.commit(), 3)
        release.set()
        response = await asyncio.wait_for(loading, 3)
        assert response.status_code == 200
        page = response.json()
        assert page["snapshot"]["expected_count"] == 1 and page["snapshot"]["max_seq"] == 0
        assert page["snapshot"]["backend"] == "claude-code" and page["snapshot"]["claude_session_id"] == "export-native"
        assert [item["content"] for item in page["messages"]] == ["first"]
    finally:
        release.set()
        await asyncio.gather(loading, return_exceptions=True)


@pytest.mark.parametrize("archived", [False, True])
async def test_export_checks_current_account_and_session_on_every_page(export_runtime, archived):
    database, manager, session, client, _, _ = export_runtime
    await database.append_message(session.id, 0, "user", "text", "owned")
    if archived:
        await manager.archive_session(session.id, replace=False)
    url = f"/api/sessions/{session.id}/message-export"
    first = await client.get(url, params={"limit": 1})
    assert first.status_code == 200 and first.json()["messages"][0]["content"] == "owned"
    snapshot = first.json()["snapshot"]
    other, token = await create_account(database, username="other-export-owner")
    forged = {**snapshot, "user_id": other["id"]}
    for query in ({}, {"snapshot": json.dumps(snapshot), "after_seq": 0}, {"snapshot": json.dumps(forged)}):
        assert (await client.get(url, params=query, headers={"Authorization": f"Bearer {token}"})).status_code == 404
    assert (await client.get(url, headers={"Authorization": ""})).status_code == 401
    assert (await client.get(url, params={"snapshot": json.dumps(forged)})).status_code == 409
    assert (await client.get(url, params={"snapshot": json.dumps({**snapshot, "session_id": "other"})})).status_code == 409
    assert (await client.get(url, params={"after_seq": 0})).status_code == 409


@pytest.mark.parametrize("native_id", ["export-native", None])
async def test_pull_freezes_codec_inputs_and_excludes_late_members(export_runtime, tmp_path, monkeypatch, native_id, capsys):
    database, _, session, _, args, _ = export_runtime
    await database.update_session_field(session.id, claude_session_id=native_id)
    for seq in range(0, 402, 2):
        await database.append_message(session.id, seq, "user", "text", f"original-{seq}")
    args.cwd = "/chosen/cwd"
    opener, loop = urllib.request.urlopen, asyncio.get_running_loop()
    changed = False

    async def change_after_first_read():
        await database.update_session_field(session.id, backend="codex", claude_session_id="new-native", working_dir="/new/cwd")
        await database.append_message(session.id, 1, "user", "text", "late reserved")
        await database.append_message(session.id, 402, "user", "text", "appended")

    def changing(request, **kwargs):
        nonlocal changed
        response = opener(request, **kwargs)
        if not changed:
            changed = True
            asyncio.run_coroutine_threadsafe(change_after_first_read(), loop).result(timeout=3)
        return response

    monkeypatch.setattr(urllib.request, "urlopen", changing)
    await asyncio.to_thread(do_pull, args)
    paths = list((tmp_path / "output").glob("*.jsonl"))
    assert len(paths) == 1 and paths[0].stem != "new-native"
    lines = [json.loads(line) for line in paths[0].read_text().splitlines()]
    assert [line["message"]["content"] for line in lines] == [f"original-{seq}" for seq in range(0, 402, 2)]
    assert all(line["sessionId"] == paths[0].stem and line["cwd"] == "/chosen/cwd" for line in lines)
    output = capsys.readouterr().out
    assert output.count("No claude_session_id on server") == (1 if native_id is None else 0)


async def test_pull_retains_full_tool_content_and_counts_source_events(export_runtime, tmp_path, capsys):
    database, _, session, client, args, _ = export_runtime
    text = "完整原文" * 100000
    await database.append_message(session.id, 0, "user", "text", text)
    await database.append_message(session.id, 2, "assistant", "tool_use", tool_name="Read", tool_input={"path": text}, tool_use_id="tool-original")
    await database.append_message(session.id, 5, "tool", "tool_result", text, tool_use_id="tool-original", is_error=True)
    await database.append_message(session.id, 9, "system", "notice", "not a native transcript line")
    page = (await client.get(f"/api/sessions/{session.id}/message-export")).json()
    assert page["messages"][0]["content"] == text
    assert page["messages"][1]["tool_input"] == {"path": text}
    await asyncio.to_thread(do_pull, args)
    lines = [json.loads(line) for line in (tmp_path / "output/export-native.jsonl").read_text().splitlines()]
    assert len(lines) == 3 and lines[0]["message"]["content"] == text
    assert lines[1]["message"]["content"][0] == {"type": "tool_use", "id": "tool-original", "name": "Read", "input": {"path": text}}
    assert lines[2]["message"]["content"][0] == {"type": "tool_result", "tool_use_id": "tool-original", "content": text, "is_error": True}
    assert "Pulled session with 4 messages" in capsys.readouterr().out


@pytest.mark.parametrize("problem", ["codex", "unsafe_native_id", "duplicate"])
async def test_pull_refuses_unrecoverable_history_before_creating_output(export_runtime, tmp_path, problem, capsys):
    database, _, session, _, args, _ = export_runtime
    await database.append_message(session.id, 0, "user", "text", "original")
    if problem == "codex":
        await database.update_session_field(session.id, backend="codex")
    elif problem == "unsafe_native_id":
        await database.update_session_field(session.id, claude_session_id="../escaped")
    else:
        async with database.transaction(write=True) as tx:
            await tx.execute("DROP INDEX messages_unique_sequence")
        await database.append_message(session.id, 0, "assistant", "text", "duplicate")
    with pytest.raises(SystemExit) as caught:
        await asyncio.to_thread(do_pull, args)
    assert caught.value.code == 1 and not (tmp_path / "output").exists()
    assert "Pulled session" not in capsys.readouterr().out


@pytest.mark.parametrize("fault", ["http", "json", "schema", "snapshot", "count", "max_seq", "cursor", "stalled", "duplicate", "order", "delete_member", "delete_session"])
async def test_download_and_membership_failures_preserve_previous_file(export_runtime, tmp_path, monkeypatch, capsys, fault):
    database, _, session, _, args, _ = export_runtime
    for seq in range(203):
        await database.append_message(session.id, seq, "user", "text", f"original-{seq}")
    target = tmp_path / "output/export-native.jsonl"
    target.parent.mkdir()
    previous = b"previous local transcript\n"
    target.write_bytes(previous)
    opener, loop = urllib.request.urlopen, asyncio.get_running_loop()

    async def delete_unread_member():
        async with database.transaction(write=True) as tx:
            await tx.execute("DELETE FROM messages WHERE session_id=? AND seq=200", (session.id,))

    def damaged(request, **kwargs):
        subsequent = "after_seq=" in request.full_url
        if subsequent:
            if fault == "http":
                raise urllib.error.HTTPError(request.full_url, 503, "offline", {}, io.BytesIO(b"controlled disconnect"))
            if fault in {"delete_member", "delete_session"}:
                change = delete_unread_member() if fault == "delete_member" else database.delete_session(session.id)
                asyncio.run_coroutine_threadsafe(change, loop).result(timeout=3)
        with opener(request, **kwargs) as response:
            content = response.read()
        if subsequent and fault == "json":
            return io.BytesIO(b"incomplete JSON{")
        page = json.loads(content)
        if not subsequent:
            if fault == "count":
                page["snapshot"]["expected_count"] += 1
            elif fault == "max_seq":
                page["snapshot"]["max_seq"] += 1
            elif fault == "cursor":
                page["next_after_seq"] += 1
            elif fault == "stalled":
                page["messages"] = []
        elif fault == "schema":
            del page["messages"]
        elif fault == "snapshot":
            page["snapshot"]["through_id"] += 1
        elif fault == "duplicate":
            page["messages"][0]["seq"] = 199
        elif fault == "order":
            page["messages"].reverse()
        return io.BytesIO(json.dumps(page).encode())

    monkeypatch.setattr(urllib.request, "urlopen", damaged)
    with pytest.raises(SystemExit) as caught:
        await asyncio.to_thread(do_pull, args)
    assert caught.value.code == 1
    assert target.read_bytes() == previous and list(target.parent.iterdir()) == [target]
    output = capsys.readouterr()
    assert "Error:" in output.err and "Pulled session" not in output.out


@pytest.mark.parametrize("stage", ["mkstemp", "fdopen", "write", "flush", "close", "file_fsync", "replace", "directory_open", "directory_fsync"])
async def test_local_commit_failures_respect_the_replacement_boundary(export_runtime, tmp_path, monkeypatch, capsys, stage):
    import tempfile

    database, _, session, _, args, _ = export_runtime
    await database.append_message(session.id, 0, "user", "text", "new complete transcript")
    target = tmp_path / "output/export-native.jsonl"
    target.parent.mkdir()
    previous = b"previous local transcript\n"
    target.write_bytes(previous)
    mkstemp, fdopen, open_file = tempfile.mkstemp, os.fdopen, io.open
    fsync, replace, open_descriptor = os.fsync, os.replace, os.open
    descriptors = []

    def refuse(*args, **kwargs):
        raise OSError(f"controlled export {stage} failure")

    def temporary(*args, **kwargs):
        if stage == "mkstemp":
            refuse()
        result = mkstemp(*args, **kwargs)
        descriptors.append(result[0])
        return result

    def file_stream(fd, *args, **kwargs):
        if stage == "fdopen" and fd in descriptors:
            refuse()
        handle = fdopen(fd, *args, **kwargs)
        if fd in descriptors:
            if stage == "flush":
                monkeypatch.setattr(handle, "flush", refuse)
            elif stage == "close":
                close = handle.close

                def failed_close():
                    close()
                    refuse()

                monkeypatch.setattr(handle, "close", failed_close)
        return handle

    def file_open(path, mode="r", *args, **kwargs):
        handle = open_file(path, mode, *args, **kwargs)
        if stage == "write" and str(path).endswith(".tmp") and "w" in mode:
            monkeypatch.setattr(handle, "write", refuse)
        return handle

    def sync(fd):
        selected = "directory_fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file_fsync"
        if stage == selected:
            refuse()
        return fsync(fd)

    def publish(source, destination):
        assert target.read_bytes() == previous
        if stage == "replace":
            refuse()
        return replace(source, destination)

    def directory_open(path, flags, *args, **kwargs):
        if stage == "directory_open" and flags & os.O_DIRECTORY:
            refuse()
        return open_descriptor(path, flags, *args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", temporary)
    monkeypatch.setattr(os, "fdopen", file_stream)
    monkeypatch.setattr(io, "open", file_open)
    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", publish)
    monkeypatch.setattr(os, "open", directory_open)
    with pytest.raises(SystemExit) as caught:
        await asyncio.to_thread(do_pull, args)
    assert caught.value.code == 1 and list(target.parent.iterdir()) == [target]
    output = capsys.readouterr()
    assert f"controlled export {stage} failure" in output.err and "Pulled session" not in output.out
    if stage.startswith("directory_"):
        assert "new file may already be active" in output.err
        assert json.loads(target.read_text())["message"]["content"] == "new complete transcript"
    else:
        assert target.read_bytes() == previous
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("fault", ["serialization", "incomplete_codec"])
async def test_codec_failure_cannot_publish_partial_output(export_runtime, tmp_path, monkeypatch, capsys, fault):
    from server.harness import get_harness

    database, _, session, _, args, _ = export_runtime
    await database.append_message(session.id, 0, "user", "text", "first")
    await database.append_message(session.id, 1, "user", "text", "last")
    target = tmp_path / "output/export-native.jsonl"
    target.parent.mkdir()
    previous = b"previous local transcript\n"
    target.write_bytes(previous)
    encode = json.JSONEncoder.iterencode

    def failed_encode(encoder, payload, *args, **kwargs):
        if isinstance(payload, dict) and payload.get("sessionId") == "export-native":
            raise TypeError("controlled native codec serialization failure")
        return encode(encoder, payload, *args, **kwargs)

    def incomplete(path, messages, *args):
        next(iter(messages))
        Path(path).write_text("partial codec output")

    if fault == "serialization":
        monkeypatch.setattr(json.JSONEncoder, "iterencode", failed_encode)
    else:
        monkeypatch.setattr(get_harness("claude-code").transcript_codec, "write_file", incomplete)
    with pytest.raises(SystemExit) as caught:
        await asyncio.to_thread(do_pull, args)
    assert caught.value.code == 1 and target.read_bytes() == previous
    assert list(target.parent.iterdir()) == [target]
    assert "Pulled session" not in capsys.readouterr().out

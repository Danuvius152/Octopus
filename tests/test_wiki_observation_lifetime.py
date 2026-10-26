"""A registered vault keeps its receiver; known observation gaps fail closed."""

import asyncio
import ctypes
import errno
import os
import sqlite3

import pytest

from server.config import settings
from server.wiki import events
from server.wiki.registry import WikiRegistry
from server.wiki.store import WikiError


def _replace(path):
    original = path.read_bytes()
    path.unlink()
    path.write_bytes(original)


def _stream_state(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT received_seq,uncertain FROM filesystem_state WHERE singleton=1").fetchone()


def _owner_recovery(registry):
    manager = registry.recoveries()
    plan = manager.plan(None)
    recovery_id, plan_hash = plan["recovery_id"], plan["plan_hash"]
    with pytest.raises(WikiError) as unapproved:
        manager.apply(None, recovery_id, plan_hash)
    assert unapproved.value.status == 409
    manager.approve(None, recovery_id, plan_hash)
    for _ in range(12):
        result = manager.apply(None, recovery_id, plan_hash)
        if result["phase"] == "complete":
            assert result["completion_receipt"]
            return
    raise AssertionError("Owner-approved recovery did not complete")


async def test_thirty_three_registered_vaults_keep_queued_and_later_replacements_observed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    monkeypatch.setattr(settings, "wiki_reconcile_seconds", 3600)
    registry = WikiRegistry()
    roots = []
    first = None
    try:
        for index in range(32):
            root = tmp_path / f"vault-{index}"
            root.mkdir()
            (root / "note.md").write_text("# Note\nSame bytes, distinct file occurrence.\n")
            registry.configure(f"{index:012x}", root, "curator")
            roots.append(root)
            if index == 0:
                first = registry.store(f"{0:012x}")
                original_id = first.read("note.md")["id"]
        assert first is not None
        registry.start_reconciler()
        _replace(roots[0] / "note.md")  # Queued before the 33rd registration.
        last = tmp_path / "vault-32"
        last.mkdir()
        registry.configure(f"{32:012x}", last, "curator")
        current = registry.store(f"{0:012x}")
        first_replacement = current.read("note.md")["id"]
        assert first_replacement != original_id
        assert current is first
        received = _stream_state(current.db_path)[0]
        _replace(roots[0] / "note.md")  # Later events need intake without a read.

        async def wait_for_receipt():
            while _stream_state(current.db_path)[0] <= received:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(wait_for_receipt(), 5)
        assert registry.store(f"{0:012x}").read("note.md")["id"] != first_replacement
    finally:
        await registry.stop()


@pytest.mark.parametrize("change", ["delete", "rename"])
@pytest.mark.parametrize("application", ["synchronize", "refresh", "queued"])
@pytest.mark.parametrize("transition", ["replacement", "stop-start"])
async def test_observed_directory_exit_does_not_poison_replacement_or_reopen(tmp_path, monkeypatch, change, application, transition):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    root = tmp_path / "vault"
    directory = root / "A"
    directory.mkdir(parents=True)
    (directory / "note.md").write_text("# Original\nKeep this identity on rename.\n")
    registry = WikiRegistry()
    try:
        registry.configure(None, root, "curator-a")
        store = registry.store(None)
        original = store.read("A/note.md")
        if change == "delete":
            (directory / "note.md").unlink()
            directory.rmdir()
        else:
            directory.rename(root / "B")
        if application != "queued":
            getattr(store, application)()
        if transition == "replacement":
            registry.configure(None, root, "curator-b")
        else:
            await registry.stop()
            registry.resume_access()
        current = registry.store(None)
        assert not _stream_state(current.db_path)[1]
        if change == "rename":
            assert current.read("B/note.md")["id"] == original["id"]
        else:
            with pytest.raises(WikiError) as gone:
                current.read(original["id"])
            assert gone.value.status == 404
        # Simulate the derived stale hints left by older releases. They are
        # not proof of directory existence and must not demand owner recovery.
        with current.connect() as db:
            db.execute("INSERT OR IGNORE INTO watch_directories VALUES('A')")
        await registry.stop()
        registry.resume_access()
        reopened = registry.store(None)
        assert not _stream_state(reopened.db_path)[1]
        if change == "rename":
            assert reopened.read("B/note.md")["id"] == original["id"]
    finally:
        registry.close()


@pytest.mark.parametrize("change", ["delete", "rename"])
async def test_queued_directory_exit_then_plain_file_does_not_turn_a_hint_into_a_gap(tmp_path, monkeypatch, change):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    root = tmp_path / "vault"
    directory = root / "A.md"
    (directory / "nested").mkdir(parents=True)
    (directory / "nested" / "note.md").write_text("# Original\nKeep identity on rename.\n")
    registry = WikiRegistry()
    try:
        registry.configure(None, root, "curator-a")
        old = registry.store(None).read("A.md/nested/note.md")
        if change == "rename":
            directory.rename(root / "B")
        else:
            (directory / "nested" / "note.md").unlink()
            (directory / "nested").rmdir()
            directory.rmdir()
        directory.write_text("# Replacement\nThis is a new plain file.\n")
        # No intake call before replacement: old directory hints are still in DB.
        registry.configure(None, root, "curator-b")
        current = registry.store(None)
        assert current.read("A.md")["content"] == "# Replacement\nThis is a new plain file.\n"
        assert current.read("A.md")["id"] != old["id"]
        if change == "rename":
            assert current.read("B/nested/note.md")["id"] == old["id"]
        else:
            with pytest.raises(WikiError) as gone:
                current.read(old["id"])
            assert gone.value.status == 404
        # Legacy hints can also have a plain-file ancestor.
        with current.connect() as db:
            db.executemany("INSERT OR IGNORE INTO watch_directories VALUES(?)", [("A.md",), ("A.md/nested",)])
        await registry.stop()
        registry.resume_access()
        reopened = registry.store(None)
        assert not _stream_state(reopened.db_path)[1]
        assert reopened.read("A.md")["content"].startswith("# Replacement")
    finally:
        registry.close()


@pytest.mark.parametrize("failure", ["observer", "root-watch"])
def test_initial_observer_failures_remain_uncertain_after_resources_recover(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").write_text("# Original\nPreserved content.\n")
    registry = WikiRegistry()
    native = ctypes.CDLL

    def failing_library(*args, **kwargs):
        library = native(*args, **kwargs)

        def refused(*args):
            ctypes.set_errno(errno.EMFILE if failure == "observer" else errno.ENOSPC)
            return -1

        if failure == "observer":
            library.inotify_init1 = refused
        else:
            library.inotify_add_watch = refused
        return library

    try:
        with monkeypatch.context() as fault:
            fault.setattr(events.ctypes, "CDLL", failing_library)
            with pytest.raises(WikiError) as rejected:
                registry.configure(None, root, "curator")
            assert rejected.value.status == 503
        assert _stream_state(tmp_path / "state" / "index.sqlite3")[1]
        with pytest.raises(WikiError) as still_closed:
            registry.store(None)
        assert still_closed.value.status == 503
        _owner_recovery(registry)
        assert registry.store(None).read("note.md")["content"] == "# Original\nPreserved content.\n"
    finally:
        registry.close()


@pytest.mark.parametrize("phase", ["active-scan", "reopen"])
def test_subdirectory_watch_failure_is_durable_and_scanning_cannot_clear_it(tmp_path, monkeypatch, phase):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    root = tmp_path / "vault"
    directory = root / "Library"
    directory.mkdir(parents=True)
    (directory / "note.md").write_text("# Original\nPreserved content.\n")
    registry = WikiRegistry()
    native = ctypes.CDLL
    try:
        registry.configure(None, root, "curator")
        if phase == "reopen":
            registry.close()
        else:
            directory = root / "New-directory"
            directory.mkdir()
            (directory / "note.md").write_text("# New note\n")

        def failing_library(*args, **kwargs):
            library = native(*args, **kwargs)
            add = library.inotify_add_watch

            def refused(fd, path, flags):
                if os.fsdecode(path) == str(directory):
                    ctypes.set_errno(errno.ENOSPC)
                    return -1
                return add(fd, path, flags)

            library.inotify_add_watch = refused
            return library

        with monkeypatch.context() as fault:
            if phase == "reopen":
                fault.setattr(events.ctypes, "CDLL", failing_library)
                def operation():
                    return registry.store(None)
            else:
                store = registry.store(None)
                native_add = store.events._libc.inotify_add_watch

                def refused(fd, path, flags):
                    if os.fsdecode(path) == str(directory):
                        ctypes.set_errno(errno.ENOSPC)
                        return -1
                    return native_add(fd, path, flags)

                fault.setattr(store.events._libc, "inotify_add_watch", refused)
                operation = store.refresh
            with pytest.raises(WikiError) as rejected:
                operation()
            assert rejected.value.status == 503
        assert _stream_state(tmp_path / "state" / "index.sqlite3")[1]
        if phase == "active-scan":
            with pytest.raises(WikiError):
                store.refresh()
            assert store.index_state()["index_status"] == "unavailable"
        registry.close()
        with pytest.raises(WikiError):
            registry.store(None)
        _owner_recovery(registry)
        assert registry.store(None).read("Library/note.md")["content"] == "# Original\nPreserved content.\n"
    finally:
        registry.close()

"""Lifecycle and external editing through the registered Wiki service."""

import asyncio
import os

import pytest

from server.config import settings
from server.wiki.registry import WikiRegistry
from server.wiki.store import WikiError


@pytest.fixture
def registered(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").write_text("# 原文\n原来的解释。\n")
    registry = WikiRegistry()
    registry.configure(None, root, "curator")
    yield registry, root
    registry.close()


async def test_compensation_finds_an_edit_without_a_directory_event(registered, tmp_path):
    registry, root = registered
    service = registry.store(None)
    original = service.read("note.md")
    outside = tmp_path / "outside.md"
    os.link(root / "note.md", outside)
    outside.write_text("# 原文\n补偿对账发现的新解释。\n")
    await registry.reconcile()
    found = registry.store(None).search("补偿对账")
    assert found["items"][0]["id"] == original["id"]
    assert found["items"][0]["revision"] != original["revision"]
    assert found["index_status"] == "ready"


def test_failed_inventory_preserves_document_identity_and_reports_unavailable(registered, monkeypatch):
    registry, root = registered
    store = registry.store(None)
    original = store.read("note.md")
    original_walk = os.walk

    def interrupted_inventory(path, *args, **kwargs):
        if path == root:
            error = PermissionError("Controlled directory access failure")
            callback = kwargs.get("onerror")
            if callback:
                callback(error)
            return iter(())
        return original_walk(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "walk", interrupted_inventory)
        with pytest.raises(WikiError):
            store.refresh()
        assert store.index_state()["index_status"] == "unavailable"
    store.refresh()
    assert store.read(original["id"])["id"] == original["id"]
    assert store.index_state()["index_status"] == "ready"


def test_external_rename_and_edit_keep_the_same_article_identity(registered):
    registry, root = registered
    store = registry.store(None)
    original = store.read("note.md")
    renamed = root / "renamed.md"
    (root / "note.md").rename(renamed)
    renamed.write_text("# 新题名\n改名后同时修正的一句话。\n")
    found = store.search("同时修正")["items"][0]
    assert found["id"] == original["id"]
    assert found["path"] == "renamed.md"
    assert found["revision"] != original["revision"]


def test_compensation_keeps_identity_when_a_directory_is_renamed_and_child_edited(registered):
    registry, root = registered
    (root / "old").mkdir()
    (root / "old/note.md").write_text("# 文件夹里的文章\n原观点。\n")
    store = registry.store(None)
    store.refresh()
    original = store.read("old/note.md")
    (root / "old").rename(root / "new")
    (root / "new/note.md").write_text("# 文件夹里的文章\n修正一处明确笔误。\n")
    store.refresh()
    changed = store.read(original["id"])
    assert changed["path"] == "new/note.md"
    assert changed["metadata"]["uid"] == original["metadata"]["uid"]


def test_unsupported_symlink_does_not_block_queries_for_real_articles(registered, tmp_path):
    registry, root = registered
    outside = tmp_path / "private.md"
    outside.write_text("# 库外私有资料\n不能读入主库。\n")
    (root / "link.md").symlink_to(outside)
    service = registry.store(None)
    assert service.search("原文")["items"][0]["path"] == "note.md"
    assert service.search("库外私有资料")["items"] == []


async def test_background_updates_the_index_and_restart_preserves_identity(registered):
    registry, root = registered
    await registry.start()
    try:
        service = registry.store(None)
        original = service.read("note.md")
        epoch = service.index_state()["index_epoch"]
        (root / "note.md").write_text("# 后台更新\n无需查询触发的内容。\n")

        async def wait_for_index():
            while service.index_state()["index_epoch"] == epoch:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_index(), timeout=3)
        assert service.search("后台更新")["items"][0]["id"] == original["id"]
        updated_revision = service.read(original["id"])["revision"]
        generation = registry.config(None)["registration_generation"]
    finally:
        await registry.stop()
    with pytest.raises(WikiError) as stopped:
        registry.store(None)
    assert stopped.value.status == 503
    await registry.start()
    try:
        restarted = registry.store(None)
        assert restarted is not service
        preserved = restarted.read(original["id"])
        assert preserved["id"] == original["id"]
        assert preserved["revision"] == updated_revision
        assert preserved["content"] == "# 后台更新\n无需查询触发的内容。\n"
        assert registry.config(None)["registration_generation"] == generation
    finally:
        await registry.stop()

def test_reconfigured_store_cannot_report_stale_results_as_ready(registered):
    registry, root = registered
    previous = registry.store(None)
    registry.configure(None, root, "replacement-curator")
    (root / "note.md").write_text("# Changed\nnewword after retirement\n")
    with pytest.raises(WikiError, match="retired"):
        previous.search("newword")
    assert registry.store(None).search("newword")["items"]

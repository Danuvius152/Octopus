"""Tests for the crypto helper + credential DB methods."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from server.crypto import decrypt, encrypt
from server.database import Database
from tests.accounts import create_account

# ---------------------------------------------------------------------------
# crypto
# ---------------------------------------------------------------------------


def test_roundtrip():
    secret = "octopus-token"
    enc = encrypt("sk-ant-12345", secret)
    assert enc != "sk-ant-12345"
    assert decrypt(enc, secret) == "sk-ant-12345"


def test_wrong_key_raises():
    enc = encrypt("plaintext", "key-one")
    with pytest.raises(ValueError):
        decrypt(enc, "key-two")


def test_different_inputs_produce_different_ciphertexts():
    # Fernet bundles a random IV — same input + same key produces different
    # ciphertexts each call. We test by encrypting twice.
    enc_a = encrypt("same", "k")
    enc_b = encrypt("same", "k")
    assert enc_a != enc_b
    assert decrypt(enc_a, "k") == decrypt(enc_b, "k") == "same"


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_save_and_load_credential(db):
    now = datetime.now(UTC).isoformat()
    await db.save_credential(
        credential_id="c-1",
        backend="claude-code",
        label="Personal",
        auth_type="api_key",
        secret_encrypted=encrypt("sk-secret", "auth"),
        created_at=now,
    )
    rows = await db.load_credentials()
    assert len(rows) == 1
    assert rows[0]["id"] == "c-1"
    assert rows[0]["backend"] == "claude-code"
    assert rows[0]["label"] == "Personal"
    # Decryptable
    assert decrypt(rows[0]["secret_encrypted"], "auth") == "sk-secret"


@pytest.mark.asyncio
async def test_get_credential_returns_none_for_unknown(db):
    assert await db.get_credential("nope") is None


@pytest.mark.asyncio
async def test_update_credential_label_and_secret(db):
    now = datetime.now(UTC).isoformat()
    await db.save_credential(
        "c-2", "claude-code", "Old", "api_key", encrypt("old", "k"), now
    )
    await db.update_credential(
        "c-2", label="New", secret_encrypted=encrypt("new", "k")
    )
    row = await db.get_credential("c-2")
    assert row is not None
    assert row["label"] == "New"
    assert decrypt(row["secret_encrypted"], "k") == "new"


@pytest.mark.asyncio
async def test_update_credential_partial_keeps_other_fields(db):
    """Only `label` changes — secret should stay the same."""
    now = datetime.now(UTC).isoformat()
    await db.save_credential(
        "c-3", "claude-code", "L", "api_key", encrypt("kept", "k"), now
    )
    await db.update_credential("c-3", label="Renamed")
    row = await db.get_credential("c-3")
    assert row["label"] == "Renamed"
    assert decrypt(row["secret_encrypted"], "k") == "kept"


@pytest.mark.asyncio
async def test_delete_credential(db):
    now = datetime.now(UTC).isoformat()
    await db.save_credential(
        "c-4", "codex", "L", "api_key", encrypt("x", "k"), now
    )
    assert await db.delete_credential("c-4") is True
    assert await db.delete_credential("c-4") is False
    assert await db.get_credential("c-4") is None


# ---------------------------------------------------------------------------
# Storage split (Steal Plan B-4 / B-5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_writes_to_credential_secrets_table(db):
    now = datetime.now(UTC).isoformat()
    enc = encrypt("split-secret", "k")
    await db.save_credential("c-split", "claude-code", "L", "api_key", enc, now)

    cursor = await db.conn.execute(
        "SELECT secret_encrypted FROM credential_secrets WHERE credential_id = ?",
        ("c-split",),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert decrypt(row[0], "k") == "split-secret"


@pytest.mark.asyncio
async def test_load_prefers_split_secret_over_legacy_column(db):
    """get_credential reads from credential_secrets via JOIN; if a future
    rotation only updates the split table, the read should still surface
    the fresh value."""
    now = datetime.now(UTC).isoformat()
    await db.save_credential(
        "c-rot", "claude-code", "L", "api_key", encrypt("first", "k"), now
    )
    # Simulate a split-only rotation (the legacy column stays "first").
    fresh = encrypt("second", "k")
    await db.conn.execute(
        "UPDATE credential_secrets SET secret_encrypted = ? WHERE credential_id = ?",
        (fresh, "c-rot"),
    )
    await db.conn.commit()
    row = await db.get_credential("c-rot")
    assert decrypt(row["secret_encrypted"], "k") == "second"


async def test_upgrade_preserves_legacy_only_secrets_and_retires_stale_copies(tmp_path):
    path = tmp_path / "legacy.db"
    seed = Database(str(path))
    await seed.initialize()
    owner, _ = await create_account(seed)
    await seed.close()
    original = encrypt("legacy only", "original key")
    stale = encrypt("obsolete value", "retired key")
    current = encrypt("rotated value", "current key")
    with sqlite3.connect(path) as stored:
        stored.executescript("""
            DROP TABLE credential_secrets;
            DROP TABLE backend_credentials;
            CREATE TABLE backend_credentials (
                id TEXT PRIMARY KEY, backend TEXT NOT NULL, label TEXT NOT NULL,
                auth_type TEXT NOT NULL, secret_encrypted TEXT NOT NULL,
                created_at TEXT NOT NULL, user_id TEXT
            );
            CREATE TABLE credential_secrets (
                credential_id TEXT PRIMARY KEY, secret_encrypted TEXT NOT NULL,
                FOREIGN KEY (credential_id) REFERENCES backend_credentials(id)
            );
        """)
        stored.executemany(
            "INSERT INTO backend_credentials VALUES (?, 'codex', 'Personal', 'api_key', ?, 't', ?)",
            [("legacy", original, owner["id"]), ("rotated", stale, owner["id"])],
        )
        stored.execute("INSERT INTO credential_secrets VALUES ('rotated', ?)", (current,))

    for _ in range(2):
        upgraded = Database(str(path))
        await upgraded.initialize()
        try:
            legacy = await upgraded.get_credential("legacy")
            rotated = await upgraded.get_credential("rotated")
            assert legacy["user_id"] == rotated["user_id"] == owner["id"]
            assert decrypt(legacy["secret_encrypted"], "original key") == "legacy only"
            assert decrypt(rotated["secret_encrypted"], "current key") == "rotated value"
        finally:
            await upgraded.close()

        with sqlite3.connect(path) as stored:
            dump = "\n".join(stored.iterdump())
        assert stale not in dump
        assert dump.count(original) == 1
        assert dump.count(current) == 1


@pytest.mark.asyncio
async def test_default_metadata_fields(db):
    now = datetime.now(UTC).isoformat()
    await db.save_credential(
        "c-meta", "claude-code", "L", "api_key", encrypt("s", "k"), now
    )
    row = await db.get_credential("c-meta")
    assert row["status"] == "active"
    assert row["needs_reconnect"] is False
    assert row["token_expires_at"] is None
    assert row["last_refresh_error_code"] is None


@pytest.mark.asyncio
async def test_update_refresh_state(db):
    now = datetime.now(UTC).isoformat()
    await db.save_credential(
        "c-fail", "claude-code", "L", "oauth", encrypt("s", "k"), now
    )
    await db.update_credential(
        "c-fail",
        status="needs_reconnect",
        needs_reconnect=True,
        last_refresh_error_code="refresh_token_expired",
    )
    row = await db.get_credential("c-fail")
    assert row["status"] == "needs_reconnect"
    assert row["needs_reconnect"] is True
    assert row["last_refresh_error_code"] == "refresh_token_expired"


@pytest.mark.asyncio
async def test_delete_cascades_to_credential_secrets(db):
    now = datetime.now(UTC).isoformat()
    await db.save_credential(
        "c-del", "claude-code", "L", "api_key", encrypt("s", "k"), now
    )
    await db.delete_credential("c-del")
    cursor = await db.conn.execute(
        "SELECT 1 FROM credential_secrets WHERE credential_id = ?", ("c-del",)
    )
    assert await cursor.fetchone() is None

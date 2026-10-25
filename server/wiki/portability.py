"""Offline Wiki copying preserves proofs and rebinds only current local identity."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from ..storage_verification import _reader, verify_wiki
from . import job_events, protocols
from .event_inbox import read_state
from .store import WikiStore


def _within(root: Path, name: str) -> Path:
    path = root / name
    if Path(name).is_absolute() or ".." in Path(name).parts or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Wiki contains a path outside its declared vault")
    return path


def _stable(db: sqlite3.Connection, registration: dict, root: Path, state: Path, *, source_identity: bool = False) -> None:
    verify_wiki(db, registration)
    reception = read_state(db, registration["root"], registration["vault_id"])
    pending = (reception["received_seq"] != reception["applied_seq"] or reception["armed_epoch"] or reception["uncertain"]
               or db.execute("SELECT 1 FROM filesystem_recoveries WHERE phase<>'complete' LIMIT 1").fetchone()
               or db.execute("SELECT 1 FROM changes WHERE status='prepared' LIMIT 1").fetchone()
               or db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','interrupted') LIMIT 1").fetchone()
               or db.execute("SELECT 1 FROM wiki_job_state WHERE settlement_phase='pending' LIMIT 1").fetchone())
    if pending or (state / "external-change.json").exists() or (state / "external-change.json").is_symlink():
        raise ValueError("Wiki has unfinished writes or filesystem reception; settle them on the source before exporting")
    for row in db.execute("SELECT path,revision FROM documents"):
        path = _within(root, row[0])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row[1]:
            raise ValueError("Wiki content requires source reconciliation before exporting")
    if source_identity:
        for path, key in db.execute("SELECT path,file_key FROM document_identities WHERE path IS NOT NULL"):
            target = _within(root, path)
            if not target.is_file() or key != WikiStore.file_key(target.stat()):
                raise ValueError("Wiki file identity requires source reconciliation before exporting")
    for row in db.execute("""SELECT c.before_hash,c.after_hash,r.before_available,r.after_available
        FROM changes c JOIN change_retention r ON r.change_id=c.id"""):
        for digest, available in ((row[0], row[2]), (row[1], row[3])):
            if digest and available:
                if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    raise ValueError("Wiki history contains an invalid blob identity")
                blob = state / "blobs" / digest
                if not blob.is_file() or hashlib.sha256(blob.read_bytes()).hexdigest() != digest:
                    raise ValueError("Wiki history contains a missing or corrupted retained blob")


def inspect_wiki(state: Path) -> dict:
    """Read-only source admission; no recovery or key/ledger creation."""
    registration = protocols.read_registration(state)
    with _reader(state / "index.sqlite3") as db:
        _stable(db, registration, Path(registration["root"]), state, source_identity=True)
    return registration


def rebind_wiki(staging: Path, home: Path, source_home: Path, registration: dict, target_state: Path) -> None:
    """Rebind a private copy without rewriting immutable historical evidence."""
    def relative(value: Path) -> Path:
        path = value.relative_to(source_home)
        if path == Path(".") or ".." in path.parts:
            raise ValueError("Wiki binding is outside the selected home layout")
        return path

    state = staging / target_state.relative_to(home)
    root_relative = relative(Path(registration["root"]))
    root = staging / root_relative
    stored = protocols.read_registration(state, require_root=False)
    if stored != registration:
        raise ValueError("Wiki registration differs from the archive's declared binding")
    marker = root / ".octopus-vault"
    if marker.is_symlink() or (marker.exists() and marker.read_text().strip() != str(source_home / target_state.relative_to(home))):
        raise ValueError("Wiki vault belongs to another authoritative state")
    with _reader(state / "index.sqlite3") as db:
        _stable(db, registration, root, state)
    restored = registration | {"root": str(home / root_relative), "registration_generation": uuid.uuid4().hex}
    with closing(sqlite3.connect(state / "index.sqlite3")) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA recursive_triggers=ON")
        db.execute("UPDATE filesystem_state SET root=? WHERE singleton=1", (restored["root"],))
        for identifier, path in db.execute("SELECT id,path FROM document_identities WHERE path IS NOT NULL").fetchall():
            target = _within(root, path)
            if target.is_file():
                db.execute("UPDATE document_identities SET file_key=? WHERE id=?", (WikiStore.file_key(target.stat()), identifier))
        # Original targets were excluded by the account contract. Record that
        # fact through the existing acknowledgement owner, never replay them.
        for event, subscriber in db.execute("SELECT event_id,subscriber_id FROM wiki_job_deliveries WHERE acknowledged=0").fetchall():
            job_events.acknowledge(db, event, subscriber, "unavailable")
        db.execute("UPDATE documents SET signature=''")
        db.execute("UPDATE index_state SET checked_at=NULL")
        verify_wiki(db, restored)
    (state / "config.json").write_text(json.dumps(restored, ensure_ascii=False))
    marker.write_text(str(target_state))

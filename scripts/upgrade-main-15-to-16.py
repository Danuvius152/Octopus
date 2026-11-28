#!/usr/bin/env python3
"""Prepare stopped format-15 storage for the release that introduces format 16."""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.account_baseline import persistent_database_file
from server.config import Settings, settings
from server.db.schema import CURRENT_VERSION, SESSION_CONFIGURATION_VERSION_COLUMN
from server.portability import software_version
from server.service_installation import InstallationOwner
from server.storage_verification import _reader, verify_main


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _retained_facts(db: sqlite3.Connection) -> dict[str, str]:
    db.text_factory = lambda value: value.decode("utf-8", errors="surrogateescape")
    digests = {}
    for (table,) in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"):
        columns = [row[1] for row in db.execute(f"PRAGMA table_info({_quoted(table)})")
                   if table != "sessions" or row[1] != "configuration_version"]
        names = ",".join(map(_quoted, columns))
        digest = hashlib.sha256()
        digest.update(repr(columns).encode("utf-8", "backslashreplace"))
        for row in db.execute(f"SELECT rowid,{names} FROM {_quoted(table)} ORDER BY rowid"):
            digest.update(repr(tuple(row)).encode("utf-8", "backslashreplace") + b"\n")
        digests[table] = digest.hexdigest()
    return digests


def _save_baseline(path: Path, baseline: dict) -> None:
    # Publish a complete, synced identity and baseline before any changed SQL.
    with tempfile.NamedTemporaryFile(dir=path.parent) as temporary:
        temporary.write(json.dumps(baseline, sort_keys=True).encode())
        temporary.flush()
        os.fsync(temporary.fileno())
        os.link(temporary.name, path)


def _sync_baseline(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_source(source: Path, path: Path) -> None:
    subprocess.run([sys.executable, "-c", """
import sys
from pathlib import Path
from server.storage_verification import _reader, verify_main
with _reader(Path(sys.argv[1])) as database:
    if verify_main(database) != 15:
        raise RuntimeError('The pinned source must verify main format 15')
""", str(path)], cwd=source, check=True)


def _verify_converted(db: sqlite3.Connection, baseline: dict) -> None:
    if verify_main(db) != 16 or db.execute("SELECT 1 FROM sessions WHERE configuration_version IS NOT 0 LIMIT 1").fetchone():
        raise RuntimeError("Converted storage has noninitial session configuration versions")
    if _retained_facts(db) != baseline["facts"]:
        raise RuntimeError("Converted storage differs from the frozen format-15 facts")


def upgrade_owned(owner: InstallationOwner, source: Path, proof: Path) -> None:
    """Convert under the publisher's existing offline lock."""
    if CURRENT_VERSION != 16:
        raise RuntimeError("This conversion belongs to the format-16 release")
    config = owner.config
    path = owner.offline_database()
    if persistent_database_file(config.db_path) != path:
        raise RuntimeError("The original database is missing or its path changed")
    source = source.resolve(strict=True)
    identity = path.stat()
    fixed = {
        "database": str(path), "device": identity.st_dev, "inode": identity.st_ino,
        "source": str(source), "source_version": software_version(source=source),
        "candidate_version": software_version(),
        "converter": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "config": hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
    }
    baseline = json.loads(proof.read_text()) if proof.exists() else None
    if baseline is not None and baseline["identity"] != fixed:
        raise RuntimeError("The upgrade baseline belongs to a different input")
    with _reader(path) as reader:
        version = reader.execute("PRAGMA user_version").fetchone()[0]
        if version == 16:
            if baseline is None:
                raise RuntimeError("Reentry requires the original upgrade baseline")
            _sync_baseline(proof)
            _verify_converted(reader, baseline)
            return
        if version != 15:
            raise RuntimeError("Only main format 15 can be converted")

    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as writer, writer:
        writer.execute("PRAGMA foreign_keys=ON")
        writer.execute("PRAGMA synchronous=FULL")
        writer.execute("BEGIN IMMEDIATE")
        _verify_source(source, path)
        observed = {"identity": fixed, "facts": _retained_facts(writer)}
        if baseline is None:
            _save_baseline(proof, observed)
            baseline = observed
        elif observed != baseline:
            raise RuntimeError("The source changed after the upgrade baseline was captured")
        _sync_baseline(proof)
        writer.execute("ALTER TABLE sessions ADD COLUMN " + SESSION_CONFIGURATION_VERSION_COLUMN)
        writer.execute("PRAGMA user_version=16")
        _verify_converted(writer, baseline)
    with _reader(path) as reader:
        _verify_converted(reader, baseline)


def upgrade(config: Settings, source: Path, proof: Path) -> None:
    owner = InstallationOwner(config)
    owner.acquire_offline()
    try:
        upgrade_owned(owner, source, proof)
    finally:
        owner.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Pinned format-15 source checkout")
    parser.add_argument("--proof", type=Path, required=True, help="Private, persistent upgrade baseline path")
    parser.add_argument("--config", type=Path, help="Frozen installation settings in JSON")
    args = parser.parse_args()
    config = Settings(_env_file=None, **json.loads(args.config.read_text())) if args.config else settings
    upgrade(config, args.source, args.proof)
    print("Main format 16 verified against the original retained facts")


if __name__ == "__main__":
    main()

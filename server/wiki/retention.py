"""Deterministic retention of text recovery roots, under the vault writer lock."""

import json
import re
from datetime import UTC, datetime, timedelta

from .history import atomic, history_text
from .store import WikiError

POLICY = {
    "history_days": 30,
    "versions_per_document": 10,
    "unresolved_conflict_days": 90,
}


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def snapshot_id(value):
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise WikiError("Invalid snapshot id", 422)
    return value


def retire(history, identifier, reason):
    identifier = snapshot_id(identifier)
    with history.locked():
        file = history.state / "snapshots" / f"{identifier}.json"
        tombstone = history.state / "retired-snapshots" / file.name
        if tombstone.is_file():
            file.unlink(missing_ok=True)
            return json.loads(tombstone.read_text())
        if not file.is_file():
            raise WikiError("Snapshot not found", 404)
        with history.store.connect() as db:
            has_jobs = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='jobs'"
            ).fetchone()
            if has_jobs:
                for row in db.execute(
                    "SELECT payload FROM jobs WHERE status IN ('queued','running','interrupted')"
                ):
                    if json.loads(row[0]).get("snapshot_id") == identifier:
                        raise WikiError(
                            "An active task still protects this snapshot", 409
                        )
        manifest = json.loads(file.read_text())
        receipt = {
            "id": identifier,
            "created_at": manifest["created_at"],
            "reason": manifest["reason"],
            "retired_at": datetime.now(UTC).isoformat(),
            "retirement_reason": reason,
            "recoverable": False,
            "files": {},
        }
        atomic(tombstone, json.dumps(receipt, ensure_ascii=False, indent=2).encode())
        file.unlink()
        return receipt


def collect(history, now=None):
    now = now or datetime.now(UTC)
    ordinary_cutoff = now - timedelta(days=POLICY["history_days"])
    conflict_cutoff = now - timedelta(days=POLICY["unresolved_conflict_days"])
    with history.locked():
        history.store.refresh()
        with history.store.connect() as db:
            rows = db.execute(
                "SELECT c.*,r.document_id,r.before_available,r.after_available,r.resolved_at FROM changes c JOIN change_retention r ON r.change_id=c.id ORDER BY c.created_at DESC,c.rowid DESC"
            ).fetchall()
            prepared = {row["id"] for row in rows if row["status"] == "prepared"}
            protected_hashes, protected_changes = set(), set(prepared)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone():
                for row in db.execute(
                    "SELECT id,items,payload FROM jobs WHERE status IN ('queued','running','interrupted')"
                ):
                    payload = json.loads(row["payload"])
                    protected_changes.update(
                        payload.get("request", {}).get("created_changes", {}).values()
                    )
                    for item in json.loads(row["items"]):
                        protected_hashes.update(
                            item[key]
                            for key in ("revision", "input_revision", "output_revision")
                            if item.get(key)
                        )
                        protected_changes.update(
                            item[key]
                            for key in (
                                "change_id",
                                "output_change_id",
                                "creation_change_id",
                            )
                            if item.get(key)
                        )
                    protected_changes.update(
                        change["id"]
                        for change in rows
                        if change["reason"] == f"Ingest job {row['id']}"
                    )
                    for publication in db.execute("SELECT p.change_id,p.input_revision,p.output_revision FROM job_publications p WHERE p.job_id=?", (row["id"],)):
                        protected_changes.add(publication["change_id"])
                        protected_hashes.update((publication["input_revision"], publication["output_revision"]))
            # Immutable snapshot manifests are roots until explicitly retired.
            for manifest_file in (history.state / "snapshots").glob("*.json"):
                retired = history.state / "retired-snapshots" / manifest_file.name
                if retired.is_file():
                    manifest_file.unlink()
                    continue
                protected_hashes.update(
                    json.loads(manifest_file.read_text())["files"].values()
                )
            current = {
                row["id"]: row["revision"]
                for row in db.execute("SELECT id,revision FROM documents")
            }
            versions = {}
            roots = set(protected_hashes)
            expired_sides = 0
            expired_proposals = 0
            for row in rows:
                recent = timestamp(row["created_at"]) >= ordinary_cutoff
                unresolved = (
                    row["status"] == "conflict"
                    and not row["resolved_at"]
                    and timestamp(row["created_at"]) >= conflict_cutoff
                )
                safe = row["id"] in protected_changes
                for side in ("after", "before"):
                    digest = row[f"{side}_hash"]
                    available = bool(row[f"{side}_available"])
                    keep = (
                        safe
                        or (digest is not None and digest in protected_hashes)
                        or unresolved
                    )
                    if (
                        recent
                        and history_text(row["path"])
                        and (row["status"] != "conflict" or side == "before")
                    ):
                        seen = versions.setdefault(row["document_id"], set())
                        if (
                            digest is None
                            or digest == current.get(row["document_id"])
                            or digest in seen
                        ):
                            keep = True
                        elif len(seen) < POLICY["versions_per_document"]:
                            seen.add(digest)
                            keep = True
                    # Resolved proposals lose their special 90-day protection.
                    # The real before version still follows ordinary retention.
                    if (
                        row["status"] == "conflict"
                        and row["resolved_at"]
                        and side == "after"
                        and not safe
                    ):
                        keep = False
                    keep = available and keep
                    if keep and digest:
                        if (history.blobs / digest).is_file():
                            roots.add(digest)
                        else:
                            keep = False
                    if available and not keep:
                        expired_sides += 1
                        db.execute(
                            f"UPDATE change_retention SET {side}_available=0 WHERE change_id=?",
                            (row["id"],),
                        )
                        db.execute(
                            f"UPDATE change_management SET {side}_state=NULL WHERE change_id=?",
                            (row["id"],),
                        )
                        db.execute(
                            f"UPDATE change_material_origins SET {side}_state=NULL WHERE change_id=?",
                            (row["id"],),
                        )
                if row["status"] == "conflict" and not unresolved and not safe:
                    expired_proposals += db.execute(
                        "DELETE FROM change_proposals WHERE change_id=?", (row["id"],)
                    ).rowcount
            # Commit unavailable-side flags BEFORE unlinking any object. If
            # interrupted, another pass safely collects the remaining orphans.
        removed, removed_bytes = 0, 0
        for file in history.blobs.iterdir():
            if file.is_symlink() or not file.is_file():
                continue
            if not re.fullmatch(r"[a-f0-9]{64}", file.name):
                continue
            if file.name not in roots:
                size = file.stat().st_size
                file.unlink()
                removed += 1
                removed_bytes += size
        result = {
            "at": now.isoformat(),
            "policy": POLICY,
            "expired_sides": expired_sides,
            "expired_proposals": expired_proposals,
            "removed_objects": removed,
            "removed_bytes": removed_bytes,
            "protected_objects": len(roots),
        }
        atomic(
            history.state / "last-cleanup.json", json.dumps(result, indent=2).encode()
        )
        return result

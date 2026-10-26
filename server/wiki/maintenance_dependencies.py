"""Snapshot-local source evidence hashes; never resolve or confirm a binding."""

import json

from . import source_bindings, source_confirmations
from .maintenance_contract import digest
from .references import ParsedReference

VERSION = "direct-maintenance-evidence-v3"


def read(db, documents):
    """The caller has populated temp.maintenance_requested_documents."""
    source_bindings.validate(db)
    heads = db.execute("""SELECT wanted.doc_id,h.observation_id,h.pending_plan,
        o.source_doc,o.revision,o.path,o.occurrences
        FROM maintenance_requested_documents wanted
        LEFT JOIN source_binding_head h ON h.source_doc=wanted.doc_id
        LEFT JOIN source_binding_observations o ON o.id=h.observation_id""").fetchall()
    entries, identifiers = {}, set()
    try:
        for row in heads:
            identifier = row["doc_id"]
            if identifier not in documents:
                continue
            if (not row["observation_id"] or row["source_doc"] != identifier
                    or row["pending_plan"] or row["revision"] != documents[identifier]["revision"]):
                raise ValueError("current source observation is not settled")
            current = json.loads(row["occurrences"])
            if not isinstance(current, list):
                raise ValueError("invalid source occurrences")
            for ordinal, entry in enumerate(current):
                if (not isinstance(entry, dict) or entry["ordinal"] != ordinal
                        or not isinstance(entry["binding_id"], str)):
                    raise ValueError("invalid source occurrence")
                identifiers.add(entry["binding_id"])
            entries[identifier] = current
        db.execute("CREATE TEMP TABLE IF NOT EXISTS maintenance_requested_bindings(id TEXT PRIMARY KEY) WITHOUT ROWID")
        db.execute("DELETE FROM maintenance_requested_bindings")
        db.executemany("INSERT INTO maintenance_requested_bindings VALUES(?)", [(value,) for value in identifiers])
        bindings = {row["requested_id"]: dict(row) for row in db.execute("""SELECT wanted.id AS requested_id,b.*,
            d.path AS target_path,d.revision AS target_revision
            FROM maintenance_requested_bindings wanted LEFT JOIN source_bindings b ON b.id=wanted.id
            LEFT JOIN documents d ON d.id=b.target_doc""")}
        edges = {(row["source_doc"], row["ordinal"]): dict(row) for row in db.execute("""SELECT e.*
            FROM maintenance_requested_documents wanted JOIN wiki_edges e ON e.source_doc=wanted.doc_id
            WHERE e.relation IN ('derived_from','representation_of')""")}
        confirmations = source_confirmations.read(db, [doc for doc, values in entries.items() if values])
        result = {}
        for identifier, current in entries.items():
            evidence, warnings = [], {}
            for entry in current:
                reference = ParsedReference.restore(entry["reference"])
                binding = bindings[entry["binding_id"]]
                selector = source_bindings.selector(reference)
                if (binding["id"] is None or binding["source_doc"] != identifier
                        or binding["selector"] != selector or entry["selector"] != selector):
                    raise ValueError("source proof does not belong to this occurrence")
                recorded = entry.get("recorded_revision")
                recorded = recorded if isinstance(recorded, str) else None
                policy = binding["policy"]
                confirmation = confirmations.get((identifier, entry["ordinal"]))
                status = source_confirmations.status(policy, recorded, binding["target_revision"], confirmation)
                if reference.relation == "derived_from" and status in {"changed", "missing", "version_unknown", "unknown"}:
                    warnings[status] = warnings.get(status, 0) + 1
                # Locator/reference evidence and the *current* proof are distinct
                # from the old unresolved fields inside the immutable observation.
                evidence.append({"ordinal": entry["ordinal"], "occurrence": entry["occurrence_id"],
                                 "selector": selector, "reference": reference.record(), "source": entry.get("source"),
                                 "recorded_revision": recorded, "binding": binding["id"],
                                 "binding_reference": json.loads(binding["reference"]), "base": binding["base"],
                                 "policy": policy, "target": binding["target_doc"], "proof": json.loads(binding["proof"]),
                                 "current_path": binding["target_path"], "current_revision": binding["target_revision"],
                                 "status": status, "confirmation": confirmation,
                                 "edge": edges.get((identifier, entry["ordinal"]))})
            result[identifier] = (digest({"version": VERSION, "occurrences": evidence}), warnings)
        if set(result) != set(documents):
            raise ValueError("current source observation is missing")
        return result
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        raise source_bindings.unavailable("current dependency evidence is missing or incompatible") from error

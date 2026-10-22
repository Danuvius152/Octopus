"""Response-time permissions context and live links over cached neutral facts."""

import json
from pathlib import PurePosixPath

from . import catalog, maintenance_inputs
from .index_state import maintenance_blockers
from .links import note_name
from .maintenance_contract import PURPOSE, digest
from .maintenance_projection import Batch, bounds, dirty_ids, readiness
from .references import candidate_keys, uri_reference
from .store import WikiError


class MaintenanceSnapshot:
    """Read readiness, generation and selected summaries in the same snapshot."""

    def __init__(self, store, registration, user_id, db, actor="human", *, identifiers=None):
        self.store, self.registration, self.user_id, self.db = store, registration, user_id, db
        self.actor = actor
        self.index = readiness(db, store)
        self.ready = self.index["index_status"] in {"ready", "partial"}
        state = maintenance_inputs.state(db)
        self.generation = digest([user_id, registration, actor, PURPOSE, state, self.index])
        self.documents, self.paths = {}, {}
        self.accepted, self.receipts, self.batch = {}, [], None
        if identifiers is not None:
            self.batch = Batch(db, identifiers)
            self.accepted, self.receipts = self.batch.accepted, self.batch.receipts
            self.documents.update(self.batch.documents)
            self.scope_ready(identifiers=identifiers)

    def scope_ready(self, *, identifiers=None, directory="Library"):
        normalized = True
        if identifiers is not None:
            normalized = maintenance_inputs.ready(self.db, identifiers)
        elif self.db.execute("SELECT 1 FROM maintenance_job_dirty LIMIT 1").fetchone():
            normalized = self.db.execute("""SELECT 1 FROM documents d
                CROSS JOIN maintenance_job_members m ON m.doc_id=d.id
                CROSS JOIN maintenance_job_dirty pending ON pending.job_id=m.job_id
                WHERE d.path>=? AND d.path<? LIMIT 1""", bounds(directory)).fetchone() is None
        if not normalized or dirty_ids(self.db, identifiers=identifiers, directory=directory, limit=1):
            self.ready = False
            if self.index["index_status"] == "ready":
                self.index["index_status"] = "updating"
        return self.ready

    def load_page(self, rows):
        stored = [json.loads(row["summary_json"]) for row in rows]
        self.documents.update({value["current"]["id"]: value["current"] for value in stored})
        identifiers = [value["current"]["id"] for value in stored]
        if identifiers:
            for identifier in identifiers:
                self.paths[identifier] = set()
            for row in self.db.execute("SELECT doc_id,path FROM maintenance_accepted_paths WHERE doc_id IN (" +
                                       ",".join("?" for _ in identifiers) + ")", identifiers):
                self.paths[row["doc_id"]].add(row["path"])
        return stored

    def project(self, identifier, stored=None):
        if stored is None:
            row = self.db.execute("SELECT * FROM maintenance_projection WHERE doc_id=?", (identifier,)).fetchone()
            if row is None or dirty_ids(self.db, identifiers=[identifier], limit=1):
                self.ready = False
                if self.batch is None:
                    raise WikiError("Maintenance projection is updating; retry the observation", 503)
                value = self.batch.calculate().get(identifier)
                stored = value["summary"] if value else None
                if stored is None:
                    raise WikiError("Current Markdown document not found", 404)
            else:
                stored = self.load_page([row])[0]
        self.documents[identifier] = stored["current"]
        return {key: value for key, value in stored.items() if key != "dependency_generation"} | {
            "actionable": self.ready and stored["actionable"] and not maintenance_blockers(self.db, [identifier]),
            "historical_path_links": len(self.impact(identifier)),
            "review": self.review(stored)}

    def review(self, stored):
        """Build the complete observation without loading live navigation."""
        observation = {"doc": stored["current"]["id"], "path": stored["current"]["path"],
                       "revision": stored["current"]["revision"],
                       "baseline_receipt_id": stored["baseline"]["id"] if stored["baseline"] else None,
                       "dependency_generation": stored["dependency_generation"]}
        observation["review_key"] = "wiki-daily-v1:" + digest([self.user_id, self.registration, self.actor, PURPOSE, observation])
        return observation

    def impact(self, identifier):
        current = self.documents.get(identifier)
        if current is None:
            row = self.db.execute("SELECT id,path,revision FROM documents WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise WikiError("Current Markdown document not found", 404)
            current = dict(row)
            self.documents[identifier] = current
        if identifier not in self.paths:
            self.paths[identifier] = {row[0] for row in self.db.execute(
                "SELECT path FROM maintenance_accepted_paths WHERE doc_id=?", (identifier,))}
        old_paths = self.paths[identifier] - {current["path"]}
        if not old_paths:
            return []
        edges = {}
        for old in old_paths:
            for edge in self.db.execute("""SELECT e.*,d.path AS source_path FROM wiki_edge_lookups l
                    JOIN wiki_edges e ON e.source_doc=l.source_doc AND e.ordinal=l.ordinal
                    JOIN documents d ON d.id=e.source_doc
                    WHERE ((l.namespace='path' AND l.key=?) OR (l.namespace='name' AND l.key=?))
                    AND e.relation IN ('wikilink','markdown_link')""", (old, note_name(old))):
                edges[edge["source_doc"], edge["ordinal"]] = edge
        result = []
        for edge in sorted(edges.values(), key=lambda edge: (edge["source_path"], edge["source_doc"], edge["ordinal"])):
            reference = uri_reference(edge["resource"], edge["relation"], wiki=edge["relation"] == "wikilink")
            keys = candidate_keys(reference, PurePosixPath(edge["source_path"]).parent.as_posix(), edge["source_doc"])
            matched = next((old for old in sorted(old_paths) if ("path", old) in keys), None)
            if matched is None and edge["relation"] == "wikilink" and ("name", note_name(reference.value)) in keys:
                others = [row[0] for row in catalog.name_matches(self.db, note_name(reference.value)) if row[0] != identifier]
                if not others:
                    matched = next((old for old in sorted(old_paths) if note_name(old) == note_name(reference.value)), None)
            if matched is not None:
                result.append({"source_doc": edge["source_doc"], "source_path": edge["source_path"],
                               "matched_path": matched, "relation": edge["relation"], "status": edge["status"]})
        return result

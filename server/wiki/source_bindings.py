"""Durable source identity proofs, independent of the disposable graph index."""

import json
import uuid
from dataclasses import replace
from pathlib import PurePosixPath

from . import format as document_format
from . import portable_metadata, resolver
from .extract import extract
from .references import ParsedReference, candidate_keys, source_reference

TABLES = {
    "source_bindings": ("id", "source_doc", "selector", "reference", "base", "policy", "target_doc", "proof"),
    "source_binding_observations": ("id", "source_doc", "revision", "context", "path", "occurrences"),
    "source_binding_head": ("source_doc", "observation_id", "pending_plan"),
    "change_source_bindings": ("change_id", "before_plan", "after_plan"),
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def unavailable(message):
    from .store import WikiError

    return WikiError("Source-binding ledger " + message + "; durable state retained", 503)


def initialize(db):
    db.execute("""CREATE TABLE source_bindings(
        id TEXT PRIMARY KEY,source_doc TEXT NOT NULL,selector TEXT NOT NULL,
        reference TEXT NOT NULL,base TEXT NOT NULL,policy TEXT NOT NULL
        CHECK(policy IN ('bound','unresolved','unknown','external')),
        target_doc TEXT,proof TEXT NOT NULL,
        CHECK((policy='bound')=(target_doc IS NOT NULL)))""")
    db.execute("CREATE INDEX source_binding_selector ON source_bindings(source_doc,selector)")
    db.execute("""CREATE TABLE source_binding_observations(
        id TEXT PRIMARY KEY,source_doc TEXT NOT NULL,revision TEXT,context TEXT NOT NULL,
        path TEXT NOT NULL,occurrences TEXT NOT NULL,UNIQUE(source_doc,context))""")
    db.execute("""CREATE TABLE source_binding_head(
        source_doc TEXT PRIMARY KEY,observation_id TEXT,pending_plan TEXT)""")
    db.execute("""CREATE TABLE change_source_bindings(
        change_id TEXT PRIMARY KEY,before_plan TEXT NOT NULL,after_plan TEXT NOT NULL)""")
    db.execute("""CREATE TRIGGER source_binding_immutable BEFORE UPDATE ON source_bindings
        WHEN old.policy IN ('bound','unknown','external') OR new.id<>old.id
          OR new.source_doc<>old.source_doc OR new.selector<>old.selector
          OR new.reference<>old.reference OR new.base<>old.base
        BEGIN SELECT RAISE(ABORT,'source identity proof is immutable'); END""")
    for action in ("UPDATE", "DELETE"):
        db.execute(f"""CREATE TRIGGER source_observation_no_{action.lower()}
            BEFORE {action} ON source_binding_observations
            BEGIN SELECT RAISE(ABORT,'source observation is immutable'); END""")


def validate(db):
    for table, expected in TABLES.items():
        if tuple(row[1] for row in db.execute(f"PRAGMA table_xinfo({table})")) != expected:
            raise unavailable("is missing or incompatible")


def entries(metadata):
    result = []
    for source in metadata.get("sources", []):
        if isinstance(source, dict):
            result.append({"reference": source_reference(source).record(),
                           "recorded_revision": source.get("revision") if isinstance(source.get("revision"), str) else None,
                           "label": source.get("id") if isinstance(source.get("id"), str) else None,
                           "source": source})
    representation = metadata.get("x_wiki", {}).get("representation_of")
    if isinstance(representation, str):
        result.append({"reference": source_reference({"resource": representation}, "representation_of").record(),
                       "recorded_revision": None, "label": None, "source": {"resource": representation}})
    return result


def metadata(db, doc_id, path, raw, management):
    if raw is None:
        return {}
    extracted = extract(raw, PurePosixPath(path).suffix.lower())
    text, result = extracted["text"], extracted["metadata"]
    parsed = document_format.parse(text)
    try:
        result |= portable_metadata.from_document(parsed)
    except (ValueError, TypeError, RecursionError):
        pass
    if parsed.error:
        previous = db.execute("SELECT metadata FROM documents WHERE id=?", (doc_id,)).fetchone()
        if previous:
            old = json.loads(previous[0])
            if not old.get("frontmatter_error") or old.get("frontmatter_retained_from"):
                result |= {key: old[key] for key in ("sources", "x_wiki") if key in old}
    result |= parsed.metadata(result)
    managed = (management or {}).get("metadata", {})
    return result | (json.loads(managed) if isinstance(managed, str) else managed)


def selector(reference):
    # Unknown legacy tokens must not all collapse to the same empty value.
    return encoded(reference.selection + ((reference.raw_resource, reference.diagnostic)
                   if reference.namespace in {"unknown", "invalid"} else ()))


def observation(db, identifier):
    row = db.execute("SELECT * FROM source_binding_observations WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise unavailable("observation is missing")
    return {key: row[key] for key in row.keys() if key != "occurrences"} | {
        "entries": json.loads(row["occurrences"]), "confirmed": True}


def current(db, doc_id):
    head = db.execute("SELECT * FROM source_binding_head WHERE source_doc=?", (doc_id,)).fetchone()
    return observation(db, head["observation_id"]) if head and head["observation_id"] else None


def capture(db, doc_id, path, raw, management):
    from .store import revision

    digest = revision(raw)
    wanted = entries(metadata(db, doc_id, path, raw, management))
    saved = current(db, doc_id)
    if saved and saved["revision"] == digest and same_entries(saved["entries"], wanted):
        return saved
    return seal(db, plan(db, doc_id, path, digest, wanted,
                         "capture:" + uuid.uuid4().hex, prior=saved, historical=True), resolve_new=False)


def same_entries(left, right):
    fields = ("reference", "recorded_revision", "label", "source")
    return encoded([{key: item.get(key) for key in fields} for item in left]) == encoded(
        [{key: item.get(key) for key in fields} for item in right])


def plan(db, doc_id, path, revision, source_entries, context, *, prior=None, historical=False):
    """Reserve proof/occurrence IDs without confirming any proposed after facts."""
    prior = current(db, doc_id) if prior is None else prior
    previous = {}
    if prior:
        for entry in prior["entries"]:
            previous.setdefault(selector(ParsedReference.restore(entry["reference"])), []).append(entry)
    planned = []
    for ordinal, source in enumerate(source_entries):
        reference = ParsedReference.restore(source["reference"])
        key = selector(reference)
        candidates = previous.get(key, [])
        # Exact selected observations take precedence, including a historical
        # unknown policy with the same bytes as a separately proved version.
        bindings = [db.execute("SELECT * FROM source_bindings WHERE id=? AND source_doc=?",
                              (entry["binding_id"], doc_id)).fetchone() for entry in candidates]
        if any(binding is None for binding in bindings):
            raise unavailable("proof is missing")
        if not bindings and not historical:
            bindings = db.execute("SELECT * FROM source_bindings WHERE source_doc=? AND selector=? ORDER BY rowid",
                                  (doc_id, key)).fetchall()
        signatures = {(binding["policy"], binding["target_doc"], binding["base"]) for binding in bindings}
        binding = bindings[0] if len(signatures) == 1 else None
        if binding is None and bindings:
            policy = "unknown"
        elif binding is not None:
            policy = binding["policy"]
        else:
            policy = "external" if reference.namespace == "external" else "unknown" if historical or reference.namespace in {"unknown", "invalid"} else "unresolved"
        planned.append(source | {
            "ordinal": ordinal, "occurrence_id": uuid.uuid4().hex,
            "binding_id": binding["id"] if binding else uuid.uuid4().hex,
            "selector": key, "base": binding["base"] if binding else PurePosixPath(path).parent.as_posix(),
            "policy": policy, "target_doc": binding["target_doc"] if binding else None,
            "proof": json.loads(binding["proof"]) if binding else {"context": context, "policy": policy},
        })
    return {"version": 1, "id": uuid.uuid4().hex, "source_doc": doc_id, "path": path,
            "revision": revision, "context": context, "entries": planned, "confirmed": False}


def selected_plan(db, doc_id, path, revision, source_entries, context, selected):
    """Restore an exact side, reowning proved targets when restoration is a copy."""
    if selected is None or not selected.get("confirmed"):
        return plan(db, doc_id, path, revision, source_entries, context, prior={"entries": []}, historical=True)
    if not same_entries(selected["entries"], source_entries):
        raise unavailable("restore evidence does not match the selected side")
    if selected["source_doc"] == doc_id:
        return plan(db, doc_id, path, revision, source_entries, context, prior=selected, historical=True)
    copied = plan(db, doc_id, path, revision, source_entries, context, prior={"entries": []}, historical=True)
    for target, old in zip(copied["entries"], selected["entries"], strict=True):
        # An unresolved occurrence may have gained a durable target since this
        # observation. Copy that proof by value, retaining the selected raw
        # evidence without attaching another source document's binding.
        binding = db.execute("SELECT base,policy,target_doc,proof FROM source_bindings WHERE id=? AND source_doc=? AND selector=?",
                             (old["binding_id"], selected["source_doc"], target["selector"])).fetchone()
        if binding is None:
            raise unavailable("proof is missing or belongs to a different source or selector")
        target.update({key: binding[key] for key in ("base", "policy", "target_doc")})
        target["proof"] = json.loads(binding["proof"])
    return copied


def incoming(db, doc_id, path, revision, source_entries, policy):
    context = policy["context"]
    previous = policy.get("prior")
    mode = policy["mode"]
    if mode == "selected":
        return selected_plan(db, doc_id, path, revision, source_entries, context, previous)
    if mode == "fresh":
        return plan(db, doc_id, path, revision, source_entries, context, prior={"entries": []})
    if mode != "continuation" or not previous or not previous.get("confirmed"):
        return plan(db, doc_id, path, revision, source_entries, context, prior={"entries": []}, historical=True)
    if previous["source_doc"] != doc_id:
        copied = selected_plan(db, doc_id, previous["path"], previous["revision"],
                               previous["entries"], context + ":captured", previous)
        previous = seal(db, copied, resolve_new=False)
    return plan(db, doc_id, path, revision, source_entries, context, prior=previous)


def resolution(db, entry):
    reference = ParsedReference.restore(entry["reference"])
    binding = db.execute("SELECT * FROM source_bindings WHERE id=?", (entry["binding_id"],)).fetchone()
    if binding is None:
        raise unavailable("proof is missing")
    keys = candidate_keys(reference, binding["base"], binding["source_doc"])
    if binding["policy"] in {"unknown", "external"}:
        return resolver.Resolution(None, binding["policy"], keys)
    if binding["target_doc"]:
        target = binding["target_doc"]
        exact = replace(reference, namespace="doc", value=target, identities=(("doc", target),))
        resolved = resolver.resolve(db, exact, binding["base"], source_doc=binding["source_doc"])
        return resolver.Resolution(target, resolved.status, tuple(dict.fromkeys((*keys, ("doc", target)))))
    resolved = resolver.resolve(db, reference, binding["base"], source_doc=binding["source_doc"])
    if resolved.target:
        db.execute("UPDATE source_bindings SET policy='bound',target_doc=?,proof=? WHERE id=? AND policy='unresolved' AND target_doc IS NULL",
                   (resolved.target, encoded({"context": entry.get("proof", {}).get("context"), "target_doc": resolved.target}), binding["id"]))
    return resolved


def seal(db, draft, *, resolve_new=True):
    existing = db.execute("SELECT id FROM source_binding_observations WHERE source_doc=? AND context=?",
                          (draft["source_doc"], draft["context"])).fetchone()
    if existing:
        saved = observation(db, existing[0])
        if saved["revision"] != draft["revision"] or not same_entries(saved["entries"], draft["entries"]):
            raise unavailable("observation context was reused for different evidence")
        return saved
    for item in draft["entries"]:
        binding = db.execute("SELECT source_doc,selector FROM source_bindings WHERE id=?", (item["binding_id"],)).fetchone()
        if binding and (binding["source_doc"], binding["selector"]) != (draft["source_doc"], item["selector"]):
            raise unavailable("proof belongs to a different source or selector")
        if not binding:
            policy = item["policy"]
            if policy == "unresolved" and not resolve_new:
                policy = "unknown"
            db.execute("INSERT INTO source_bindings VALUES(?,?,?,?,?,?,?,?)", (
                item["binding_id"], draft["source_doc"], item["selector"], encoded(item["reference"]),
                item["base"], policy, item["target_doc"], encoded(item["proof"])))
        if resolve_new:
            resolution(db, item)
        binding = db.execute("SELECT policy,target_doc,proof FROM source_bindings WHERE id=?", (item["binding_id"],)).fetchone()
        item = item | {"policy": binding["policy"], "target_doc": binding["target_doc"], "proof": json.loads(binding["proof"])}
        draft["entries"][item["ordinal"]] = item
    db.execute("INSERT INTO source_binding_observations VALUES(?,?,?,?,?,?)", (
        draft["id"], draft["source_doc"], draft["revision"], draft["context"], draft["path"], encoded(draft["entries"])))
    saved = observation(db, draft["id"])
    change_id = draft.get("change_id")
    if change_id:
        db.execute("UPDATE change_source_bindings SET after_plan=? WHERE change_id=?", (encoded(saved), change_id))
    return saved


def stage(db, draft):
    db.execute("""INSERT INTO source_binding_head VALUES(?,NULL,?)
        ON CONFLICT(source_doc) DO UPDATE SET pending_plan=excluded.pending_plan""",
               (draft["source_doc"], encoded(draft)))
    db.execute("INSERT OR IGNORE INTO catalog_dirty VALUES(?)", (draft["source_doc"],))
    db.execute("UPDATE catalog_state SET pending=1 WHERE singleton=1")
    db.execute("UPDATE documents SET signature='' WHERE id=?", (draft["source_doc"],))


def index(db, row, metadata):
    wanted = entries(metadata)
    head = db.execute("SELECT * FROM source_binding_head WHERE source_doc=?", (row["id"],)).fetchone()
    active = observation(db, head["observation_id"]) if head and head["observation_id"] else None
    if head and head["pending_plan"]:
        pending = json.loads(head["pending_plan"])
        if pending.get("unknown_current"):
            pending = plan(db, row["id"], row["path"], row["revision"], wanted,
                           pending["context"], prior={"entries": []}, historical=True)
        if pending["revision"] == row["revision"] and same_entries(pending["entries"], wanted):
            active = seal(db, pending)
        else:
            # The applied version was edited again before indexing. Keep its
            # exact evidence, without resolving old unconfirmed names today.
            active = seal(db, pending, resolve_new=False)
    if active is None or active["revision"] != row["revision"] or active["path"] != row["path"] or not same_entries(active["entries"], wanted):
        active = seal(db, plan(db, row["id"], row["path"], row["revision"], wanted,
                               "filesystem:" + uuid.uuid4().hex, prior=active))
    db.execute("""INSERT INTO source_binding_head VALUES(?,?,NULL)
        ON CONFLICT(source_doc) DO UPDATE SET observation_id=excluded.observation_id,pending_plan=NULL""",
               (row["id"], active["id"]))
    return [(entry, resolution(db, entry)) for entry in active["entries"]]


def retire(db, doc_id):
    head = db.execute("SELECT pending_plan FROM source_binding_head WHERE source_doc=?", (doc_id,)).fetchone()
    if head and head[0]:
        draft = json.loads(head[0])
        if draft["revision"] is None:
            saved = seal(db, draft, resolve_new=False)
            db.execute("UPDATE source_binding_head SET observation_id=?,pending_plan=NULL WHERE source_doc=?",
                       (saved["id"], doc_id))


def record(db, change_id, doc_id, path, before, after, before_management, after_management,
           *, selected=None, before_selected=None, transition=None):
    from .store import revision

    before_revision = revision(before) if before is not None else None
    before_entries = entries(metadata(db, doc_id, path, before, before_management))
    if before_selected is not None:
        before_draft = selected_plan(db, doc_id, path, before_revision, before_entries,
                                     change_id + ":before", before_selected.get("plan"))
    else:
        before_draft = plan(db, doc_id, path, before_revision, before_entries,
                            change_id + ":before", historical=True)
    before_plan = seal(db, before_draft, resolve_new=False)
    after_revision = revision(after) if after is not None else None
    after_entries = entries(metadata(db, doc_id, path, after, after_management))
    if selected is not None:
        after_plan = selected_plan(db, doc_id, path, after_revision, after_entries, change_id + ":after", selected.get("plan"))
    elif transition is not None:
        after_plan = incoming(db, doc_id, path, after_revision, after_entries, transition | {"context": change_id + ":after"})
    else:
        after_plan = plan(db, doc_id, path, after_revision, after_entries,
                          change_id + ":after", prior=before_plan)
    after_plan["change_id"] = change_id
    db.execute("INSERT INTO change_source_bindings VALUES(?,?,?)", (change_id, encoded(before_plan), encoded(after_plan)))


def change_plan(db, change_id, side):
    if side not in {"before", "after"}:
        raise ValueError("Invalid source observation side")
    row = db.execute(f"SELECT {side}_plan FROM change_source_bindings WHERE change_id=?", (change_id,)).fetchone()
    return json.loads(row[0]) if row else None


def apply(db, change_id):
    draft = change_plan(db, change_id, "after")
    if draft is None:
        # Prepared journals from schema 9 have bytes but no exact proof.
        row = db.execute("""SELECT c.*,r.document_id,m.after_state FROM changes c
            JOIN change_retention r ON r.change_id=c.id
            LEFT JOIN change_management m ON m.change_id=c.id WHERE c.id=?""", (change_id,)).fetchone()
        if row is None:
            raise unavailable("applied change is missing")
        # The index must receive unknown policy before looking at old bytes.
        draft = {"version": 1, "id": uuid.uuid4().hex, "source_doc": row["document_id"],
                 "path": row["path"], "revision": row["after_hash"], "context": change_id + ":legacy-after",
                 "entries": [], "confirmed": False, "unknown_current": True}
    stage(db, draft)

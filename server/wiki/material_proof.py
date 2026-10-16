"""Reconstruct submitted bytes from a current file and a private byte map.

Writers supply their actual copy/insert operations, never a guessed text diff.
Only replaced original frontmatter may be retained as literal bytes. Original
prose is always read back from the current file in its original source order.
The durable state owner and recovery protocol remain in origins.py.
"""

import base64
import binascii
import json
from bisect import bisect_right
from dataclasses import dataclass

from . import format, origins
from .store import WikiError, revision

FORMAT = "raw-byte-map-v1"


def prefix_size(raw):
    text = raw.decode("utf-8")
    end = format.metadata_end(text)
    return len(text[:end].encode())


def gaps(size, ranges):
    cursor, result = 0, []
    for first, last in sorted(ranges):
        if not 0 <= cursor <= first < last <= size:
            raise ValueError("Overlapping or invalid source byte ranges")
        if cursor < first:
            result.append((cursor, first))
        cursor = last
    if cursor < size:
        result.append((cursor, size))
    return result


def region(raw, first, last):
    return {"offset": first, "size": last - first, "sha256": revision(raw[first:last])}


def reconstruct(state, doc_id, raw):
    """Unknown, invalid or stale state never grants original-material reuse."""
    if (not state or state.get("status") != "valid" or state.get("doc_id") != doc_id
            or state.get("revision") != revision(raw)):
        return None
    proof = state.get("proof")
    if not isinstance(proof, dict) or proof.get("format") != FORMAT:
        return None
    try:
        size, prefix = proof["source_size"], proof["source_prefix_size"]
        if type(size) is not int or type(prefix) is not int or not 0 <= prefix <= size:
            return None
        result, ranges, cursor = [], [], 0
        for piece in proof["pieces"]:
            length = piece["size"]
            if type(length) is not int or length <= 0:
                return None
            if "literal" in piece:
                if set(piece) != {"literal", "size", "sha256"} or cursor + length > prefix:
                    return None
                value = base64.b64decode(piece["literal"], validate=True)
            else:
                first = piece["offset"]
                if type(first) is not int or not 0 <= first < first + length <= len(raw):
                    return None
                if set(piece) != {"offset", "size", "sha256"}:
                    return None
                value = raw[first:first + length]
                ranges.append((first, first + length))
            if len(value) != length or revision(value) != piece["sha256"]:
                return None
            result.append(value)
            cursor += length
        if cursor != size:
            return None
        expected = [region(raw, first, last) for first, last in gaps(len(raw), ranges)]
        if proof["generated"] != expected:
            return None
        original = b"".join(result)
        return original if revision(original) == proof["source_hash"] and prefix_size(original) == prefix else None
    except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error):
        return None


def seeded(doc_id, raw, source):
    digest = revision(raw)
    return {
        "version": 1, "doc_id": doc_id, "revision": digest, "status": "valid",
        "proof": {
            "format": FORMAT, "source_hash": digest, "source_size": len(raw),
            "source_prefix_size": prefix_size(raw), "source": source,
            "pieces": [region(raw, 0, len(raw))] if raw else [], "generated": [],
        },
    }


def historical_preimage(db, doc_id, raw):
    """Recover an existing claim, never reinterpret a missing claim as a first one."""
    digest = revision(raw)
    known, latest = False, None
    rows = db.execute(
        """SELECT c.status,c.before_hash,c.after_hash,r.before_available,r.after_available,
          o.before_state,o.after_state FROM change_material_origins o
        JOIN change_retention r ON r.change_id=o.change_id
        JOIN changes c ON c.id=o.change_id
        WHERE r.document_id=? ORDER BY c.rowid DESC""", (doc_id,),
    )
    for row in rows:
        for side in ("after", "before"):
            encoded = row[f"{side}_state"]
            if encoded is None:
                # Retention deliberately expires payloads to SQL NULL. It does
                # not turn their identity's past into "never established".
                known = True
                continue
            try:
                state = json.loads(encoded)
            except (ValueError, TypeError):
                state = False
            if state is None:
                # A recorded JSON null is a known absence, unlike an expired side.
                continue
            known = True
            eligible = row[f"{side}_available"] and (side == "before" or row["status"] == "applied")
            if isinstance(state, dict):
                if latest is None:
                    latest = state
                if eligible and row[f"{side}_hash"] == digest:
                    return origins.preimage(state, doc_id, digest), True
            elif eligible and row[f"{side}_hash"] == digest:
                break  # A malformed current historical claim cannot be skipped for an older one.
        else:
            continue
        break
    if known:
        previous = latest if latest is not None else {"version": 1}
        return origins.invalidated(previous, doc_id, digest, "Prior origin is unavailable or no longer verifies this preimage"), True
    return None, False


def ingest(job_id):
    """Resolve the seed inside the same prepared transaction as identity creation."""
    def build(db, doc_id, before, after):
        row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        if (before is not None or after is None or row is None
                or json.loads(row[0]).get("material", "").encode() != after):
            raise WikiError("Material proof requires the actual new job input", 409)
        return None, seeded(doc_id, after, {"kind": "ingest", "job_id": job_id})
    return build


class Trace:
    """Ephemeral output pieces referencing the exact operation's input bytes."""

    def __init__(self, text):
        self.before = text.encode()
        self.pieces = [(0, len(self.before))] if self.before else []

    @property
    def raw(self):
        return b"".join(self.before[p[0]:p[1]] if isinstance(p, tuple) else p
                        for p in self.pieces)

    @property
    def text(self):
        return self.raw.decode("utf-8")

    def replace(self, chunks):
        """Copy ranges of the current intermediate bytes or insert new bytes."""
        current, replacement, copied = self.raw, [], []
        for chunk in chunks:
            if isinstance(chunk, bytes):
                if chunk:
                    replacement.append(chunk)
                continue
            first, last = chunk
            if not 0 <= first <= last <= len(current):
                raise WikiError("Invalid controlled byte transformation", 422)
            if first == last:
                continue
            copied.append((first, last))
            offset = 0
            for piece in self.pieces:
                length = piece[1] - piece[0] if isinstance(piece, tuple) else len(piece)
                start, end = max(first, offset), min(last, offset + length)
                if start < end:
                    replacement.append((piece[0] + start - offset, piece[0] + end - offset)
                                       if isinstance(piece, tuple) else piece[start - offset:end - offset])
                offset += length
                if offset >= last:
                    break
        try:
            gaps(len(current), copied)
        except ValueError as exc:
            raise WikiError("Controlled byte copies must not overlap", 422) from exc
        self.pieces = replacement

    def edit(self, edits):
        text, chunks, cursor = self.text, [], 0
        for first, last, replacement in sorted(edits, key=lambda edit: (edit[0], edit[1])):
            if not cursor <= first <= last <= len(text):
                raise WikiError("Invalid controlled text transformation", 422)
            start = len(text[:first].encode())
            chunks.extend(((len(text[:cursor].encode()), start), replacement.encode()))
            cursor = last
        chunks.append((len(text[:cursor].encode()), len(self.raw)))
        self.replace(chunks)

    def metadata(self, patched):
        """Metadata patching may replace a prefix, never guess a body diff."""
        before, after = self.raw, patched.encode()
        if before == after:
            return
        first, last = prefix_size(before), prefix_size(after)
        if before[first:] != after[last:]:
            raise WikiError("Metadata writer changed the original body", 422)
        self.replace([after[:last], (first, len(before))])
        if self.raw != after:
            raise WikiError("Metadata byte trace does not match the writer", 422)

    def transition(self, publication=None, *, allow_seed=False):
        return Transition(self, publication, allow_seed)


def compact(raw, pieces):
    """Keep the recipe proportional to current layout, not operation count."""
    result = []
    for piece in pieces:
        previous = result[-1] if result else None
        if previous and "offset" in previous and "offset" in piece and previous["offset"] + previous["size"] == piece["offset"]:
            result[-1] = region(raw, previous["offset"], piece["offset"] + piece["size"])
        elif previous and "literal" in previous and "literal" in piece:
            value = base64.b64decode(previous["literal"]) + base64.b64decode(piece["literal"])
            result[-1] = {"literal": base64.b64encode(value).decode("ascii"),
                          "size": len(value), "sha256": revision(value)}
        else:
            result.append(piece)
    return result


def compose(state, doc_id, before, after, copies):
    original = reconstruct(state, doc_id, before)
    if original is None:
        return origins.invalidated(state, doc_id, revision(after), "Original bytes did not verify")
    proof = state["proof"]
    pieces, source_cursor = [], 0
    # Copies are (old_start, old_end, new_start), independent of their new order.
    ordered = sorted(copies)
    starts = [copy[0] for copy in ordered]
    for piece in proof["pieces"]:
        if "literal" in piece:
            pieces.append(piece)
            source_cursor += piece["size"]
            continue
        first, last = piece["offset"], piece["offset"] + piece["size"]
        cursor = first
        index = max(0, bisect_right(starts, first) - 1)
        while index < len(ordered):
            old_first, old_last, new_first = ordered[index]
            index += 1
            if old_first >= last:
                break
            start, end = max(first, old_first), min(last, old_last)
            if start >= end:
                continue
            if cursor < start:
                if source_cursor + start - first > proof["source_prefix_size"]:
                    return origins.invalidated(state, doc_id, revision(after), "Controlled edit changed original material")
                value = before[cursor:start]
                pieces.append({"literal": base64.b64encode(value).decode("ascii"),
                               "size": len(value), "sha256": revision(value)})
            pieces.append(region(after, new_first + start - old_first, new_first + end - old_first))
            cursor = end
        if cursor < last:
            if source_cursor + last - first > proof["source_prefix_size"]:
                return origins.invalidated(state, doc_id, revision(after), "Controlled edit changed original material")
            value = before[cursor:last]
            pieces.append({"literal": base64.b64encode(value).decode("ascii"),
                           "size": len(value), "sha256": revision(value)})
        source_cursor += piece["size"]
    pieces = compact(after, pieces)
    ranges = [(p["offset"], p["offset"] + p["size"]) for p in pieces if "offset" in p]
    try:
        generated = [region(after, first, last) for first, last in gaps(len(after), ranges)]
    except ValueError:
        return origins.invalidated(state, doc_id, revision(after), "Original byte references overlap")
    following = state | {"revision": revision(after), "proof": proof | {
        "pieces": pieces, "generated": generated,
    }}
    if reconstruct(following, doc_id, after) != original:
        return origins.invalidated(state, doc_id, revision(after), "Composed original bytes did not verify")
    return following


@dataclass
class Transition:
    trace: Trace
    publication: dict | None
    allow_seed: bool

    def __call__(self, db, doc_id, before, after):
        if before != self.trace.before or after != self.trace.raw:
            raise WikiError("Material byte trace does not match the actual write", 409)
        previous = origins.preimage(origins.get(db, doc_id), doc_id, revision(before))
        job_id = self.publication["job_id"] if self.publication else None
        known = False
        if previous is None:
            previous, known = historical_preimage(db, doc_id, before)
        if previous is None and self.allow_seed and not known:
            job = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone() if job_id else None
            # An ingest job must keep its first proof; never seed an intermediate.
            if job and json.loads(job[0]).get("material"):
                return None, None
            previous = seeded(doc_id, before, {"kind": "maintenance_revision", "job_id": job_id})
        if previous is None:
            return None, None
        copies, offset = [], 0
        for piece in self.trace.pieces:
            if isinstance(piece, tuple):
                copies.append((*piece, offset))
                offset += piece[1] - piece[0]
            else:
                offset += len(piece)
        return previous, compose(previous, doc_id, before, after, copies)

#!/usr/bin/env python3
"""Compare chapter retrieval against the frozen v3 substring baseline, privately."""

import argparse
import fcntl
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.wiki.store import WikiStore
from tests.test_wiki_search import CASES, populate


def baseline(rows, query):
    terms = query.lower().split()
    expanded = set(terms)
    for chunk in re.findall(r"[\u3400-\u9fff]+", query):
        for width in (2, 3, 4):
            expanded.update(chunk[i : i + width] for i in range(len(chunk) - width + 1))
    ranked = []
    for row in rows:
        if row["kind"] == "asset":
            continue
        text = row["text"].lower()
        score = sum(
            20 * (term in row["title"].lower())
            + 8 * (term in row["path"].lower())
            + min(text.count(term), 8)
            for term in terms
        )
        score = score * 20 + sum(
            (5 if term in row["title"].lower() else 1)
            for term in expanded
            if term in text
        )
        if score:
            ranked.append((score, row["path"]))
    return [path for _, path in sorted(ranked, key=lambda item: (-item[0], item[1]))]


def baseline_search(store, query):
    """Same operation boundary as search: refresh, rank, page, current metadata."""
    store.refresh()
    with store.connect() as db:
        rows = db.execute("SELECT * FROM documents").fetchall()
    paths = baseline(rows, query)
    by_path = {row["path"]: row for row in rows}
    return {"items": [store.public(by_path[path]) for path in paths[:3]], "paths": paths}


def measure(store, operation):
    reads = []
    original = Path.read_bytes

    def watch(path):
        if path.is_relative_to(store.root):
            reads.append(path.relative_to(store.root).as_posix())
        return original(path)

    Path.read_bytes = watch
    try:
        began = time.perf_counter()
        result = operation()
        elapsed = (time.perf_counter() - began) * 1000
    finally:
        Path.read_bytes = original
    return result, elapsed, reads


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, help="Read a real vault; derived state stays temporary")
    parser.add_argument("--private-output", type=Path, help="Save real paths/metrics privately with mode 0600")
    parser.add_argument("--cases", type=Path, help="Private JSON list of [query, expected path or null]; diagnostic expectations stay outside Git")
    parser.add_argument("--metadata-state", type=Path, help="Read-only live state used to copy durable identities and managed aliases into temporary evaluation state")
    options = parser.parse_args()
    if options.vault and not options.private_output:
        parser.error("Real-vault evaluation requires --private-output")
    with tempfile.TemporaryDirectory(prefix="wiki-search-evaluation-") as temporary:
        folder = Path(temporary)
        root = options.vault.resolve() if options.vault else folder / "vault"
        if not options.vault:
            root.mkdir()
            populate(root)
        store = WikiStore(root, folder / "state", str(uuid.uuid4()))
        metadata_counts = {}
        if options.metadata_state:
            with (options.metadata_state / "vault.lock").open("rb") as lock:
                fcntl.flock(lock, fcntl.LOCK_SH)
                source = sqlite3.connect((options.metadata_state / "index.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    if source.execute("SELECT 1 FROM changes WHERE status='prepared' LIMIT 1").fetchone() or (options.metadata_state / "external-change.json").exists():
                        raise RuntimeError("Evaluation requires a quiescent journal; no live recovery was attempted")
                    with store.connect() as destination:
                        for table, columns in (("document_identities", "id,path,file_key"), ("managed_notes", "path,base_hash,rendered_revision,regions,metadata")):
                            records = source.execute(f"SELECT {columns} FROM {table}").fetchall()
                            destination.executemany(f"INSERT OR REPLACE INTO {table} VALUES({','.join('?' for _ in columns.split(','))})", records)
                            metadata_counts[table] = len(records)
                finally:
                    source.close()
        began = time.perf_counter()
        store.refresh()
        build_ms = (time.perf_counter() - began) * 1000
        target = "Library/Research/vidu_realtime_report/技术报告.md"
        cases = [("数字人研究有哪些结论", target), ("数字人", target), ("Avatar", target), ("实时 Avatar", target), ("Vidu", target), ("那篇 Vidu 论文", target), ("vidu实时报告", target), ("Vidu 技术报告", target), (target, target)] if options.vault else [*CASES, ("超弦拓扑", None)]
        if options.cases:
            cases = json.loads(options.cases.read_text())
            if not isinstance(cases, list) or any(not isinstance(case, list) or len(case) != 2 or not isinstance(case[0], str) or (case[1] is not None and not isinstance(case[1], str)) for case in cases):
                parser.error("--cases must contain [query, expected path or null] pairs")
        results = []
        for query, expected in cases:
            old_times, new_times = [], []
            for iteration in range(5):
                operations = [lambda query=query: baseline_search(store, query), lambda query=query: store.search(query, limit=3)]
                measured = {}
                for index in ((0, 1) if iteration % 2 == 0 else (1, 0)):
                    measured[index] = measure(store, operations[index])
                previous, old_ms, old_reads = measured[0]
                found, new_ms, new_reads = measured[1]
                old_times.append(old_ms)
                new_times.append(new_ms)
            old = previous["paths"]
            paths = [item["path"] for item in found["items"]]
            evidence = []
            for item in found["items"]:
                if item["path"] != expected:
                    continue
                for part in item["sections"]:
                    read, read_ms, read_files = measure(store, lambda item=item, part=part: store.read(item["id"], start=part["start"], length=part["end"] - part["start"], expected_revision=item["revision"]))
                    evidence.append({"section": part, "revision": read["revision"], "contains_query": query.casefold() in read["content"].casefold(), "characters": len(read["content"]), "read_ms": round(read_ms, 3), "file_reads": read_files})
            results.append(
                {
                    "query": query,
                    "expected": expected,
                    "baseline_top3": old[:3],
                    "chapter_top3": paths,
                    "baseline_rank": old.index(expected) + 1
                    if expected in old
                    else None,
                    "chapter_rank": paths.index(expected) + 1
                    if expected in paths
                    else None,
                    "baseline_ms": round(median(old_times), 3),
                    "chapter_ms": round(median(new_times), 3),
                    "baseline_file_reads": old_reads,
                    "chapter_file_reads": new_reads,
                    "evidence": evidence,
                    "read_characters": next(
                        (
                            item["section"]["end"] - item["section"]["start"]
                            for item in found["items"]
                            if item["path"] == expected
                        ),
                        0,
                    ),
                    "match_mode": found["match_mode"],
                }
            )
        personal = []
        if options.vault:
            offset = 0
            while True:
                page, page_ms, page_reads = measure(store, lambda offset=offset: store.documents(directory="Library/Me", limit=1, offset=offset))
                entries = []
                for item in page["items"]:
                    start = 0
                    while start < item["characters"]:
                        read, read_ms, read_files = measure(store, lambda item=item, start=start: store.read(item["id"], start=start, length=24000, expected_revision=item["revision"]))
                        entries.append({"path": item["path"], "revision": read["revision"], "start": start, "characters": len(read["content"]), "read_ms": round(read_ms, 3), "file_reads": read_files})
                        start += len(read["content"])
                        if not read["content"]:
                            raise RuntimeError("Personal evidence read did not advance")
                personal.append({"offset": offset, "total": page["total"], "next_offset": page["next_offset"], "list_ms": round(page_ms, 3), "list_file_reads": page_reads, "reads": entries})
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
        with store.connect() as db:
            corpus_revisions = {row["path"]: row["revision"] for row in db.execute("SELECT path,revision FROM documents ORDER BY path")}
        report = json.dumps(
                {
                    "corpus": "private real vault; temporary derived index" if options.vault else "synthetic; 8 documents including assets and a long article",
                    "build_ms": round(build_ms, 3),
                    "copied_navigation_metadata": metadata_counts,
                    "corpus_revisions": corpus_revisions,
                    "timing_scope": "median of 5 alternating full operations: refresh, cached-row query/ranking, pagination and current metadata; evidence read measured separately",
                    "queries": results,
                    "personal_directory": personal,
                    "real_model_behavior_verified": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        if options.private_output:
            with os.fdopen(os.open(options.private_output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(report + "\n")
            print(json.dumps({"saved": str(options.private_output), "queries": len(results), "real_model_behavior_verified": False}))
        else:
            print(report)


if __name__ == "__main__":
    main()

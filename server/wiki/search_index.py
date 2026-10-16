"""Rebuildable, local lexical navigation. Evidence stays in current files."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import cast

import jieba
from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML

from .extract import HTML_BODY_VERSION, HTMLBodyAnalyzer, html_has_readable_body
from .formats import MARKDOWN_SQL

VERSION = "sections-v8"
_tokenizer = jieba.Tokenizer()
_markdown = MarkdownIt("commonmark")
jieba.setLogLevel(logging.WARNING)
_letter = r"(?:(?![\u3400-\u9fff])[^\W_])"
_pieces = re.compile(rf"[\u3400-\u9fff]+|{_letter}+(?:[-+]{_letter}+)*", re.UNICODE)
_chinese = re.compile(r"[\u3400-\u9fff]+\Z")
_stop = set(
    "的 了 和 与 或 是 在 有 我 我的 我们 你 这个 那个 一下 如何 怎么 什么 哪些 "
    "为什么 是否 请 请问 关于 告诉 解释 介绍 可以 能 吗 呢 么 地 得 "
    "那篇 这篇 那份 这份 怎样 有何 查找 搜索 "
    "a an the of on in is are to for and or what how why my me please".split()
)


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


def words(text: str) -> list[str]:
    result: list[str] = []
    for match in _pieces.finditer(normalize(text)):
        piece = match[0]
        if not _chinese.fullmatch(piece):
            result.append(piece)
            continue
        # Unknown names are often emitted as adjacent single characters.
        # Keep those runs intact; matching their letters separately is not
        # evidence of the name. Grammar stopwords delimit lexical runs.
        pending = ""
        for word in _tokenizer.cut(piece, HMM=False):
            if len(word) == 1 and word not in _stop:
                pending += word
            else:
                if pending:
                    result.append(pending)
                    pending = ""
                result.append(word)
        if pending:
            result.append(pending)
    return result


def encoded(word: str, prefix="w") -> str:
    # One safe unicode61 token, including hyphenated terms and punctuation.
    return prefix + word.encode().hex()


def tokens(text: str) -> str:
    lexical = []
    for word in words(text):
        lexical.append(encoded(word))
        if "-" in word:
            lexical.extend(encoded(part) for part in word.split("-"))
    # Keep order and frequency. A query uses a whole contiguous bigram phrase,
    # never ORs arbitrary fragments from an unknown term.
    bigrams: list[str] = []
    for chunk in re.findall(r"[\u3400-\u9fff]+", normalize(text)):
        bigrams.extend(encoded(chunk[i : i + 2], "b") for i in range(len(chunk) - 1))
        bigrams.append("boundary")
    return " ".join([*lexical, *bigrams])


def query_groups(query: str) -> tuple[list[str], list[str]]:
    candidates = []
    for match in _pieces.finditer(normalize(query)):
        piece = match[0]
        split = words(piece)
        # A bare Chinese concept stays contiguous even when the dictionary
        # splits it (数字 + 人, 自 + 监督 + 学习). In questions, grammar
        # stopwords separate the request from its searchable content words.
        if _chinese.fullmatch(piece) and len(split) > 1 and not any(word in _stop for word in split):
            candidates.append(piece)
        else:
            candidates.extend(split)
    terms = list(dict.fromkeys(word for word in candidates if word not in _stop))
    if not terms:
        terms = list(dict.fromkeys(words(query)))
    groups = []
    for word in terms:
        group = '"' + encoded(word) + '"'
        if len(word) >= 2 and _chinese.fullmatch(word):
            phrase = " ".join(
                encoded(word[i : i + 2], "b") for i in range(len(word) - 1)
            )
            group = f'({group} OR "{phrase}")'
        elif "-" in word:
            phrase = " ".join(encoded(part) for part in word.split("-"))
            group = f'({group} OR "{phrase}")'
        groups.append(group)
    return terms, groups


def sections(text: str, title: str) -> list[dict]:
    """Character coordinates refer to extracted text, exactly as read() does."""
    from .format import markdown_line_offsets, parse

    parsed = parse(text)
    # Keep the rendered container/heading state across all Markdown tokens.
    # A fence creates a source partition, not a fresh HTML context.
    body = text[parsed.body_start:]
    line_offsets = markdown_line_offsets(body, parsed.body_start)
    env: dict = {}
    stream = _markdown.parse(body, env)
    renderer = cast(RendererHTML, _markdown.renderer)
    analyzer = HTMLBodyAnalyzer()
    evidence_ranges = []
    active_range = (parsed.body_start, len(text))
    for token_index, token in enumerate(stream):
        if token.map is not None:
            active_range = (line_offsets[token.map[0]], line_offsets[token.map[1]])
        if token.type == "inline":
            html = renderer.renderInline(token.children or [], _markdown.options, env)
        elif token.type in renderer.rules:
            html = renderer.rules[token.type](stream, token_index, _markdown.options, env)
        else:
            html = renderer.renderToken(stream, token_index, _markdown.options, env)
        before = analyzer.qualifying_events
        analyzer.feed(html)
        if analyzer.qualifying_events > before:
            evidence_ranges.append(active_range)
    before = analyzer.qualifying_events
    analyzer.close()
    if analyzer.qualifying_events > before:
        evidence_ranges.append(active_range)
    boundaries = [(part["start"], part["title"] or title, part["role"]) for part in parsed.sections]
    if not boundaries:
        boundaries = [(parsed.body_start, title, "body")]
    result = []
    for index, (start, heading, role) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(text)
        # Project actual qualifying token ranges onto the established source
        # partitions before windowing; wrappers/metadata alone cannot qualify.
        usable = any(left < end and right > start for left, right in evidence_ranges)
        while start < end:
            stop = min(start + 6000, end)
            if stop < end:
                newline = text.rfind("\n", start + 3000, stop)
                if newline >= 0:
                    stop = newline + 1
            result.append({"title": heading, "start": start, "end": stop, "section_role": role, "readable_body": usable})
            # A query is at most 1000 characters. Overlap keeps any bounded
            # term/Chinese adjacency phrase intact even in unbroken prose.
            # Coordinates remain literal slices of the current extracted text.
            start = stop if stop == end else stop - min(1000, (stop - start) // 2)
    return result or [{"title": title, "start": parsed.body_start, "end": parsed.body_start, "section_role": "body", "readable_body": False}]


def readable_body(content: str, part: dict) -> bool:
    # Fenced examples inherit their surrounding section's heading. A code
    # block inside a bookkeeping/related region cannot make it evidence.
    if part["section_role"] in {"related_notes", "curator_note"} or (
        part["section_role"] == "example"
        and normalize(part["title"]) in {"相关笔记", "整理说明"}
    ):
        return False
    # Render the complete logical region before inspecting its structure.
    # Inline and block HTML share hidden/media rules; escaped literal code
    # remains data. Neither this rendering nor the flag changes read offsets.
    return html_has_readable_body(_markdown.render(content))


def initialize(db) -> None:
    expected = {"search_documents", "search_sections", "search_fts"}
    existing = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    incomplete = not expected.issubset(existing)
    db.execute("""CREATE TABLE IF NOT EXISTS search_documents (
      doc_id TEXT PRIMARY KEY,revision TEXT NOT NULL,path TEXT NOT NULL,
      title TEXT NOT NULL,aliases TEXT NOT NULL,version TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS search_sections (
      id INTEGER PRIMARY KEY,doc_id TEXT NOT NULL,title TEXT NOT NULL,
      start INTEGER NOT NULL,end INTEGER NOT NULL,line_start INTEGER NOT NULL,
      line_end INTEGER NOT NULL,section_role TEXT NOT NULL DEFAULT 'body',
      readable_body INTEGER NOT NULL DEFAULT 0
    )""")
    db.execute(
        "CREATE INDEX IF NOT EXISTS search_sections_doc ON search_sections(doc_id)"
    )
    if "section_role" not in {column[1] for column in db.execute("PRAGMA table_info(search_sections)")}:
        db.execute("ALTER TABLE search_sections ADD COLUMN section_role TEXT NOT NULL DEFAULT 'body'")
    if "readable_body" not in {column[1] for column in db.execute("PRAGMA table_info(search_sections)")}:
        db.execute("ALTER TABLE search_sections ADD COLUMN readable_body INTEGER NOT NULL DEFAULT 0")
    db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
      title,path,aliases,tags,key_note,heading,body,tokenize='unicode61'
    )""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS search_section_delete
      AFTER DELETE ON search_sections BEGIN
      DELETE FROM search_fts WHERE rowid=old.id; END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS search_document_delete
      AFTER DELETE ON documents BEGIN
      DELETE FROM search_sections WHERE doc_id=old.id;
      DELETE FROM search_documents WHERE doc_id=old.id; END""")
    if incomplete:
        db.execute("DELETE FROM search_sections")
        db.execute("DELETE FROM search_fts")
        db.execute("DELETE FROM search_documents")


def current(db, row) -> bool:
    indexed = db.execute(
        "SELECT revision,path,version FROM search_documents WHERE doc_id=?",
        (row["id"],),
    ).fetchone()
    return bool(
        indexed and tuple(indexed) == (row["revision"], normalize(row["path"]), VERSION)
        and db.execute("SELECT 1 FROM search_sections WHERE doc_id=? LIMIT 1", (row["id"],)).fetchone()
        and not db.execute("SELECT 1 FROM search_sections s LEFT JOIN search_fts f ON f.rowid=s.id WHERE s.doc_id=? AND f.rowid IS NULL LIMIT 1", (row["id"],)).fetchone()
    )


def index(db, row, metadata) -> None:
    doc_id, text = row["id"], row["text"]
    aliases = metadata.get("aliases", [])
    tags = metadata.get("tags", [])
    managed = db.execute(
        "SELECT metadata FROM managed_notes WHERE path=?", (row["path"],)
    ).fetchone()
    if managed:
        supplied = json.loads(managed[0])
        aliases = [*aliases, *supplied.get("aliases", [])]
        tags = [*tags, *supplied.get("tags", [])]
    chunks = sections(text, row["title"])
    if row["path"].rsplit(".", 1)[-1].casefold() in {"html", "htm"} and metadata.get("html_body_version") == HTML_BODY_VERSION:
        # HTML extraction preserves its historical text coordinates. Its
        # original structure, including media-only body, was inspected once
        # by the extractor; headings cannot become plain-text body evidence.
        for part in chunks:
            part["readable_body"] = metadata["html_body_readable"]
    fields = [
        tokens(row["title"]),
        tokens(row["path"]),
        tokens(" ".join(aliases)),
        tokens(" ".join(tags) + " " + metadata.get("description", "")),
    ]
    db.execute("DELETE FROM search_sections WHERE doc_id=?", (doc_id,))
    for part in chunks:
        content = text[part["start"] : part["end"]]
        # The heading already has its own field. A title that lists several
        # concepts must not outrank chapters that actually discuss them.
        prose = re.sub(r"\A\ufeff?#{1,6}\s+[^\n]*(?:\n|$)", "", content)
        prose = re.sub(r"(?s)<!--.*?-->", "", prose)
        if part["section_role"] in {"related_notes", "curator_note"}:
            prose = ""
        cursor = db.execute(
            "INSERT INTO search_sections(doc_id,title,start,end,line_start,line_end,section_role,readable_body) VALUES(?,?,?,?,?,?,?,?)",
            (
                doc_id,
                part["title"],
                part["start"],
                part["end"],
                text.count("\n", 0, part["start"]) + 1,
                text.count("\n", 0, part["end"]) + 1,
                part["section_role"],
                int(part["readable_body"]),
            ),
        )
        db.execute(
            "INSERT INTO search_fts(rowid,title,path,aliases,tags,key_note,heading,body) VALUES(?,?,?,?,?,?,?,?)",
            (
                cursor.lastrowid,
                *fields,
                tokens(prose)
                if normalize(part["title"]) in {"key note", "摘要", "要点", "核心观点", "总结"}
                else "",
                tokens(part["title"]),
                tokens(prose),
            ),
        )
    db.execute(
        "INSERT OR REPLACE INTO search_documents VALUES(?,?,?,?,?,?)",
        (
            doc_id,
            row["revision"],
            normalize(row["path"]),
            normalize(row["title"]),
            json.dumps([normalize(alias) for alias in aliases]),
            VERSION,
        ),
    )


def select(db, query, kind, include_assets, offset, limit, directory=None, exclude_roles=False, deduplicate=False, comparison=False, diversify=False):
    terms, groups = query_groups(query)
    if not groups:
        return [], 0, terms, "lexical"
    filters, arguments = [MARKDOWN_SQL], []
    if kind:
        filters.append("d.kind=?")
        arguments.append(kind)
    elif not include_assets:
        filters.append("d.kind<>'asset'")
    if directory:
        prefix = directory.rstrip("/") + "/"
        filters.append("substr(d.path,1,?)=?")
        arguments.extend([len(prefix), prefix])
    if exclude_roles:
        filters.append("coalesce(json_extract(d.metadata,'$.doc_role'),'unknown') NOT IN ('template','source_snapshot')")
    if deduplicate:
        # A matching title/alias cannot make an unreadable representation
        # target usable. Decide from the index, without opening vault files.
        filters.append("d.quality NOT IN ('error','excluded')")
        filters.append("EXISTS(SELECT 1 FROM search_sections usable WHERE usable.doc_id=d.id AND usable.readable_body=1)")
    predicate = " AND " + " AND ".join(filters) if filters else ""
    # Metadata names break ranking ties; body coverage keeps precedence and
    # every matching body candidate remains available to the reader.
    latin_names = [group for term, group in zip(terms, groups, strict=True) if re.fullmatch(r"[a-z][a-z0-9-]+", term)]
    named_documents = []
    if latin_names:
        named_documents = [row[0] for row in db.execute("""SELECT DISTINCT s.doc_id FROM search_fts
          JOIN search_sections s ON s.id=search_fts.rowid JOIN documents d ON d.id=s.doc_id
          WHERE search_fts MATCH ?""" + predicate,
          ["{title path aliases} : (" + " OR ".join(latin_names) + ")", *arguments])]
    # Coverage belongs to a document, not a chapter: a comparison can discuss
    # the queried concepts in separate sections. Rank actual reading locations
    # only after choosing documents with complete term-group coverage.
    group_sql = (
        """WITH group_hits AS MATERIALIZED (
      SELECT s.id,s.doc_id,t.key AS term
      FROM json_each(?) t JOIN search_fts ON search_fts MATCH t.value
      JOIN search_sections s ON s.id=search_fts.rowid
      JOIN documents d ON d.id=s.doc_id WHERE 1=1"""
        + predicate
        + ") "
    )
    prefix = (
        group_sql + """, local_hits AS MATERIALIZED (
      SELECT s.id,s.doc_id,t.key AS term
      FROM json_each(?) t JOIN search_fts
        ON search_fts MATCH ('{key_note body} : (' || t.value || ')')
      JOIN search_sections s ON s.id=search_fts.rowid
      JOIN documents d ON d.id=s.doc_id WHERE 1=1"""
        + predicate
        + """
    ), coverage AS (
      SELECT doc_id,count(DISTINCT term) AS covered FROM group_hits GROUP BY doc_id
    ), hits AS MATERIALIZED (
      SELECT s.*, bm25(search_fts,18,12,16,5,4,6,1) AS cost
      FROM search_fts JOIN search_sections s ON s.id=search_fts.rowid
      JOIN documents d ON d.id=s.doc_id
      WHERE search_fts MATCH ?"""
        + predicate
        + """
    ), locations AS (
      SELECT h.*, c.covered,
        (SELECT count(*) FROM local_hits l WHERE l.id=h.id) AS local_coverage,
        (SELECT count(DISTINCT term) FROM local_hits l WHERE l.doc_id=h.doc_id) AS local_doc_coverage,
        (SELECT count(*) FROM group_hits g WHERE g.id=h.id) AS section_coverage,
        (SELECT json_group_array(term) FROM group_hits g WHERE g.id=h.id) AS term_hits,
        (SELECT json_group_array(term) FROM local_hits l WHERE l.id=h.id) AS local_term_hits
      FROM hits h JOIN coverage c ON c.doc_id=h.doc_id WHERE c.covered>=?
    ), ranked AS (
      SELECT h.*, CASE WHEN n.path=? THEN 4 WHEN n.title=? THEN 3
        WHEN EXISTS (SELECT 1 FROM json_each(n.aliases) WHERE value=?) THEN 2
        ELSE 0 END AS exact,
        CASE WHEN h.doc_id IN (SELECT value FROM json_each(?)) THEN 1 ELSE 0 END AS name_hit,
        row_number() OVER (PARTITION BY h.doc_id
          ORDER BY local_coverage DESC,section_coverage DESC,cost,start) AS position
      FROM locations h JOIN search_documents n ON n.doc_id=h.doc_id
    ) """
    )
    normalized = normalize(query)
    expression = " OR ".join(groups)
    common = [json.dumps(groups), *arguments, json.dumps(groups), *arguments, expression, *arguments]

    def parameters(required):
        return [*common, required, normalized, normalized, normalized, json.dumps(named_documents)]

    def count(required):
        return db.execute(
            group_sql + "SELECT count(*) FROM (SELECT doc_id FROM group_hits GROUP BY doc_id HAVING count(DISTINCT term)>=?)",
            [json.dumps(groups), *arguments, required],
        ).fetchone()[0]

    required = 1 if comparison else len(groups)
    total = count(required)
    mode = "comparison" if comparison else "lexical"
    if total == 0 and len(groups) > 1:
        mode = "lexical_partial"
        required = 1
        total = count(required)
    chosen = "SELECT * FROM ranked WHERE position=1"
    if deduplicate:
        # Direct, resolved identities alone define a representation group.
        # If its source survived the same lexical/scope/readability filters,
        # keep that source's own evidence ahead of a shorter summary. Missing
        # or filtered sources leave a deterministically ranked summary; do not
        # expand relationships or borrow the source's identity/coordinates.
        chosen = """SELECT * FROM (SELECT candidate.*,row_number() OVER (
            PARTITION BY representation_group
            ORDER BY (doc_id=representation_group) DESC,exact DESC,covered DESC,local_doc_coverage DESC,name_hit DESC,cost,doc_id) AS duplicate_position
            FROM (SELECT ranked.*,coalesce((SELECT target_doc FROM wiki_edges
                WHERE source_doc=ranked.doc_id AND relation='representation_of' AND target_doc IS NOT NULL
                ORDER BY ordinal LIMIT 1),ranked.doc_id) AS representation_group
                FROM ranked WHERE position=1) candidate) WHERE duplicate_position=1"""
        total = db.execute(prefix + "SELECT count(*) FROM (" + chosen + ")", parameters(required)).fetchone()[0]
    order = "exact DESC,covered DESC,local_doc_coverage DESC,name_hit DESC,cost,doc_id"
    if comparison or diversify:
        direction = "json_extract(candidate.term_hits,'$[0]')" if comparison else "coalesce(nullif((SELECT json_extract(metadata,'$.tags[0]') FROM documents WHERE id=candidate.doc_id),''),candidate.doc_id)"
        chosen = "SELECT candidate.*,row_number() OVER (PARTITION BY " + direction + " ORDER BY " + order + ") AS direction_position FROM (" + chosen + ") candidate"
        order = "direction_position," + order
    rows = [dict(row) for row in db.execute(
        prefix + chosen + " ORDER BY " + order + " LIMIT ? OFFSET ?",
        [*parameters(required), limit, offset],
    )]
    for row in rows:
        row["sections"] = [{key: row[key] for key in ("title", "start", "end", "line_start", "line_end")}]
    ids = [row["doc_id"] for row in rows if row["section_coverage"] < row["covered"] or row["local_coverage"] < row["local_doc_coverage"]]
    if ids:
        locations = db.execute(
            prefix + "SELECT * FROM ranked WHERE doc_id IN (" + ",".join("?" for _ in ids) + ") ORDER BY doc_id,position",
            [*parameters(required), *ids],
        ).fetchall()
        for row in rows:
            # Supply additional chapters only when they contribute missing
            # terms. Long notes never occupy multiple document result slots.
            covered = set(json.loads(row["term_hits"]))
            local_covered = set(json.loads(row["local_term_hits"]))
            for location in locations:
                if location["doc_id"] != row["doc_id"]:
                    continue
                contributed = set(json.loads(location["term_hits"]))
                local_contributed = set(json.loads(location["local_term_hits"]))
                if contributed - covered or local_contributed - local_covered:
                    row["sections"].append(dict(location))
                    covered.update(contributed)
                    local_covered.update(local_contributed)
    return rows, total, terms, mode


def public_section(part):
    return {key: part[key] for key in ("title", "start", "end", "line_start", "line_end")}


def preview(row, hit, terms=()):
    content = row["text"][hit["start"]:hit["end"]]
    positions = [match.start() for term in terms for match in re.finditer(re.escape(term), content, re.I)]
    start = hit["start"] + max(0, min(positions, default=0) - 100)
    return row["text"][start:min(start + 400, hit["end"])], start

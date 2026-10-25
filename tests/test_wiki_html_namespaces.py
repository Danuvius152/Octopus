"""Comment/text boundaries follow the namespace of the actual HTML element."""

import json
import uuid
from html.parser import HTMLParser

import pytest

from server.wiki import format, portable_metadata, source_bindings
from tests import test_wiki_reference_writers as writers

store = writers.store
_TEXT_ELEMENTS = ("script", "style", "textarea", "title", "xmp", "iframe", "noembed", "noframes")
_COMMENT = '<!-- octopus-wiki {"title":"Portable title"} -->'


def namespace_examples():
    """These expected comments are also checked against Chromium, offline."""
    examples = []
    for namespace in ("svg", "math"):
        for tag in _TEXT_ELEMENTS:
            examples.append((f"{namespace}-{tag}-empty", f"<{namespace}><{tag}/>@comment@</{namespace}>", True))
            examples.append((f"{namespace}-{tag}-paired", f"<{namespace}><{tag}>@comment@</{tag}></{namespace}>", True))
    points = [
        ("svg-foreignObject", "<svg><foreignObject>", "</foreignObject></svg>"),
        ("svg-desc", "<svg><desc>", "</desc></svg>"),
        ("svg-title", "<svg><title>", "</title></svg>"),
        *[("math-" + tag, f"<math><{tag}>", f"</{tag}></math>") for tag in ("mi", "mo", "mn", "ms", "mtext")],
        ("annotation-html", '<math><annotation-xml encoding="TEXT/HTML">', "</annotation-xml></math>"),
        ("annotation-xhtml", '<math><annotation-xml encoding="application/xhtml+xml">', "</annotation-xml></math>"),
    ]
    for name, start, end in points:
        for tag in _TEXT_ELEMENTS:
            examples.append((name + "-" + tag, start + f"<{tag}/>@comment@</{tag}>" + end, False))
    examples.extend([
        ("svg-html-breakout", "<svg><g><div><textarea/>@comment@</textarea></div></g></svg>", False),
        ("svg-void-breakout", "<svg><g><br/><textarea/>@comment@</textarea></g></svg>", False),
        ("math-font-breakout", '<math><mrow><font color="red"><title/>@comment@</title></font></mrow></math>', False),
        ("svg-font-stays-foreign", "<svg><g><font><title/>@comment@</title></font></g></svg>", True),
        ("svg-end-p-breakout", "<svg></p><title/>@comment@</title></svg>", False),
        ("math-end-br-breakout", "<math></br><style/>@comment@</style></math>", False),
        ("math-mglyph-stays-foreign", "<math><mtext><mglyph><title/>@comment@</mglyph></mtext></math>", True),
        ("math-malignmark-stays-foreign", "<math><mi><malignmark><script/>@comment@</malignmark></mi></math>", True),
        ("annotation-xml-stays-foreign", '<math><annotation-xml encoding="application/xml"><style/>@comment@</annotation-xml></math>', True),
        ("annotation-svg-html", "<math><annotation-xml><svg><foreignObject><title/>@comment@</title></foreignObject></svg></annotation-xml></math>", False),
        ("svg-math-keeps-svg-namespace", "<svg><math><mi><style/>@comment@</mi></math></svg>", True),
        ("math-svg-keeps-math-namespace", "<math><svg><title/><script/>@comment@</svg></math>", True),
        ("html-point-reenters-svg", "<svg><foreignObject><svg><title/>@comment@</svg></foreignObject></svg>", True),
        ("svg-exit-restores-html", "<svg><foreignObject><svg><title/></svg><style/>@comment@</style></foreignObject></svg>", False),
        ("math-point-restores-html", "<math><mtext><svg><title/></svg><textarea/>@comment@</textarea></mtext></math>", False),
        ("annotation-reenters-svg", '<math><annotation-xml encoding="text/html"><svg><title/>@comment@</svg></annotation-xml></math>', True),
        ("html-xmlns-does-not-switch", '<div xmlns="http://www.w3.org/2000/svg"><title/>@comment@</title></div>', False),
        ("svg-xmlns-does-not-switch", '<svg xmlns="http://www.w3.org/1999/xhtml"><style/>@comment@</svg>', True),
        ("selfclosed-svg-restores-html", "<svg/><title/>@comment@</title>", False),
        ("selfclosed-math-restores-html", "<math/><style/>@comment@</style>", False),
        ("empty-svg-point-keeps-svg", "<svg><foreignObject/><title/>@comment@</svg>", True),
        ("empty-math-point-keeps-math", "<math><mtext/><style/>@comment@</math>", True),
        ("html-div-slash-does-not-close", "<svg><foreignObject><div/><title/>@comment@</title></foreignObject></svg>", False),
        ("empty-annotation-keeps-math", '<math><annotation-xml encoding="text/html"/><style/>@comment@</math>', True),
    ])
    return examples


_EXAMPLES = namespace_examples()


@pytest.mark.parametrize("markup,real_comment", [(markup, real) for _, markup, real in _EXAMPLES], ids=[name for name, _, _ in _EXAMPLES])
def test_comment_projection_respects_foreign_namespaces_and_html_integration_points(markup, real_comment):
    parsed = format.parse("# Body\n\n" + markup.replace("@comment@", _COMMENT) + "\n")
    assert portable_metadata.from_document(parsed) == ({"title": "Portable title"} if real_comment else {})


@pytest.mark.parametrize("markup,real_comment", [
    ("<svg>\n\n<title/>@comment@</title>\n\n</svg>", True),
    ("<svg>\n\n*text* <title/>@comment@</title>\n\n</svg>", False),
    ("<svg>\n\n```\ncode\n```\n\n<title/>@comment@</title>\n\n</svg>", False),
    ("<math>\n\n<title/>@comment@</title>\n\n</math>", True),
    ("<svg><foreignObject>\n\n<style/>@comment@</style>\n\n</foreignObject></svg>", False),
    ("<svg>\n\n```html\n<title/>@comment@</title>\n```\n\n</svg>", False),
    ("<svg>\n\n```html\n<title/>\n```\n\n@comment@", True),
    ("<svg>\n\n```\ncode\n```\n\n<svg><title/></svg>\n\n@comment@", True),
])
def test_comment_projection_uses_rendered_markdown_namespace_boundaries(markup, real_comment):
    parsed = format.parse("# Body\n\n" + markup.replace("@comment@", _COMMENT) + "\n")
    assert portable_metadata.from_document(parsed) == ({"title": "Portable title"} if real_comment else {})


@pytest.mark.parametrize("wrapper", [
    "<svg><title/></svg>",
    "<svg><script/></svg>",
    "<math><style/></math>",
    "<svg><foreignObject><svg><title/></svg></foreignObject></svg>",
    '<math><annotation-xml encoding="text/html"><svg><script/></svg></annotation-xml></math>',
])
def test_refresh_retains_real_foreign_comment_title_alias_uid_and_current_source_head(store, monkeypatch, wrapper):
    uid = "urn:uuid:" + str(uuid.uuid4())
    source = writers.note(store, "Library/source.md", "# Source\nEvidence.\n")
    payload = {"title": "Portable title", "aliases": ["Portable alias"], "uid": uid,
               "sources": [{"doc": source["id"], "revision": source["revision"], "location": "L1-L2"}]}
    raw = "# Body\n\n" + wrapper + "\n\n<!-- octopus-wiki " + json.dumps(payload) + " -->\n"
    # Only this temporary vault uses the first candidate's self-closing
    # callback to establish its valid current data. This is not a migration.
    with monkeypatch.context() as baseline:
        baseline.setattr(format, "VERSION", "okf-parser-v8")
        baseline.setattr(format._HTMLAnchors, "handle_startendtag", HTMLParser.handle_startendtag)
        before = writers.note(store, "Library/consumer.md", raw)
        store.refresh()
        assert before["title"] == payload["title"]
        assert before["metadata"]["aliases"] == payload["aliases"]
        assert store.read(uid)["id"] == before["id"]
        with store.connect() as db:
            head = source_bindings.current(db, before["id"])
            aliases = [tuple(row) for row in db.execute("SELECT * FROM uid_aliases ORDER BY rowid")]
            observations = [tuple(row) for row in db.execute("SELECT * FROM source_binding_observations ORDER BY rowid")]
        assert len(head["entries"]) == 1
    store.refresh()
    after = store.read(uid)
    assert after["id"] == before["id"]
    assert after["title"] == payload["title"]
    assert after["metadata"]["aliases"] == payload["aliases"]
    assert after["metadata"]["format_version"] == format.VERSION
    assert store.path(before["path"]).read_bytes() == raw.encode()
    with store.connect() as db:
        assert source_bindings.current(db, after["id"]) == head
        assert [tuple(row) for row in db.execute("SELECT * FROM uid_aliases ORDER BY rowid")] == aliases
        assert [tuple(row) for row in db.execute("SELECT * FROM source_binding_observations ORDER BY rowid")] == observations
        assert source_bindings.metadata(db, after["id"], after["path"], raw.encode(), None)["sources"] == payload["sources"]

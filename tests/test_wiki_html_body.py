"""HTML body eligibility stays separate from lossless extraction coordinates."""

import pytest

from server.wiki.extract import (
    HTML_BODY_VERSION,
    HTMLBodyAnalyzer,
    extract,
    html_has_readable_body,
)


@pytest.mark.parametrize(
    "html",
    [
        "",
        " \n\t ",
        "&nbsp; &#32;",
        "<!-- meaningful text <img src='image.png'> -->",
        "<!DOCTYPE html>",
        "<?processing metadata?>",
        "<html><body><br><hr><wbr></body></html>",
        "<a href='https://example.test' title='article'></a>",
        "<img alt='An informative alternative description'>",
        "<source src='standalone.ogg'>",
    ],
)
def test_empty_attributes_and_document_metadata_are_not_body(html):
    assert html_has_readable_body(html) is False


@pytest.mark.parametrize("tag", ["h1", "h2", "h3", "h4", "h5", "h6"])
def test_headings_exclude_their_text_and_nested_media(tag):
    html = (
        f"<{tag}><span>Article title</span><img src='image.png'>"
        "<audio src='sound.ogg'><source src='alternate.ogg'></audio>"
        f"<video src='clip.mp4'></video></{tag}>"
    )
    result = extract(html.encode(), ".html")
    assert html_has_readable_body(html) is False
    assert result["metadata"]["html_body_readable"] is False
    assert result["metadata"]["html_media_refs"] == []


@pytest.mark.parametrize("tag", ["head", "title", "script", "style", "noscript"])
def test_hidden_regions_exclude_text_and_media(tag):
    html = (
        f"<{tag}><div>Hidden material <img src='hidden.png'>"
        "<video><source src='hidden.mp4'></video>"
        f"</div></{tag}>"
    )
    result = extract(html.encode(), ".html")
    assert html_has_readable_body(html) is False
    assert result["metadata"]["html_media_refs"] == []


@pytest.mark.parametrize(
    "html",
    [
        "<h1>Heading</h2>Still inside heading",
        "<noscript><div></style>Still hidden</div></noscript>",
        "<head><title>Title</title></noscript><img src='still-hidden.png'></head>",
        "<noscript><h1>Heading</h1><img src='outer-hidden.png'></noscript>",
        "<h1><title>Title</title><audio src='heading.ogg'></audio></h1>",
        "<noscript><br><img src='hidden.png'></br>Still hidden</noscript>",
        "<div><noscript>Hidden</div><img src='after.png'>",
        "<div><h1>Title</div>Still inside the unclosed heading",
        "<audio><div><h1>Heading</audio><source src='outside.ogg'>",
    ],
)
def test_unmatched_end_tags_and_nested_regions_do_not_release_exclusion(html):
    assert html_has_readable_body(html) is False


@pytest.mark.parametrize(
    "tag", ["h1", "h6", "head", "title", "script", "style", "noscript"]
)
def test_closed_regions_restore_visible_following_content(tag):
    assert html_has_readable_body(f"<{tag}>Hidden</{tag}><p>0</p>") is True
    assert html_has_readable_body(f"<{tag}/><p>Visible successor</p>") is True


@pytest.mark.parametrize(
    "html",
    [
        "Plain fragment without a body element",
        "<p>0</p>",
        "<div>π = 3.14</div>",
        "<p>!</p>",
        "<blockquote>Quoted claim</blockquote>",
        "<pre><code>return 0</code></pre>",
        "<code>&lt;script&gt;literal code&lt;/script&gt;</code>",
        "<code>&lt;img src=&quot;literal.png&quot;&gt;</code>",
        "<textarea>Notes</textarea>",
        "<div hidden>Attribute visibility is outside the structural contract</div>",
        "<div style='display:none'>CSS visibility is outside the contract</div>",
        "<NOSCRIPT/>Visible successor after an upper-case self-closing region",
    ],
)
def test_visible_body_data_includes_code_math_and_non_token_content(html):
    assert html_has_readable_body(html) is True


@pytest.mark.parametrize("tag", ["img", "audio", "video"])
@pytest.mark.parametrize("src", [None, "", " \n\t "])
def test_media_requires_an_explicit_nonempty_source(tag, src):
    attribute = "" if src is None else f" src='{src}'"
    html = f"<{tag}{attribute}></{tag}>"
    result = extract(html.encode(), ".html")
    assert html_has_readable_body(html) is False
    assert result["metadata"]["html_media_refs"] == []


@pytest.mark.parametrize("tag", ["img", "audio", "video"])
def test_media_source_alone_is_body_and_preserves_actual_reference(tag):
    html = f"<{tag.upper()} SRC='  ../media/item?a=1&amp;b=2  '/>"
    result = extract(html.encode(), ".html")
    assert html_has_readable_body(html) is True
    assert result["metadata"]["html_body_readable"] is True
    assert result["metadata"]["html_media_refs"] == [
        {"tag": tag, "src": "../media/item?a=1&b=2"}
    ]
    # Media does not introduce invented extraction text or alter text quality.
    assert result["text"] == ""
    assert result["quality"] == "needs_review"


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<audio><source src='sound.ogg'></audio>", True),
        ("<video><div><source src='clip.mp4'/></div></video>", True),
        ("<video><source src=''></video>", False),
        ("<audio><source src='   '></audio>", False),
        ("<audio></audio><source src='outside.ogg'>", False),
        ("<video/><source src='outside.mp4'>", False),
        ("<picture><source src='picture.png'></picture>", False),
        ("<h1><audio><source src='heading.ogg'></audio></h1>", False),
        ("<audio><noscript><source src='hidden.ogg'></noscript></audio>", False),
        ("<video><h2><source src='heading.mp4'></h2></video>", False),
        ("<audio><noscript/></audio><source src='outside.ogg'>", False),
        (
            "<audio><noscript>Hidden</audio></noscript><source src='outside.ogg'>",
            False,
        ),
        (
            "<video><div><h1>Title</video></h1><source src='outside.mp4'>",
            False,
        ),
    ],
)
def test_source_elements_only_count_inside_visible_audio_or_video(html, expected):
    result = extract(html.encode(), ".html")
    assert html_has_readable_body(html) is expected
    assert result["metadata"]["html_body_readable"] is expected
    assert bool(result["metadata"]["html_media_refs"]) is expected


def test_media_references_have_stable_deduplication_and_exclude_hidden_sources():
    html = (
        "<head><img src='hidden.png'></head>"
        "<img src=' first.png '/><img src='first.png'>"
        "<audio src='first.png'><source src='alternate.ogg'/></audio>"
        "<video><source src='alternate.ogg'></video>"
        "<h1><video src='heading.mp4'></video></h1>"
        "<source src='outside.mp4'>"
        "<video src='last.mp4'></video>"
    )
    result = extract(html.encode(), ".html")
    assert result["metadata"]["html_media_refs"] == [
        {"tag": "img", "src": "first.png"},
        {"tag": "audio", "src": "first.png"},
        {"tag": "source", "src": "alternate.ogg"},
        {"tag": "video", "src": "last.mp4"},
    ]


def test_malformed_wrapper_close_keeps_region_until_its_own_explicit_end():
    html = "<div><noscript>Hidden</div><img src='hidden.png'></noscript>"
    assert html_has_readable_body(html) is False
    result = extract((html + "<img src='visible.png'>").encode(), ".html")
    assert result["metadata"]["html_body_readable"] is True
    assert result["metadata"]["html_media_refs"] == [
        {"tag": "img", "src": "visible.png"}
    ]


@pytest.mark.parametrize("suffix", [".html", ".htm"])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "gb18030"])
def test_html_body_metadata_preserves_extracted_text_encoding_and_offsets(
    suffix, encoding
):
    html = (
        "<html><head><title>页面题名</title><style>p{color:red}</style></head>"
        "<body><h1>真实标题</h1><p>甲 &amp; 乙</p>"
        "<div>公式 π = 3.14<br>第二行</div>"
        '<ul><li>代码 &lt;img src="x"&gt;</li></ul>'
        "<script>hidden</script><noscript>hidden</noscript><!-- ignored -->"
        "</body></html>"
    )
    expected = (
        '页面题名\n真实标题\n\n甲 & 乙\n\n公式 π = 3.14\n第二行\n\n代码 <img src="x">'
    )
    result = extract(html.encode(encoding), suffix)
    assert result["text"] == expected
    assert result["text"].encode("utf-8") == expected.encode("utf-8")
    assert result["text"][18:29] == "公式 π = 3.14"
    assert result["text"].encode("utf-8")[38:54] == "公式 π = 3.14".encode()
    assert result["pages"] == []
    assert result["quality"] == "text"
    assert result["metadata"] == {
        "extractor": "octopus-text-v3",
        "pipeline_version": "octopus-extract-v3",
        "limitations": "Text extraction does not verify image, table or formula semantics.",
        "encoding": "utf-8-sig" if encoding == "utf-8" else encoding,
        "html_body_version": HTML_BODY_VERSION,
        "html_body_readable": True,
        "html_media_refs": [],
    }


def test_stricter_qualification_does_not_rewrite_legacy_extraction():
    # Existing HTMLText releases its counter on </style>; body qualification
    # must reject the content while preserving that prior extraction output.
    html = "<noscript><div></style>原提取仍保留</div></noscript>"
    result = extract(html.encode(), ".html")
    assert result["text"] == "原提取仍保留"
    assert result["quality"] == "text"
    assert result["metadata"]["html_body_readable"] is False
    assert result["metadata"]["html_media_refs"] == []


@pytest.mark.parametrize("suffix", [".md", ".markdown", ".txt", ".pdf"])
def test_non_html_extraction_does_not_receive_html_structure_metadata(suffix):
    source = "# Heading\n\nLiteral <img src='asset.png'> remains source text\n"
    result = extract(source.encode(), suffix)
    assert not any(key.startswith("html_") for key in result["metadata"])
    if suffix == ".pdf":
        assert result["text"] == ""
        assert result["quality"] == "excluded"
    else:
        assert result["text"] == source
        assert result["quality"] == "text"


@pytest.mark.parametrize("heading", ["相关笔记", "整理说明"])
@pytest.mark.parametrize("suffix", [".html", ".htm"])
def test_related_and_curator_html_sections_do_not_qualify_body(heading, suffix):
    html = f"<h2>{heading}</h2><p>Realtime bookkeeping only.</p>"
    result = extract(html.encode(), suffix)
    assert html_has_readable_body(html) is False
    assert result["metadata"]["html_body_readable"] is False
    # Compatibility extraction still returns its previous text and coordinates.
    assert result["text"] == heading + "\n\nRealtime bookkeeping only."
    assert result["quality"] == "text"


@pytest.mark.parametrize("heading", ["相关笔记", "整理说明"])
def test_ordinary_child_headings_code_and_media_inherit_excluded_role(heading):
    html = (
        f"<h2>{heading}</h2><h3>Details</h3><p>Bookkeeping.</p>"
        "<h4>Examples</h4><pre><code>return 0</code></pre>"
        "<blockquote>Curator quotation.</blockquote>"
        "<img src='excluded.png'><audio src='excluded.ogg'>"
        "<source src='excluded-alt.ogg'></audio>"
        "<video><source src='excluded.mp4'></video>"
    )
    parser = HTMLBodyAnalyzer()
    parser.feed(html)
    parser.close()
    assert parser.readable is False
    assert parser.qualifying_events == 0
    assert parser.media_refs == []


@pytest.mark.parametrize("heading", ["相关笔记", "整理说明"])
@pytest.mark.parametrize(
    "successor",
    [
        "<h3>正文</h3>",
        "<h4>Key note</h4>",
        "<h3>KEYNOTE</h3>",
        "<h2>Discussion</h2>",
        "<h1>New article</h1>",
    ],
)
def test_explicit_body_key_note_and_peer_headings_restore_body(heading, successor):
    html = (
        f"<h2>{heading}</h2><p>Excluded entry.</p><h3>Subsection</h3>"
        "<img src='excluded.png'>"
        + successor
        + "<pre><code>return 0</code></pre><img src='visible.png'>"
    )
    parser = HTMLBodyAnalyzer()
    parser.feed(html)
    parser.close()
    assert parser.readable is True
    assert parser.qualifying_events == 2
    assert parser.media_refs == [{"tag": "img", "src": "visible.png"}]


@pytest.mark.parametrize("tag", ["head", "title", "script", "style", "noscript"])
@pytest.mark.parametrize("heading", ["正文", "Key note", "New peer"])
def test_hidden_headings_cannot_reset_the_outer_excluded_role(tag, heading):
    html = (
        "<h2>相关笔记</h2>"
        f"<{tag}><h1>{heading}</h1></{tag}>"
        "<p>Still related.</p><img src='still-related.png'>"
    )
    result = extract(html.encode(), ".html")
    assert html_has_readable_body(html) is False
    assert result["metadata"]["html_media_refs"] == []


@pytest.mark.parametrize("tag", ["head", "title", "script", "style", "noscript"])
def test_hidden_excluded_headings_do_not_disable_following_visible_body(tag):
    html = f"<{tag}><h2>整理说明</h2></{tag}><p>Actual body.</p>"
    assert html_has_readable_body(html) is True


@pytest.mark.parametrize(
    "heading",
    [
        "<h2><span>相关</span><em>笔记</em></h2>",
        "<h2>整理<!-- ignored -->说明</h2>",
        "<h2>整理<noscript>Invisible addition.</noscript>说明</h2>",
        "<h2>整理<script>Invisible addition.</script>说明</h2>",
        "<h2>相关&#31508;记</h2>",
    ],
)
def test_only_actual_visible_heading_labels_select_excluded_roles(heading):
    assert html_has_readable_body(heading + "<p>Excluded entry.</p>") is False


@pytest.mark.parametrize(
    "html",
    [
        "<p>相关笔记</p><p>正文内容。</p>",
        "<span>整理说明</span><img src='visible.png'>",
        "<pre><code>&lt;h2&gt;整理说明&lt;/h2&gt;</code></pre><p>Body.</p>",
        "<h2>Related note</h2><p>Ordinary heading.</p>",
    ],
)
def test_ordinary_data_and_literal_heading_code_do_not_select_roles(html):
    assert html_has_readable_body(html) is True


def test_escaped_heading_code_cannot_reset_an_excluded_role():
    html = (
        "<h2>整理说明</h2>"
        "<pre><code>&lt;h2&gt;正文&lt;/h2&gt;</code></pre>"
        "<p>Still excluded.</p><img src='excluded.png'>"
    )
    assert html_has_readable_body(html) is False


def test_analyzer_retains_hidden_state_across_separately_rendered_code_tokens():
    parser = HTMLBodyAnalyzer()
    for html in (
        "<noscript>\n",
        "<pre><code class='language-text'>realtime secret\n</code></pre>\n",
        "</noscript>\n",
    ):
        parser.feed(html)
        assert parser.qualifying_events == 0
    assert parser.readable is False
    parser.feed("<p>Visible after the hidden container.</p>\n")
    parser.close()
    assert parser.readable is True
    assert parser.qualifying_events == 1


def test_analyzer_retains_heading_and_role_state_across_token_feeds():
    parser = HTMLBodyAnalyzer()
    for html in (
        "<h2>",
        "整理<span>说明</span>",
        "</h2>\n",
        "<pre><code>Excluded code.</code></pre>\n",
        "<h3>",
        "Ordinary child",
        "</h3>\n",
        "<img src='excluded.png'>",
        "<h3>",
        "Key<br>note",
        "</h3>\n",
    ):
        parser.feed(html)
        assert parser.qualifying_events == 0
    parser.feed("<p>Visible claim.</p>")
    parser.feed("<video><source src='visible.mp4'></video>")
    parser.close()
    assert parser.readable is True
    assert parser.qualifying_events == 2
    assert parser.media_refs == [{"tag": "source", "src": "visible.mp4"}]


def test_event_count_qualifies_later_data_and_duplicate_media_ranges():
    parser = HTMLBodyAnalyzer()
    fragments = (
        ("<p>First claim.</p>", 1),
        ("<p>Second claim.</p>", 2),
        ("<img src='same.png'>", 3),
        ("<img src='same.png'>", 4),
        ("<h2>相关笔记</h2><p>Excluded.</p><img src='same.png'>", 4),
    )
    for html, expected in fragments:
        parser.feed(html)
        assert parser.qualifying_events == expected
    parser.close()
    assert parser.readable is True
    assert parser.media_refs == [{"tag": "img", "src": "same.png"}]


def test_close_accounts_for_readable_incomplete_tail_without_releasing_hidden_state():
    visible = HTMLBodyAnalyzer()
    visible.feed("<p>π = 3.14 &unfinished")
    before = visible.qualifying_events
    visible.close()
    assert visible.readable is True
    assert visible.qualifying_events > before

    hidden = HTMLBodyAnalyzer()
    hidden.feed("<noscript><pre><code>Hidden &unfinished")
    hidden.close()
    assert hidden.readable is False
    assert hidden.qualifying_events == 0


@pytest.mark.parametrize("container", ["blockquote", "ul", "ol", "li"])
def test_nested_heading_does_not_change_outer_body_or_editorial_role(container):
    assert html_has_readable_body(
        f"<{container}><h2>整理说明</h2>Quoted note.</{container}><p>Actual body.</p>"
    )
    assert not html_has_readable_body(
        f"<h2>相关笔记</h2><{container}><h2>正文</h2>Linked note.</{container}><p>Bookkeeping.</p>"
    )

"""Reference syntax must not choose a different file after a second decode."""

import json

import pytest

from server.wiki.references import (
    ParsedReference,
    candidate_keys,
    literal_reference,
    local_references,
    source_reference,
    uri_reference,
)


@pytest.mark.parametrize("token,path,fragment", [
    ("a%23b.md#%2523", "a#b.md", "%23"),
    ("a%2523b.md#%23", "a%23b.md", "#"),
    ("a%25b.md#%253F", "a%b.md", "%3F"),
    ("a%3Fb.md#%3F", "a?b.md", "?"),
    ("%E4%B8%AD%E6%96%87%20%F0%9F%98%80.md", "中文 😀.md", ""),
])
def test_uri_is_decoded_exactly_once_before_any_lookup(token, path, fragment):
    reference = uri_reference(token, "wikilink", wiki=True)
    assert reference.raw_resource == token
    assert reference.value == path and reference.fragment == fragment
    assert ("path", "Library/" + path) in candidate_keys(reference, "Library")
    assert ParsedReference.restore(json.loads(json.dumps(reference.record()))) == reference


@pytest.mark.parametrize("path", ["a#b.md", "a%23b.md", "a?b.md", "中文 😀.md"])
def test_literal_api_path_does_not_pass_through_uri_syntax(path):
    reference = source_reference({"path": path, "location": "L2-L3"})
    assert reference.value == path and reference.fragment == "L2-L3"
    assert reference.input_type == "literal_path" and reference.base_policy == "vault"
    assert candidate_keys(reference, "Library/nested")[0] == ("path", path)
    assert all("Library/nested" not in value for _, value in candidate_keys(reference, "Library/nested"))


def test_equivalent_uri_spelling_and_fragment_do_not_make_new_source_selections():
    references = [uri_reference(token, "derived_from", wiki=True)
                  for token in ["%61.md#before", "a.md#after", "./a.md#%2523"]]
    assert len({item.selection for item in references}) == 1
    assert [item.raw_resource for item in references] == ["%61.md#before", "a.md#after", "./a.md#%2523"]
    assert len({item.fragment for item in references}) == 3
    assert literal_reference("%61.md", "derived_from").selection != references[0].selection


def test_exact_source_identity_is_retained_with_stale_or_punctuation_display_paths():
    identifier = "a" * 32
    for path in ("Library/old.md", "a#b.md", "a%23b.md", "a?b.md"):
        reference = source_reference({"resource": path, "doc": identifier, "path": path})
        assert reference.namespace == "doc" and reference.value == identifier
        assert candidate_keys(reference, "Library") == (("doc", identifier),)
        assert reference.raw_resource == path


def test_multiple_explicit_identities_are_retained_for_agreement_check_without_path_fallback():
    identifier = "a" * 32
    uid = "urn:uuid:00000000-0000-0000-0000-000000000123"
    reference = source_reference({"doc": identifier, "resource": uid, "path": "replacement.md"})
    assert reference.identities == (("doc", identifier), ("uid", uid))
    assert candidate_keys(reference, "Library") == reference.identities
    same = source_reference({"doc": identifier, "resource": identifier})
    assert same.identities == (("doc", identifier),)
    unknown = source_reference({"doc": "not-an-identity", "path": "replacement.md"})
    assert unknown.namespace == "unknown" and candidate_keys(unknown, "Library") == ()


@pytest.mark.parametrize("resource", ["a#b.md", "a%23b.md", "a?b.md", "./a%2523b.md"])
def test_legacy_punctuation_without_type_or_proof_cannot_guess_from_current_files(resource):
    reference = source_reference({"resource": resource})
    assert reference.namespace == "unknown"
    assert reference.diagnostic == "untyped_resource_syntax"
    assert reference.raw_resource == resource and candidate_keys(reference, "Library") == ()


def test_plain_legacy_path_still_has_indexed_candidates_and_conflicting_paths_do_not():
    plain = source_reference({"resource": "a.md"})
    assert plain.namespace == "path"
    assert candidate_keys(plain, "Library")[:4] == (
        ("path", "Library/a.md"), ("path", "Library/a.md.md"),
        ("path", "Library/a.md.markdown"), ("path", "a.md"),
    )
    assert candidate_keys(plain, "Library")[-1] == ("name", "a")
    literal = source_reference({"resource": "a#b.md", "path": "a#b.md"})
    assert literal.input_type == "literal_path" and literal.value == "a#b.md"
    conflict = source_reference({"resource": "old.md", "path": "new.md"})
    assert conflict.namespace == "unknown" and conflict.diagnostic == "conflicting_source_paths"


def test_external_resource_and_display_path_do_not_claim_local_identity():
    reference = source_reference({"resource": "https://example.com/a.md", "path": "Library/a.md"})
    assert reference.namespace == "external"
    assert candidate_keys(reference, "Library") == ()
    reference = source_reference({"resource": "https://example.com/a.md", "doc": "a" * 32})
    assert candidate_keys(reference, "Library") == (("doc", "a" * 32),)


def test_lookup_keys_preserve_case_and_missing_higher_priority_candidates():
    reference = uri_reference("Name", "wikilink", wiki=True)
    assert candidate_keys(reference, "Library/nested") == (
        ("path", "Library/nested/Name"), ("path", "Library/nested/Name.md"),
        ("path", "Library/nested/Name.markdown"), ("path", "Name"),
        ("path", "Name.md"), ("path", "Name.markdown"), ("name", "Name"),
    )
    markdown = uri_reference("Name", "markdown_link")
    assert candidate_keys(markdown, "Library") == (
        ("path", "Library/Name"), ("path", "Library/Name.md"), ("path", "Library/Name.markdown"),
    )
    anchor = uri_reference("#%2523", "markdown_link")
    assert anchor.namespace == "self" and anchor.fragment == "%23"
    assert candidate_keys(anchor, "Library", "a" * 32) == (("doc", "a" * 32),)


@pytest.mark.parametrize("path", ["../outside.md", "/outside.md", "a/../outside.md", "a\\b.md", "a\x00.md"])
def test_invalid_literal_paths_do_not_produce_candidates(path):
    reference = literal_reference(path, "derived_from")
    assert reference.namespace == "invalid" and candidate_keys(reference, "Library") == ()


def test_actual_markdown_and_wikilink_tokens_keep_single_decode_and_code_boundaries():
    text = ("---\ntitle: '[[metadata-only]]'\n---\n"
            "# Source\n[a](a%2523b.md#%2523) [[a%23b.md#%23|display]] "
            "![image](a%253Fb.png) [outside](https://example.com/a.md)\n\n"
            "`[[inline-code]]`\n\n```md\n[[fenced-code]]\n```\n")
    references = list(local_references(text))
    assert [(item.raw_resource, item.value, item.fragment, item.relation) for item in references] == [
        ("a%2523b.md#%2523", "a%23b.md", "%23", "markdown_link"),
        ("a%23b.md#%23", "a#b.md", "#", "wikilink"),
        ("a%253Fb.png", "a%3Fb.png", "", "markdown_link"),
    ]

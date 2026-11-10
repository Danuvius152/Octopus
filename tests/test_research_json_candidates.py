"""Research output selection through the parsers and injected leaf boundary."""

import json
import sys

import pytest

from server.research.evidence import EvidenceBounds
from server.research.leaf import LeafResult
from server.research.orchestrator import ResearchLimits, run_research
from server.research.schemas import parse_angles, parse_search, parse_vote


@pytest.fixture(params=["integer_digits", "recursion_refusal"])
def decoder_limit_noise(request, monkeypatch):
    if request.param == "recursion_refusal":
        depth = sys.getrecursionlimit() + 100
        noise = "[" * depth + "0" + "]" * depth
        original_loads = json.loads

        def decode(candidate, *args, **kwargs):
            # CPython 3.14 can decode this iteratively. Inject the decoder's
            # recursion refusal without changing the process recursion limit.
            if candidate == noise:
                raise RecursionError("Controlled JSON decoder nesting refusal")
            return original_loads(candidate, *args, **kwargs)

        monkeypatch.setattr(json, "loads", decode)
        return noise
    return "[" + "9" * 5000 + "]"


@pytest.mark.parametrize("position", ["before", "after"])
def test_decoder_limits_in_noise_preserve_valid_fence_selection(decoder_limit_noise, position):
    def reply(value):
        fenced = "```json\n" + value + "\n```"
        return decoder_limit_noise + " " + fenced if position == "before" else fenced + " " + decoder_limit_noise

    assert parse_angles(reply('["reliability"]'), question="q", max_angles=5) == ["reliability"]
    parsed = parse_search(reply('[{"claim": "Reliable", "url": "https://source.test"}]'),
                          angle="original", max_findings=5)
    assert parsed is not None
    assert [(f.claim, f.url) for f in parsed[0]] == [("Reliable", "https://source.test")]
    bounds = EvidenceBounds()
    vote = parse_vote(reply('{"refuted": true, "note": "Counterexample", '
                            '"sources": ["https://counterexample.test"]}'), None, bounds)
    assert vote.status == "refute" and vote.note == "Counterexample"
    assert vote.sources == ["https://counterexample.test"] and not bounds.omitted


def test_decoder_limits_in_naked_output_keep_domain_failure_and_fallback(decoder_limit_noise):
    assert parse_angles(decoder_limit_noise, question="Original question", max_angles=5) == ["Original question"]
    assert parse_search(decoder_limit_noise, angle="original", max_findings=5) is None
    vote = parse_vote(decoder_limit_noise, None, EvidenceBounds())
    assert vote.status == "failed" and vote.note == "Invalid verification output"


@pytest.mark.parametrize("noisy_phase", ["scope", "search", "vote"])
async def test_decoder_limits_in_leaf_noise_do_not_lose_pipeline_counterevidence(tmp_path, decoder_limit_noise, noisy_phase):
    synthesis_inputs = []

    def reply(value, phase):
        fenced = "```json\n" + value + "\n```"
        return fenced + " " + decoder_limit_noise if phase == noisy_phase else fenced

    async def reason(prompt):
        if "Decompose" in prompt:
            return LeafResult(text=reply('["reliability"]', "scope"))
        synthesis_inputs.append(prompt)
        return LeafResult(text="Report retains the counterexample.")

    async def search(prompt):
        if "Fact-check" in prompt:
            return LeafResult(text=reply('{"refuted": true, "verified": true, '
                '"note": "Counterexample found", "conditions": "Under load", '
                '"sources": ["https://counterexample.test/proof"]}', "vote"))
        return LeafResult(text=reply('[{"claim": "Reliable", "url": "https://source.test"}]', "search"))

    result = await run_research("Assess reliability", working_dir=str(tmp_path), reason=reason, search=search,
                                limits=ResearchLimits(votes_per_claim=1))
    assert result.angles == ["reliability"] and result.evidence.searches[0].status == "findings"
    assert len(result.evidence.claims) == 1 and result.evidence.claims[0].status == "refuted"
    assert result.evidence.claims[0].votes[0].sources == ["https://counterexample.test/proof"]
    assert result.findings == [] and result.sources == ["https://counterexample.test/proof"]
    for text in [synthesis_inputs[0], result.report]:
        assert "Counterexample found" in text and "Under load" in text
        assert "https://counterexample.test/proof" in text


def test_prose_verdict_preserves_counterevidence_and_nested_sources():
    vote = parse_vote(
        'The check found a limit. {"refuted": true, "verified": true, '
        '"note": "A counterexample", "conditions": "Only under load", '
        '"sources": ["https://counterexample.test/proof"]} End of check.',
        None,
        EvidenceBounds(),
    )
    assert vote.status == "refute"
    assert vote.note == "A counterexample"
    assert vote.conditions == "Only under load"
    assert vote.sources == ["https://counterexample.test/proof"]


@pytest.mark.parametrize("text", [
    'Output: [{"refuted": false, "verified": true}]',
    'Output: {"wrapper": {"refuted": false, "verified": true}}',
    'Output: {"wrapper": broken, "child": {"refuted": false, "verified": true}}',
    'Output: {"wrapper": {"refuted": false, "verified": true}',
    'Output: [```json\n{"refuted": false, "verified": true}\n```]',
])
def test_nested_verdict_cannot_escape_a_wrong_or_malformed_outer_value(text):
    vote = parse_vote(text, None, EvidenceBounds())
    assert vote.status == "failed"


@pytest.mark.parametrize("text,expected", [
    ('[" alpha ", "", {"query": "beta"}, 7]', ["alpha", "beta"]),
    ('```json\n["alpha"]\n```', ["alpha"]),
    ('```\n["alpha"]\n```', ["alpha"]),
    ('Angles: ["alpha", {"q": "beta"}] End.', ["alpha", "beta"]),
    ('Bad: [7] Good: ["alpha"]', ["alpha"]),
    ('["noise"] ```json\n["alpha"]\n``` ["other"]', ["alpha"]),
    ('```json\n[7]\n``` ```json\n["alpha"]\n```', ["alpha"]),
    ('["alpha"] ["beta"]', ["Original question"]),
    ('["alpha"] ["alpha"]', ["Original question"]),
    ('```json\n["alpha"]\n``` ```\n["alpha"]\n```', ["Original question"]),
    ('```json\n["alpha"] ["beta"]\n```', ["Original question"]),
    ('{"nested": ["alpha"]}', ["Original question"]),
    ('Angles: {"nested": ["alpha"]}', ["Original question"]),
    ('Angles: {"nested": ["alpha"]', ["Original question"]),
    ('Angles: {"nested": ["alpha"], broken}', ["Original question"]),
    ('[[]]', ["Original question"]),
    ('[]', ["Original question"]),
    ('[7, null, false, {"angle": 3}]', ["Original question"]),
    ('No usable output', ["Original question"]),
])
def test_angles_select_one_usable_outer_array_or_keep_the_original_question(text, expected):
    assert parse_angles(text, question="Original question", max_angles=5) == expected


@pytest.mark.parametrize("text,expected", [
    ('[]', []),
    ('```json\n[]\n```', []),
    ('No material: [] End.', []),
    ('[false] []', []),
    ('["bad"] [{"claim": "Good", "source": "https://source.test"}]', ["Good"]),
    ('Found: [{"claim": "Good", "url": "https://source.test", "extra": [1, 2]}]', ["Good"]),
    ('[{"claim": "Noise"}] ```json\n[{"claim": "Good"}]\n```', ["Good"]),
    ('[{"claim": "A"}] [{"claim": "B"}]', None),
    ('[] [{"claim": "A"}]', None),
    ('[] []', None),
    ('{"findings": [{"claim": "Nested"}]}', None),
    ('Found: {"findings": [{"claim": "Nested"}]}', None),
    ('[{"wrapper": [{"claim": "Nested"}]}]', None),
    ('[{"claim": ""}, {"claim": 1}, null]', None),
    ('[{"claim": "unfinished"}', None),
    ('Not JSON', None),
])
def test_search_distinguishes_empty_success_from_invalid_or_ambiguous_output(text, expected):
    parsed = parse_search(text, angle="Original angle", max_findings=5)
    if expected is None:
        assert parsed is None
    else:
        assert parsed is not None
        findings, omitted = parsed
        assert [finding.claim for finding in findings] == expected
        assert all(finding.angle == "Original angle" for finding in findings)
        assert not omitted


def test_selected_array_keeps_existing_aliases_and_limits():
    assert parse_angles(
        'Angles: [{"angle":"a"}, {"query":"b"}, {"q":"c"}]',
        question="q", max_angles=2,
    ) == ["a", "b"]
    parsed = parse_search(
        'Found: [null, {"claim":" A ","source":" https://source.test "}, '
        '{"claim":"B","url":7}]', angle="original", max_findings=1,
    )
    assert parsed is not None
    findings, omitted = parsed
    assert [(f.claim, f.url, f.angle) for f in findings] == [("A", "https://source.test", "original")]
    assert omitted


@pytest.mark.parametrize("text,expected", [
    ('{"refuted": false, "verified": true}', "support"),
    ('```json\n{"refuted": true}\n```', "refute"),
    ('```\n{"refuted": false}\n```', "unverified"),
    ('Invalid: {"refuted": 1} Actual: {"refuted": true}', "refute"),
    ('[] Actual: {"refuted": false}', "unverified"),
    ('{"refuted": true} {"refuted": false}', "failed"),
    ('{"refuted": false, "verified": true} {"refuted": false, "verified": true}', "failed"),
    ('```json\n{"refuted": true}\n``` ```json\n{"refuted": false}\n```', "failed"),
    ('{"refuted": false, "verified": true} ```json\n{"refuted": true}\n```', "refute"),
    ('```json\n{"refuted": 1}\n``` ```json\n{"refuted": true}\n```', "refute"),
    ('```json\n{"wrapper": {"refuted": false, "verified": true}}\n```', "failed"),
    ('Not JSON', "failed"),
])
def test_vote_uses_expected_shape_schema_and_priority_without_guessing(text, expected):
    assert parse_vote(text, None, EvidenceBounds()).status == expected


def test_scanner_respects_quoted_delimiters_and_escaped_quotes_and_backslashes():
    note = 'Literal ] } [ {, escaped "quote", backslash \\ and Unicode 验证'
    verdict = json.dumps({"refuted": True, "note": note,
                          "sources": ["https://source.test/[section]"]}, ensure_ascii=False)
    quoted_noise = json.dumps('do not parse [{"refuted": false, "verified": true}]')
    vote = parse_vote(f'Quoted explanation: {quoted_noise}. Verdict: {verdict} End.', None, EvidenceBounds())
    assert vote.status == "refute" and vote.note == note
    assert vote.sources == ["https://source.test/[section]"]
    assert parse_vote(json.dumps(verdict), None, EvidenceBounds()).status == "failed"


@pytest.mark.parametrize("selection", ["invalid_schema", "ambiguous", "fence_priority", "leaf_error"])
def test_discarded_verdicts_do_not_mutate_evidence_bounds(selection):
    large = {"refuted": True, "verified": True, "note": "long" * 100,
             "conditions": "condition" * 100,
             "sources": ["https://discarded.test/" + "path" * 200] * 4}
    small = '{"refuted": false, "verified": true, "note": "Accepted"}'
    if selection == "invalid_schema":
        large["refuted"] = 1
        text, error, status = json.dumps(large) + " " + small, None, "support"
    elif selection == "ambiguous":
        text, error, status = json.dumps(large) + " " + small, None, "failed"
    elif selection == "fence_priority":
        text, error, status = json.dumps(large) + " ```json\n" + small + "\n```", None, "support"
    else:
        text, error, status = json.dumps(large), "Original leaf failure", "failed"
    bounds = EvidenceBounds()
    vote = parse_vote(text, error, bounds)
    assert vote.status == status
    assert not bounds.omitted
    assert vote.sources == []
    if error:
        assert vote.note == error


async def test_pipeline_retains_refutation_sources_and_failed_verification(tmp_path):
    synthesis_inputs = []
    checks = {
        "Refuted claim": LeafResult(text='Counterevidence: {"refuted": true, "verified": true, '
            '"note": "Counterexample found", "conditions": "Under load", '
            '"sources": ["https://counterexample.test/proof"]}'),
        "Ambiguous claim": LeafResult(text='{"refuted": false, "verified": true} '
            '{"refuted": false, "verified": true}'),
        "Broken claim": LeafResult(text='[{"refuted": false, "verified": true}]'),
        "Partial claim": LeafResult(text='{"refuted": false, "verified": true}', error="Original leaf refusal"),
        "Supported claim": LeafResult(text='Actual: {"refuted": false, "verified": true, '
            '"sources": ["https://support.test/proof"]}'),
    }

    async def reason(prompt):
        if "Decompose" in prompt:
            return LeafResult(text='Selected angles: ["reliability"]')
        synthesis_inputs.append(prompt)
        return LeafResult(text="Report retains gaps and counterevidence.")

    async def search(prompt):
        if "Fact-check" in prompt:
            claim = prompt.split("CLAIM: ", 1)[1].split("\nSOURCE:", 1)[0]
            return checks[claim]
        return LeafResult(text="Found: " + json.dumps([
            {"claim": claim, "url": f"https://claim.test/{index}"}
            for index, claim in enumerate(checks)
        ]))

    result = await run_research("Assess reliability", working_dir=str(tmp_path), reason=reason, search=search,
                                limits=ResearchLimits(votes_per_claim=1))
    claims = {claim.claim: claim for claim in result.evidence.claims}
    assert {claim: value.status for claim, value in claims.items()} == {
        "Refuted claim": "refuted", "Ambiguous claim": "failed", "Broken claim": "failed",
        "Partial claim": "failed", "Supported claim": "verified",
    }
    assert claims["Refuted claim"].votes[0].conditions == "Under load"
    assert claims["Refuted claim"].votes[0].sources == ["https://counterexample.test/proof"]
    assert claims["Partial claim"].votes[0].note == "Original leaf refusal"
    assert {finding.claim for finding in result.findings} == set(checks) - {"Refuted claim"}
    assert "https://counterexample.test/proof" in result.sources
    assert "https://support.test/proof" in result.sources
    for text in [synthesis_inputs[0], result.report]:
        assert "[refuted] Refuted claim" in text and "Counterexample found" in text
        assert "https://counterexample.test/proof" in text and "Under load" in text
        assert "[failed] Ambiguous claim" in text and "[failed] Broken claim" in text


@pytest.mark.parametrize("output,status", [
    ('No results: []', "empty"),
    ('[] []', "failed"),
    ('[false]', "failed"),
    ('Not JSON', "failed"),
])
async def test_pipeline_gap_report_can_complete_without_successful_search_or_verification(tmp_path, output, status):
    async def reason(prompt):
        if "Decompose" in prompt:
            return LeafResult(text='["one"] ["two"]')
        return LeafResult(text="Gap report: no usable claims.")

    async def search(prompt):
        assert "ANGLE: Original question" in prompt
        assert "Fact-check" not in prompt
        return LeafResult(text=output)

    result = await run_research("Original question", working_dir=str(tmp_path), reason=reason, search=search)
    assert result.angles == ["Original question"]
    assert result.evidence.searches[0].status == status
    assert result.findings == [] and result.evidence.claims == [] and result.sources == []
    assert "Gap report" in result.report and "Insufficient evidence" in result.report

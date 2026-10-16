"""Optional evaluation reports keep protocol failures and ungraded answers."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from tests.wiki_query_evidence import collect_evaluation


def probe_for(tmp_path, *, answer="A different wording.", failure=None, mutate=False):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    source = root / "Library/example.md"
    source.write_text("# Synthetic source\nOriginal conditions.\n")
    probe = SimpleNamespace(
        root=root, last_session_id="synthetic-session", last_request_id="synthetic-request",
        last_events=[], call_trace=[{"operation": "read", "params": {"path": "Library/example.md"},
                                    "response": {"path": "Library/example.md", "revision": "current-test-revision", "start": 0, "end": 40}}],
        usage_observations=[{"raw": {"usage": {"input_tokens": 100}}},
                            {"raw": {"usage": {"input_tokens": 150}}}], ask_count=0,
    )

    async def ask(question):
        probe.ask_count += 1
        probe.last_events = [{"type": "assistant_text", "content": answer}]
        if mutate:
            source.write_text("Changed during a read-only query.\n")
        if failure:
            raise failure
        probe.last_events.append({"type": "result", "terminal": True, "is_error": False,
                                  "session_id": probe.last_session_id, "request_id": probe.last_request_id})
        return answer

    probe.ask = ask
    return probe, source


@pytest.mark.parametrize("answer", ["A different wording.", "另一种表达方式。", "An answer still requiring semantic review."])
async def test_recorded_execution_does_not_grade_answer_wording(tmp_path, answer):
    probe, source = probe_for(tmp_path, answer=answer)
    path = await collect_evaluation(probe, "Explain this note.", case_id="case", output=tmp_path / "reports")
    report = json.loads(path.read_text())
    assert report["execution_status"] == "success" and report["semantic_acceptance"] == "not_reviewed"
    assert report["answer"] == answer and report["question"] == "Explain this note."
    assert report["source_before"] == report["source_after"] == [{
        "path": "Library/example.md", "bytes": len(source.read_bytes()),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }]
    assert (path.parent / "sources/Library/example.md").read_bytes() == source.read_bytes()
    assert report["tool_trace"] == probe.call_trace and report["normalized_events"] == probe.last_events
    assert report["usage_observations"] == probe.usage_observations
    assert report["total_input_plus_output_tokens"] is None and report["USD"] is None


@pytest.mark.parametrize("failure", [AssertionError("failed settlement"), asyncio.CancelledError()])
async def test_failed_or_cancelled_execution_preserves_partial_evidence_and_raises(tmp_path, failure):
    probe, _ = probe_for(tmp_path, answer="Partial text.", failure=failure)
    output = tmp_path / "reports"
    with pytest.raises(type(failure)):
        await collect_evaluation(probe, "Explain this note.", case_id="case", output=output)
    report = json.loads(next(output.rglob("report.json")).read_text())
    assert report["execution_status"] == "failed" and report["semantic_acceptance"] == "not_reviewed"
    assert report["failure"]["type"] == type(failure).__name__ and report["answer"] == "Partial text."


async def test_read_only_source_change_is_a_mechanical_failure(tmp_path):
    probe, _ = probe_for(tmp_path, mutate=True)
    output = tmp_path / "reports"
    with pytest.raises(AssertionError, match="changed its source"):
        await collect_evaluation(probe, "Explain this note.", case_id="case", output=output)
    report = json.loads(next(output.rglob("report.json")).read_text())
    assert report["execution_status"] == "failed" and report["source_before"] != report["source_after"]
    retained = next(output.rglob("sources/Library/example.md"))
    assert hashlib.sha256(retained.read_bytes()).hexdigest() == report["source_before"][0]["sha256"]


async def test_existing_report_is_not_overwritten_or_paid_for_again(tmp_path):
    probe, _ = probe_for(tmp_path)
    args = {"case_id": "case", "output": tmp_path / "reports"}
    path = await collect_evaluation(probe, "Explain this note.", **args)
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        await collect_evaluation(probe, "Explain this note.", **args)
    assert path.read_bytes() == original and probe.ask_count == 1

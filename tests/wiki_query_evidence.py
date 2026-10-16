"""Record optional query execution without grading natural-language answers."""

import hashlib
import json
import time
from pathlib import Path


def source_manifest(root: Path) -> list[dict]:
    result = []
    for path in sorted(root.rglob("*.md")):
        raw = path.read_bytes()
        result.append({"path": path.relative_to(root).as_posix(), "bytes": len(raw),
                       "sha256": hashlib.sha256(raw).hexdigest()})
    return result


async def collect_evaluation(probe, question: str, *, case_id: str, output: Path) -> Path:
    """A successful pytest case means execution was recorded, not semantic acceptance."""
    directory = output / hashlib.sha256(case_id.encode()).hexdigest()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = time.monotonic()
    before = source_manifest(probe.root)
    for document in before:
        target = directory / "sources" / document["path"]
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        raw = (probe.root / document["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == document["sha256"], "source changed while capturing evidence"
        target.write_bytes(raw)
        target.chmod(0o600)
    after = None
    answer = ""
    failure = None
    try:
        answer = await probe.ask(question)
        after = source_manifest(probe.root)
        assert before == after, "read-only evaluation changed its source material"
    except BaseException as error:
        failure = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        # Keep partial output and failure evidence; a failed execution is never
        # converted into a successful semantic evaluation.
        if not answer:
            answer = "\n".join(event.get("content", "") for event in probe.last_events
                               if event.get("type") == "assistant_text")
        report = {
            "schema": "wiki-knowledge-evaluation-v1", "case_id": case_id,
            "question": question, "execution_status": "failed" if failure else "success",
            "input_context": getattr(probe, "input_context", None),
            "semantic_acceptance": "not_reviewed", "failure": failure,
            "session_id": probe.last_session_id, "request_id": probe.last_request_id,
            "answer": answer, "source_before": before,
            "source_snapshot_directory": "sources",
            "source_after": after if after is not None else source_manifest(probe.root),
            "tool_trace": getattr(probe, "call_trace", []),
            "normalized_events": probe.last_events,
            "elapsed_seconds": time.monotonic() - started,
            "timing_scope": "Query execution and source checks; excludes CLI availability probe and test fixture setup/teardown",
            "usage_observations": getattr(probe, "usage_observations", []),
            "cleanup_failures": getattr(probe, "cleanup_failures", []),
            "total_input_plus_output_tokens": None, "USD": None,
            "usage_limit": "Raw counters may be cumulative or omit helpers/recovery; retain observations without summing or treating unknown usage as zero",
        }
        path = directory / "report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        path.chmod(0o600)
    return path

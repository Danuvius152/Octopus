"""Record optional query execution without grading natural-language answers."""

import hashlib
import json
import shutil
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from server.harness import get_harness
from tests.wiki_baseline import coverage
from tests.wiki_cost_probe import source_fingerprint


def guarded_credential(manager, credential_id, home_dir):
    """Check the registered login again at each actual invocation boundary."""
    original = manager._resolve_credential
    expected = Path(home_dir).resolve()

    async def resolve(session, agent, harness):
        assert session.credential_id == credential_id, "registered credential selection changed"
        resolved = await original(session, agent, harness)
        assert resolved and resolved.home_dir and Path(resolved.home_dir).resolve() == expected, "selected credential unavailable; default-login fallback forbidden"
        return resolved

    return resolve


def evaluation_source_fingerprint():
    root = Path(__file__).resolve().parents[1]
    return source_fingerprint() | {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest() for relative in (
            'tests/wiki_query_evidence.py', 'tests/test_wiki_query_real.py',
            'tests/test_wiki_chat_real.py', 'tests/test_wiki_baseline_real.py',
        )
    }


def observe_invocations(monkeypatch, probe):
    """Record configured controls and actual CLI flags, without prompts or bearers.

    App Server transmits controls by RPC; its absent CLI model flags stay absent.
    Verified response settings are collected independently from the runtime sink.
    """
    harness = get_harness('codex')
    original = harness.profile.build_turn_argv
    probe.input_context['source_sha256'] = evaluation_source_fingerprint()

    def build(context):
        argv, options = original(context)
        if context.credential is not None:
            controls = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == '-c'
                        and argv[index + 1].startswith(('model_reasoning_effort=', 'service_tier='))]
            probe.input_context.setdefault('rendered_attempt_profiles', []).append({
                'configured_model': context.model, 'configured_effort': context.reasoning_effort,
                'configured_tier': context.service_tier,
                'model_argument': argv[argv.index('-m') + 1] if '-m' in argv else None,
                'control_arguments': controls,
            })
        return argv, options

    monkeypatch.setattr(harness, 'profile', replace(harness.profile, build_turn_argv=build))


def observe_model_measurements(monkeypatch, probe):
    """Retain verified native response metadata already observed by the runtime."""
    from server.harness.run import HarnessRun

    original = HarnessRun.record_measurement

    def record(run, event, *, request_id, attempt_id):
        if event.type == 'model_measurement' and event.raw.get('kind') == 'codex_response':
            probe.model_observations.append({
                'session_id': run._config.session_id, 'request_id': request_id,
                'attempt_id': attempt_id, **deepcopy(event.raw),
            })
        return original(run, event, request_id=request_id, attempt_id=attempt_id)

    monkeypatch.setattr(HarnessRun, 'record_measurement', record)


def load_registration(path):
    registration = json.loads(Path(path).read_text())
    questions = registration['questions']
    assert 1 <= len(questions) <= 4 and len({item['id'] for item in questions}) == len(questions)
    assert all(isinstance(item['question'], str) and item['question'].strip() for item in questions)
    root = Path(registration['source_root']).resolve(strict=True)
    digest = hashlib.sha256(json.dumps(source_manifest(root), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert digest == registration['source_manifest_sha256'], 'registered source version changed'
    assert isinstance(registration['credential_id'], str) and registration['credential_id']
    profile = registration.get('profile', {'model': 'gpt-6.1-sol', 'reasoning_effort': 'high', 'service_tier': 'default'})
    assert set(profile) == {'model', 'reasoning_effort', 'service_tier'}
    assert all(isinstance(value, str) and value for value in profile.values()), 'explicit model controls required'
    registration['profile'] = profile
    return registration


def copy_registered_sources(registration, destination):
    """Each evaluation owns its source copy; live updates cannot taint later cases."""
    shutil.copytree(registration['source_root'], destination)
    digest = hashlib.sha256(json.dumps(source_manifest(destination), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert digest == registration['source_manifest_sha256'], 'registered sources changed during copy'
    return destination

def source_manifest(root: Path) -> list[dict]:
    result = []
    for path in sorted(root.rglob("*.md")):
        raw = path.read_bytes()
        result.append({"path": path.relative_to(root).as_posix(), "bytes": len(raw),
                       "sha256": hashlib.sha256(raw).hexdigest()})
    return result


async def collect_evaluation(probe, question: str, *, case_id: str, output: Path, expected_sources=None) -> Path:
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

    def source_reading():
        versions = {item['path']: item['sha256'] for item in before}
        result = []
        for source in expected_sources or []:
            path = source['path']
            text = (directory / 'sources' / path).read_bytes().decode('utf-8-sig')
            result.append({'path': path, 'revision': versions[path], 'required_full': source.get('full', False),
                           **coverage(getattr(probe, 'call_trace', []), path, versions[path], text)})
        return result

    try:
        answer = await probe.ask(question)
        for source in source_reading():
            assert not source['required_full'] or source['complete'], f"Missing full reading: {source['path']} {source['missing']}"
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
            "source_coverage": source_reading(), "expected_sources": expected_sources,
            "normalized_events": probe.last_events,
            "elapsed_seconds": time.monotonic() - started,
            "timing_scope": "Query execution and source checks; excludes CLI availability probe and test fixture setup/teardown",
            "usage_observations": getattr(probe, "usage_observations", []),
            "native_model_observations": getattr(probe, "model_observations", []),
            "cleanup_failures": getattr(probe, "cleanup_failures", []),
            "total_input_plus_output_tokens": None, "USD": None,
            "usage_limit": "Raw counters may be cumulative or omit helpers/recovery; retain observations without summing or treating unknown usage as zero",
        }
        path = directory / "report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        path.chmod(0o600)
    return path

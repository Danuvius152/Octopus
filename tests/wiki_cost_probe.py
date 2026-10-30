"""Separate uninstrumented timings from diagnostic work counts for Wiki probes."""

import hashlib
import json
import math
import os
import platform
import sqlite3
import time
import tracemalloc
from collections import Counter
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from statistics import median

import pytest

from server.wiki import format, storage

NOISE_RULE = {
    'pairing': 'Adjacent uninstrumented baseline pairs: seven warm, three cold; no candidate data chooses tolerance.',
    'relative_floor': .20, 'absolute_floor_ms': .5, 'noise_multiplier': 3,
    'decision': 'Candidate p50/p95 must stay within respective baseline percentile plus max(20%, three times maximum adjacent-pair relative noise, 0.5ms). Investigate peak increases; correctness remains a hard gate.',
    'transfer': 'Long-document compact delivered text bytes must fall at least 50%; no per-hit full-row query. Diagnostic samples must remain bounded independently of total errors.',
}


def source_fingerprint():
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "server").rglob("*.py"))
    paths += sorted((root / "server/wiki/prompts").glob("*.md"))
    paths += [root / name for name in (
        "tests/wiki_cost_probe.py", "tests/test_wiki_efficiency_matrix.py",
        "tests/conftest.py", "tests/wiki_baseline.py", "tests/test_wiki_phase1_baseline.py",
        "tests/fixtures/wiki-baseline-questions.json", "pyproject.toml",
    )]
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.exists()}


@pytest.fixture(scope="session")
def cost_report(pytestconfig, tmp_path_factory):
    assert not pytestconfig.getoption('numprocesses', default=0), 'Cost measurements require one serial executor'
    measured_root = Path(storage.__file__).resolve().parents[2]
    assert measured_root == Path(__file__).resolve().parents[1], "Tests and measured server must use the same snapshot"
    destination = pytestconfig.getoption("wiki_perf_output")
    path = Path(destination) if destination else tmp_path_factory.mktemp("wiki-cost-report") / "report.json"
    report = {
        "schema": 2,
        "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                        "platform": platform.platform(), "cpu_count": os.cpu_count(),
                        "load_average": os.getloadavg(), "test_root": str(tmp_path_factory.getbasetemp()),
                        "source_root": str(measured_root), "machine": platform.machine(),
                        "parsers": {name: version(name) for name in ("markdown-it-py", "PyYAML", "jieba")}},
        "method": {
            "timings": "Uninstrumented serial samples; preparation, correctness assertions and JSON encoding excluded. p50/p95 use empirical nearest rank, descriptive samples, not production percentiles.",
            "diagnostics": "One separate operation after timing samples; SQLite VM steps in multiples of 100, excluding connection initialization and commit. UTF-8 text and blob bytes count cells actually returned to Python, not SQLite internal I/O or Python object overhead. Query plans are collected after disabling counters. Path.read_bytes/stat under fixture roots; Markdown parses and os.walk inventories. Optional traced peak includes diagnostic instrumentation and is Python allocations, not RSS.",
            "cold": "Fresh application index or explicitly cleared coordinate cache; OS page cache is not evicted. Tokenizer first-use may affect the first build sample.",
        },
        "source_sha256": source_fingerprint(), "cases": [],
    }

    def save():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)

    save()
    yield report, save
    report["source_unchanged"] = source_fingerprint() == report["source_sha256"]
    save()
    assert report["source_unchanged"], "Measured source changed during the run"
    print(f"Wiki efficiency report: {path}", flush=True)


@contextmanager
def work_counts(roots, *, memory=False):
    counts = Counter(connections=0, sql_statements=0, vm_steps=0, file_reads=0,
                     file_bytes=0, file_stats=0, inventories=0, markdown_parses=0, parsed_characters=0,
                     sqlite_rows=0, sqlite_text_bytes=0, sqlite_blob_bytes=0)
    plans = {}
    roots = tuple(Path(root) for root in roots)
    connect, read_bytes, stat, walk, parse = storage.connect, Path.read_bytes, Path.stat, os.walk, format._markdown.parse

    def tracked(path):
        return any(path.is_relative_to(root) for root in roots)

    def progress():
        counts["vm_steps"] += 100
        return 0

    @contextmanager
    def connection(*args, **kwargs):
        counts["connections"] += 1
        with connect(*args, **kwargs) as db:
            factory = db.row_factory
            queries = set()

            def row(cursor, values):
                counts['sqlite_rows'] += 1
                counts['sqlite_text_bytes'] += sum(len(value.encode()) for value in values if isinstance(value, str))
                counts['sqlite_blob_bytes'] += sum(len(value) for value in values if isinstance(value, bytes))
                return factory(cursor, values) if factory else values

            def statement(sql):
                counts['sql_statements'] += 1
                if sql.lstrip().upper().startswith(('SELECT', 'WITH')):
                    queries.add(sql)

            db.row_factory = row
            db.set_progress_handler(progress, 100)
            db.set_trace_callback(statement)
            try:
                yield db
            finally:
                db.set_trace_callback(None)
                db.set_progress_handler(None, 0)
                db.row_factory = factory
                for sql in sorted(queries - plans.keys()):
                    plans[sql] = [item[3] for item in db.execute('EXPLAIN QUERY PLAN ' + sql)]

    def read(path, *args, **kwargs):
        raw = read_bytes(path, *args, **kwargs)
        if tracked(path):
            counts.update(file_reads=1, file_bytes=len(raw))
        return raw

    def metadata(path, *args, **kwargs):
        if tracked(path):
            counts["file_stats"] += 1
        return stat(path, *args, **kwargs)

    def inventory(path, *args, **kwargs):
        if tracked(Path(path)):
            counts["inventories"] += 1
        yield from walk(path, *args, **kwargs)

    def markdown(text, *args, **kwargs):
        counts.update(markdown_parses=1, parsed_characters=len(text))
        return parse(text, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(storage, "connect", connection)
        patch.setattr(Path, "read_bytes", read)
        patch.setattr(Path, "stat", metadata)
        patch.setattr(os, "walk", inventory)
        patch.setattr(format._markdown, "parse", markdown)
        if memory:
            tracemalloc.start()
        try:
            yield counts
        finally:
            if memory:
                _, counts["peak_python_bytes"] = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            counts['query_plans'] = [{'sql': sql, 'plan': plan} for sql, plan in plans.items()]


def response_size(result):
    if hasattr(result, "content") and hasattr(result, "status_code"):
        return len(result.content)
    if isinstance(result, (dict, list, tuple, str, int, float, bool)) or result is None:
        return len(json.dumps(result, ensure_ascii=False).encode())
    return None


class CostProbe:
    def __init__(self, report, save, node):
        self.report, self.save, self.node = report, save, node

    def start(self, name, dimensions):
        row = {"test": self.node, "name": name, "dimensions": dimensions, "samples": [], "status": "running"}
        self.report["cases"].append(row)
        self.save()
        return row

    def finish(self, row):
        milliseconds = [sample["wall_ms"] for sample in row["samples"]]
        ordered = sorted(milliseconds)
        row.update(median_ms=median(milliseconds), min_ms=min(milliseconds), max_ms=max(milliseconds),
                   p50_ms=ordered[math.ceil(len(ordered) * .5) - 1],
                   p95_ms=ordered[math.ceil(len(ordered) * .95) - 1], status="passed")
        self.save()
        print(json.dumps({"case": row["name"], **row["dimensions"], "median_ms": round(row["median_ms"], 3),
                          "diagnostics": {key: value for key, value in row.get("diagnostics", {}).items()
                                          if key != 'query_plans'}}), flush=True)

    def settle_timing(self, row, reference):
        if reference is not None:
            originals = [case for case in reference['cases']
                         if (case['test'], case['name'], case['dimensions']) == (row['test'], row['name'], row['dimensions'])]
            if len(originals) != 1 or 'prospective_tolerance' not in originals[0]:
                row.update(status='failed', failure=f'Missing unique frozen baseline limits: {row["name"]} {row["dimensions"]}')
                self.save()
                raise AssertionError(row['failure'])
            limits = originals[0]['prospective_tolerance']
            row['baseline_comparison'] = {key: limits[key] for key in ('p50_upper_ms', 'p95_upper_ms')}
            exceeded = [key for key in ('p50', 'p95') if row[key + '_ms'] > limits[key + '_upper_ms']]
            if exceeded:
                row.update(status='failed', failure='Exceeded original baseline limits: ' + ', '.join(exceeded))
            self.save()
            assert not exceeded, row.get('failure')
            return
        samples = [sample['wall_ms'] for sample in row['samples']]
        ratios = [abs(a - b) / max(min(a, b), .001) for a, b in zip(samples[::2], samples[1::2], strict=True)]
        relative = max(NOISE_RULE['relative_floor'], NOISE_RULE['noise_multiplier'] * max(ratios))
        row['prospective_tolerance'] = {
            'relative': relative, 'absolute_ms': NOISE_RULE['absolute_floor_ms'],
            'p50_upper_ms': row['p50_ms'] + max(row['p50_ms'] * relative, NOISE_RULE['absolute_floor_ms']),
            'p95_upper_ms': row['p95_ms'] + max(row['p95_ms'] * relative, NOISE_RULE['absolute_floor_ms']),
            'paired_relative_noise': ratios,
        }
        self.save()

    def measure(self, name, operation, check, *, roots=(), dimensions=None, prepare=None, repeat=5, memory=False, diagnose=True,
                baseline=False, reference=None):
        row = self.start(name, dimensions or {})
        for number in range(repeat):
            if prepare:
                prepare(number)
            cpu, began = time.process_time(), time.perf_counter()
            result = operation()
            sample = {"wall_ms": (time.perf_counter() - began) * 1000, "cpu_ms": (time.process_time() - cpu) * 1000}
            check(result)
            sample["response_bytes"] = response_size(result)
            row["samples"].append(sample)
            self.save()
        if diagnose:
            if prepare:
                prepare(repeat)
            with work_counts(roots, memory=memory) as counts:
                result = operation()
            check(result)
            row["diagnostics"] = dict(counts)
        self.finish(row)
        if baseline:
            self.settle_timing(row, reference)
        return row

    async def measure_async(self, name, operation, check, *, roots=(), dimensions=None, repeat=5, diagnose=True):
        row = self.start(name, dimensions or {})
        for _ in range(repeat):
            cpu, began = time.process_time(), time.perf_counter()
            result = await operation()
            sample = {"wall_ms": (time.perf_counter() - began) * 1000, "cpu_ms": (time.process_time() - cpu) * 1000}
            check(result)
            sample["response_bytes"] = response_size(result)
            row["samples"].append(sample)
            self.save()
        if diagnose:
            with work_counts(roots) as counts:
                result = await operation()
            check(result)
            row["diagnostics"] = dict(counts)
        self.finish(row)
        return row


@pytest.fixture
def cost_probe(cost_report, request):
    report, save = cost_report
    return CostProbe(report, save, request.node.nodeid)

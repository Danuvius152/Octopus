"""Import gate behavior without rewriting or importing real test cases."""

import json

from scripts.check_test_imports import check, imports


def test_static_spellings_nested_relative_and_support_imports(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_consumer.py").write_text('''
from tests.test_source import fixture as reused
from tests import test_source
import tests.test_other as other
from .test_relative import helper
from tests.support import helper
def later():
    from tests.test_deferred import delayed
''')
    found = imports(tmp_path)
    assert len(found) == 5
    assert "tests/test_consumer.py:tests.test_relative:helper" in found
    assert "tests/test_consumer.py:tests.test_deferred:delayed" in found
    assert not any("support" in key for key in found)


def test_baseline_allows_only_original_consumer_symbol_and_cannot_grow(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    path = tests / "test_consumer.py"
    path.write_text("from tests.test_source import helper\n")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(list(imports(tmp_path))))
    assert not check(tmp_path, baseline)
    path.write_text("from tests.test_source import helper, new_helper\n")
    assert len(check(tmp_path, baseline, prune=True)) == 1
    assert "new_helper" not in baseline.read_text()
    path.write_text("from tests.support import helper\n")
    assert not check(tmp_path, baseline, prune=True)
    assert json.loads(baseline.read_text()) == []


def test_new_consumer_does_not_inherit_another_files_exception(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_original.py").write_text("from tests.test_source import helper\n")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(list(imports(tmp_path))))
    (tests / "test_new.py").write_text("from tests.test_source import helper\n")
    assert "test_new.py:1:" in check(tmp_path, baseline)[0]

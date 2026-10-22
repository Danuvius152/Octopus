"""Optional evaluation and performance tiers are independent of correctness."""

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("evaluations,performance,expected_names", [
    (False, False, ["first", "second"]),
    (True, False, ["first", "quality", "second"]),
    (False, True, ["first", "scale", "second"]),
    (True, True, ["first", "quality", "scale", "both", "second"]),
])
def test_optional_selection_preserves_order_markers_and_setup(
    request, evaluations, performance, expected_names,
):
    path = Path(__file__).with_name("conftest.py").resolve()
    plugin = next(plugin for plugin in request.config.pluginmanager.get_plugins()
                  if getattr(plugin, "__file__", None) and Path(plugin.__file__).resolve() == path)

    class Item:
        def __init__(self, name, *optional_markers):
            self.name = name
            self.markers = {"real", "real_codex", *optional_markers}

        def get_closest_marker(self, name):
            return name if name in self.markers else None

        def setup(self):
            raise AssertionError("collection must not run setup")

    items = [Item("first"), Item("quality", "knowledge_eval"),
             Item("scale", "wiki_perf"), Item("both", "knowledge_eval", "wiki_perf"),
             Item("second")]
    original = list(items)
    markers = [set(item.markers) for item in items]
    deselected = []
    config = SimpleNamespace(
        getoption=lambda name: {"run_knowledge_evals": evaluations,
                                "run_wiki_perf": performance}[name],
        hook=SimpleNamespace(pytest_deselected=lambda *, items: deselected.extend(items)),
    )
    plugin.pytest_collection_modifyitems(config, items)
    assert [item.name for item in items] == expected_names
    assert deselected == [item for item in original if item.name not in expected_names]
    assert [item.markers for item in original] == markers

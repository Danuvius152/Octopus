"""Default selection excludes evaluations without changing mechanism tests."""

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("with_evaluations", [False, True])
def test_optional_selection_preserves_order_markers_and_setup(request, enabled, with_evaluations):
    path = Path(__file__).with_name("conftest.py").resolve()
    plugin = next(plugin for plugin in request.config.pluginmanager.get_plugins()
                  if getattr(plugin, "__file__", None) and Path(plugin.__file__).resolve() == path)

    class Item:
        def __init__(self, name, evaluation=False):
            self.name = name
            self.markers = {"real", "real_codex"} | ({"knowledge_eval"} if evaluation else set())

        def get_closest_marker(self, name):
            return name if name in self.markers else None

        def setup(self):
            raise AssertionError("collection must not run setup")

    mechanism = [Item("first"), Item("second")]
    evaluation = Item("quality", evaluation=True)
    items = [mechanism[0], evaluation, mechanism[1]] if with_evaluations else list(mechanism)
    original = list(items)
    markers = [set(item.markers) for item in items]
    deselected = []
    config = SimpleNamespace(
        getoption=lambda name: enabled,
        hook=SimpleNamespace(pytest_deselected=lambda *, items: deselected.extend(items)),
    )
    plugin.pytest_collection_modifyitems(config, items)
    assert items == (original if enabled else mechanism)
    assert deselected == ([evaluation] if with_evaluations and not enabled else [])
    assert [item.markers for item in original] == markers

"""Launch pytest with controller diagnostics and the selected entry's imports."""

import os
import sys


def main() -> int:
    evidence, entry, *args = sys.argv[1:]
    module_entry = entry == "-m"
    if not sys.flags.safe_path:
        sys.path[0] = os.getcwd() if module_entry else os.path.dirname(os.path.realpath(entry))
    if module_entry:
        from pytest import __main__ as pytest_entry

        entry = str(pytest_entry.__file__)
        args = args[1:]
    sys.argv[:] = [entry, *args]

    from importlib import import_module
    from pathlib import Path
    from types import ModuleType

    # Load the runner's own modules without exposing checkout paths to pytest imports.
    package = ModuleType("_octopus_pytest_diagnostics")
    package.__path__ = [str(Path(__file__).parent)]
    sys.modules[package.__name__] = package
    plugin = import_module(f"{package.__name__}.pytest_evidence")
    return int(plugin.run_diagnostic_pytest(Path(evidence), args))


if __name__ == "__main__":
    sys.exit(main())

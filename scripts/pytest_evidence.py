"""Persist pytest failures and worker topology from the controller."""

from __future__ import annotations

import os
import sys
from argparse import Namespace
from collections.abc import Callable, Generator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from _pytest.config import ConftestImportFailure

from .check_runner import atomic_json

if TYPE_CHECKING:
    from _pytest._code.code import ExceptionRepr
    from pluggy import HookImpl
    from xdist.workermanage import WorkerController


class DiagnosticPlugin:
    def __init__(self, path: Path) -> None:
        self.path = path

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_cmdline_main(self, config: pytest.Config) -> Generator[None, int, int]:
        evidence = ExecutionEvidence(self.path, config, config.option)
        config.pluginmanager.register(evidence)
        # Observe reports before dispatch, including before tryfirst wrappers.
        undo_monitor = config.pluginmanager.add_hookcall_monitoring(evidence.observe_hook, lambda *_: None)
        config.add_cleanup(undo_monitor)
        try:
            return (yield)
        except (Exception, SystemExit) as error:
            evidence.record_exception(error, phase="internal")
            raise

    @pytest.hookimpl(wrapper=True)
    def pytest_cmdline_parse(
        self, pluginmanager: pytest.PytestPluginManager,
    ) -> Generator[None, pytest.Config, pytest.Config]:
        try:
            return (yield)
        except (Exception, SystemExit) as error:
            config = pluginmanager.get_plugin("pytestconfig")
            if isinstance(config, pytest.Config):
                options = getattr(config, "known_args_namespace", config.option)
                # The preliminary parser may mistake values for targets before plugins load.
                evidence = ExecutionEvidence(self.path, config, options)
                evidence.record_exception(error, phase="collection")
            raise


def run_diagnostic_pytest(path: Path, args: list[str]) -> int:
    # Early plugin imports can fail before a plugin loaded through -p can observe them.
    return pytest.main(args, plugins=[DiagnosticPlugin(path)])


def exception_location(error: BaseException) -> dict[str, str | int | None] | None:
    if isinstance(error, (ImportError, pytest.UsageError)) and error.__cause__ is not None:
        error = error.__cause__
    if isinstance(error, SyntaxError) and error.filename:
        return {"file": error.filename, "line": error.lineno}
    trace = error.__traceback__
    if trace is not None:
        previous = trace
        while trace.tb_next is not None:
            previous, trace = trace, trace.tb_next
        # pytest.exit hides its helper frame. Report the calling plugin or test.
        if isinstance(error, pytest.exit.Exception) and trace.tb_frame.f_locals.get("__tracebackhide__") is True:
            trace = previous
        file = trace.tb_frame.f_code.co_filename
        if not file.startswith("<"):
            return {"file": file, "line": trace.tb_lineno}
    return None


def diagnostic_text(render: Callable[[], str], original: object | None = None) -> str:
    try:
        return render()
    except (Exception, SystemExit):
        pass
    if original is not None:
        try:
            return str(original)
        except (Exception, SystemExit):
            pass
    return "<unavailable failure text>"


def failure_details(report: pytest.TestReport | pytest.CollectReport) -> dict[str, Any]:
    location = None
    original = report.longrepr
    try:
        crash = getattr(original, "reprcrash", None)
        if crash is not None:
            file, line = os.fspath(crash.path), crash.lineno
            if isinstance(file, str) and file and isinstance(line, int) and not isinstance(line, bool) and line > 0:
                location = {"file": file, "line": line}
    except (Exception, SystemExit):
        pass
    if location is None and report.location is not None:
        file, line, _ = report.location
        if file:
            location = {"file": file, "line": line + 1 if line is not None else None}
    return {"error": diagnostic_text(lambda: report.longreprtext, original), "location": location}


class ExecutionEvidence:
    def __init__(self, path: Path, config: pytest.Config, options: Namespace) -> None:
        self.path = path
        self.config = config
        self.options = options
        # Pytest reports explicit exits through one interrupt hook without a phase.
        self.exit_phase = "startup"
        self.data: dict[str, Any] = {
            "targets": config.args,
            "marker": getattr(options, "markexpr", ""),
            "keyword": getattr(options, "keyword", ""),
            "worker_override": os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"),
            "cpu_affinity_count": len(os.sched_getaffinity(0)),
            "workers_started": 0,
            "worker_crashes": [],
            "failures": [],
            "exit_code": None,
        }

    def observe_worker_reports(self) -> None:
        # xdist imports this module when it creates its distributed controller.
        if "xdist.dsession" not in sys.modules:
            return
        from xdist.dsession import DSession

        distributed = self.config.pluginmanager.get_plugin("dsession")
        if not isinstance(distributed, DSession):
            return
        forward_collection = distributed.worker_collectreport
        forward_internal = distributed.worker_internal_error

        def collectreport(node: WorkerController, rep: pytest.CollectReport | pytest.TestReport) -> None:
            rep.worker_id = node.gateway.id  # type: ignore[union-attr]
            forward_collection(node, rep)

        def internal_error(node: WorkerController, formatted_error: str) -> None:
            self.record_failure({"nodeid": None, "phase": "internal", "worker": node.gateway.id,
                                 "error": formatted_error, "location": None})
            forward_internal(node, formatted_error)

        # xdist 3.8.0 dispatches these queue events on the controller with the sender.
        patches = pytest.MonkeyPatch()
        patches.setattr(distributed, "worker_collectreport", collectreport)
        patches.setattr(distributed, "worker_internal_error", internal_error)
        self.config.add_cleanup(patches.undo)

    def observe_hook(self, name: str, _impls: Sequence[HookImpl], args: Mapping[str, object]) -> None:
        if name in ("pytest_runtest_logreport", "pytest_collectreport"):
            self.record_report(cast("pytest.TestReport | pytest.CollectReport", args["report"]))
        elif name == "pytest_internalerror":
            self.record_internal_error(cast("ExceptionRepr", args["excrepr"]),
                                       cast("pytest.ExceptionInfo[BaseException]", args["excinfo"]))
        elif name == "pytest_sessionstart":
            self.observe_worker_reports()
        elif name == "pytest_collection":
            self.exit_phase = "collection"
        elif name == "pytest_runtestloop":
            self.exit_phase = "internal"
        elif name == "pytest_keyboard_interrupt":
            error = cast("pytest.ExceptionInfo[BaseException]", args["excinfo"]).value
            if isinstance(error, pytest.exit.Exception) and error.returncode:
                self.record_exception(error, phase=self.exit_phase)
        elif name == "pytest_testnodeready":
            self.data["workers_started"] += 1
        elif name == "pytest_testnodedown" and args["error"] is not None:
            node = cast("WorkerController", args["node"])
            self.data["worker_crashes"].append({"worker": node.gateway.id,
                                                "error": diagnostic_text(lambda: str(args["error"]))})
            self.save()

    def record_report(self, report: pytest.TestReport | pytest.CollectReport) -> None:
        if not report.failed:
            return
        failure = {"nodeid": report.nodeid, "phase": "collection", "worker": getattr(report, "worker_id", None),
                   **failure_details(report)}
        if isinstance(report, pytest.TestReport):
            failure.update(phase=report.when, duration_seconds=report.duration)
            if failure["worker"] is None and hasattr(report, "node"):
                failure["worker"] = report.node.gateway.id
        self.record_failure(failure)

    def record_internal_error(self, excrepr: ExceptionRepr, excinfo: pytest.ExceptionInfo[BaseException]) -> None:
        value = excinfo.value
        if isinstance(value, AssertionError):
            frame = excinfo.traceback[-1].frame
            # The original worker error was recorded before xdist wrapped it for pytest.
            if frame.f_globals.get("__name__") == "xdist.dsession" and frame.code.name == "worker_internal_error":
                return
        self.record_failure({
            "nodeid": None, "phase": "internal", "worker": None,
            "error": diagnostic_text(lambda: str(excrepr), value), "location": exception_location(value),
        })

    def record_exception(self, error: BaseException, *, phase: str) -> None:
        if isinstance(error, SystemExit) and (error.code is None or isinstance(error.code, int) and error.code == 0):
            return
        nodeid = None
        if isinstance(error, ConftestImportFailure):
            nodeid = os.path.relpath(error.path, self.config.rootpath)
            error = error.cause
        location = exception_location(error)
        if location is None and nodeid:
            location = {"file": nodeid, "line": None}
        self.record_failure({
            "nodeid": nodeid, "phase": phase, "worker": None,
            "error": diagnostic_text(lambda: str(pytest.ExceptionInfo.from_exception(error).getrepr()), error),
            "location": location,
        })

    def record_failure(self, failure: dict[str, Any]) -> None:
        self.data["failures"].append(failure)
        self.save()

    def pytest_sessionfinish(self, exitstatus: int) -> None:
        self.data["exit_code"] = int(exitstatus)
        self.save()

    def save(self) -> None:
        # xdist normalizes scheduling and worker counts after our early registration.
        self.data.update(
            distribution=getattr(self.options, "dist", "no"),
            workers_requested=getattr(self.options, "numprocesses", None) or 0,
            worker_limit=getattr(self.options, "maxprocesses", None),
        )
        try:
            atomic_json(self.path, self.data)
        except OSError as error:
            print(f"Cannot save pytest diagnostic evidence {self.path}: {error}", file=sys.__stderr__)

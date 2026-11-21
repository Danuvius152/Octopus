"""Record the actual pytest worker topology and failing nodes in the controller."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from scripts.check_runner import atomic_json

if TYPE_CHECKING:
    from xdist.workermanage import WorkerController


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--execution-evidence", type=Path, help="Write controller-side execution evidence")


def pytest_configure(config: pytest.Config) -> None:
    path = config.getoption("execution_evidence")
    if path is not None and not hasattr(config, "workerinput"):
        config.pluginmanager.register(ExecutionEvidence(config, path))


@pytest.hookimpl(wrapper=True)
def pytest_report_to_serializable(
    config: pytest.Config, report: pytest.CollectReport | pytest.TestReport,
) -> Generator[None, dict[str, Any] | None, dict[str, Any] | None]:
    data = yield
    if (data is not None and isinstance(report, pytest.CollectReport) and report.failed
            and hasattr(config, "workerinput")):
        data["worker_id"] = config.workerinput["workerid"]
    return data


class ExecutionEvidence:
    def __init__(self, config: pytest.Config, path: Path) -> None:
        self.path = path
        self.data = {
            "targets": config.args,
            "marker": config.getoption("markexpr"),
            "keyword": config.getoption("keyword"),
            "distribution": config.getoption("dist", default="no"),
            "workers_requested": config.getoption("numprocesses", default=0) or 0,
            "worker_limit": config.getoption("maxprocesses", default=None),
            "worker_override": os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"),
            "cpu_affinity_count": len(os.sched_getaffinity(0)),
            "workers_started": 0,
            "worker_crashes": [],
            "failures": [],
            "exit_code": None,
        }

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodeready(self, node: WorkerController) -> None:
        self.data["workers_started"] += 1

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: WorkerController, error: object) -> None:
        if error is not None:
            self.data["worker_crashes"].append({"worker": node.gateway.id, "error": str(error)})
            atomic_json(self.path, self.data)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.failed:
            worker = getattr(report, "worker_id", None)
            if worker is None and hasattr(report, "node"):
                worker = report.node.gateway.id
            self.data["failures"].append({
                "nodeid": report.nodeid, "phase": report.when,
                "worker": worker, "duration_seconds": report.duration,
            })
            atomic_json(self.path, self.data)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.data["failures"].append({
                "nodeid": report.nodeid, "phase": "collection", "worker": getattr(report, "worker_id", None),
            })
            atomic_json(self.path, self.data)

    def pytest_sessionfinish(self, exitstatus: int) -> None:
        self.data["exit_code"] = int(exitstatus)
        atomic_json(self.path, self.data)

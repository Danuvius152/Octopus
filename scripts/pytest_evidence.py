"""Record the actual pytest worker topology and failing nodes in the controller."""

import os
from pathlib import Path

import pytest

from scripts.check_runner import atomic_json


def pytest_addoption(parser):
    parser.addoption("--execution-evidence", type=Path, help="Write controller-side execution evidence")


def pytest_configure(config):
    path = config.getoption("execution_evidence")
    if path is not None and not hasattr(config, "workerinput"):
        config.pluginmanager.register(ExecutionEvidence(config, path))


class ExecutionEvidence:
    def __init__(self, config, path):
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
            "failures": [],
            "exit_code": None,
        }

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodeready(self, node):
        self.data["workers_started"] += 1

    def pytest_runtest_logreport(self, report):
        if report.failed:
            worker = getattr(report, "worker_id", None)
            if worker is None and hasattr(report, "node"):
                worker = report.node.gateway.id
            self.data["failures"].append({
                "nodeid": report.nodeid, "phase": report.when,
                "worker": worker, "duration_seconds": report.duration,
            })
            atomic_json(self.path, self.data)

    def pytest_collectreport(self, report):
        if report.failed:
            self.data["failures"].append({"nodeid": report.nodeid, "phase": "collection"})
            atomic_json(self.path, self.data)

    def pytest_sessionfinish(self, exitstatus):
        self.data["exit_code"] = int(exitstatus)
        atomic_json(self.path, self.data)

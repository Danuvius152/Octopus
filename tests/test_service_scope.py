"""The pre-start checker observes the entire systemd unit, without signalling."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def scope_files(tmp_path, *, count):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    mount = tmp_path / "cgroup mount"
    unit = mount / "user@1000.service/app.slice/octopus-test.service"
    control = unit / ".control"
    control.mkdir(parents=True)
    (proc / "self/cgroup").write_text(
        "0::/user.slice/user@1000.service/app.slice/octopus-test.service/.control\n"
    )
    escaped = str(mount).replace(" ", "\\040")
    (proc / "self/mountinfo").write_text(
        f"128 119 0:25 /user.slice {escaped} rw,nosuid,nodev,noexec - cgroup2 cgroup2 rw\n"
    )
    (unit / "pids.current").write_text(str(count) + "\n")
    (control / "pids.current").write_text("1\n")
    (control / "cgroup.procs").write_text(str(os.getpid()) + "\n")
    (unit / "cgroup.procs").write_text("")
    return proc, unit


@pytest.mark.parametrize("count", [1, 2])
def test_prestart_checks_unit_root_even_when_the_checker_is_in_control(tmp_path, count):
    from server.service_scope import ScopeError, check_prestart

    proc, unit = scope_files(tmp_path, count=count)
    if count == 1:
        assert check_prestart("octopus-test.service", proc_root=proc).root == unit
    else:
        with pytest.raises(ScopeError, match="task"):
            check_prestart("octopus-test.service", proc_root=proc)


@pytest.mark.parametrize("fault", ["missing_pids", "wrong_member", "wrong_unit", "nested_unit", "duplicate_mount", "leaf_mount"])
def test_prestart_refuses_unprovable_membership(tmp_path, fault):
    from server.service_scope import ScopeError, check_prestart

    proc, unit = scope_files(tmp_path, count=1)
    if fault == "missing_pids":
        (unit / "pids.current").unlink()
    elif fault == "wrong_member":
        (unit / ".control/cgroup.procs").write_text(str(os.getpid() + 100000) + "\n")
    elif fault == "wrong_unit":
        path = proc / "self/cgroup"
        path.write_text(path.read_text().replace("/octopus-test.service/", "/prefix-octopus-test.service/"))
    elif fault == "nested_unit":
        path = proc / "self/cgroup"
        path.write_text(path.read_text().replace("/.control", "/octopus-test.service/.control"))
    elif fault == "duplicate_mount":
        path = proc / "self/mountinfo"
        path.write_text(path.read_text() * 2)
    else:
        path = proc / "self/mountinfo"
        path.write_text(path.read_text().replace("/user.slice ", "/user.slice/user@1000.service/app.slice/octopus-test.service/.control "))
    with pytest.raises(ScopeError):
        check_prestart("octopus-test.service", proc_root=proc)


def test_checker_runs_without_site_packages_and_refuses_an_unrelated_scope():
    from server import service_scope

    result = subprocess.run(
        [sys.executable, "-I", "-S", service_scope.__file__, "--unit=octopus-unassigned-test.service"],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 78, result.stderr
    assert "not unambiguously inside" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("membership,invocation_matches", [(1, True), (2, True), (1, False)])
def test_managed_git_proof_requires_empty_scope_and_current_invocation(tmp_path, monkeypatch, membership, invocation_matches):
    from server import service_installation as installation
    from server import service_scope

    proc, _ = scope_files(tmp_path, count=membership)
    unit = "octopus-test.service"
    scope = service_scope.current_scope(unit, proc_root=proc)
    invocation = "12" * 16
    properties = (
        f"MainPID={os.getpid()}\nControlGroup={scope.unit_path}\n"
        f"Type=simple\nExitType=main\nKillMode=control-group\nSendSIGKILL=yes\n"
        f"Delegate=no\nTasksAccounting=yes\nInvocationID={invocation}\n"
    )
    profile = installation.ServiceProfile(unit=unit, manager="user", paths={"db_path": str(tmp_path / "db")},
                                          host="127.0.0.1", port=8080, enable_tunnel=False)
    read_text = Path.read_text

    def load_file(path, *args, **kwargs):
        if path == Path("/etc/machine-id"):
            return "machine\n"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", load_file)
    monkeypatch.setattr(installation, "current_scope", lambda unit: service_scope.current_scope(unit, proc_root=proc))
    monkeypatch.setattr(installation, "check_prestart", lambda unit: service_scope.check_prestart(unit, proc_root=proc))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, properties))
    monkeypatch.setenv("INVOCATION_ID", invocation if invocation_matches else "23" * 16)
    proof = installation._verify_service(profile)
    if membership == 1 and invocation_matches:
        assert proof.invocation == invocation
        assert proof.covers_previous({"installation": proof.installation, "invocation": "older"})
    else:
        assert proof is None

"""The ordinary Codex entry and independent exec leaf have distinct purposes."""

import pytest

from server.harness import RunConfig, get_harness


def test_ordinary_codex_uses_native_host_without_transport_selection(tmp_path):
    run = get_harness("codex").create_run(RunConfig(mcp_servers=[]))
    argv, _ = run.build_argv("One original input", str(tmp_path))
    assert argv[1] == "app-server"
    assert run.can_steer and not run.reusable
    assert "One original input" not in argv


def test_explicit_exec_cannot_start_an_ordinary_codex_request(tmp_path):
    run = get_harness("codex").create_run(RunConfig(transport="exec", mcp_servers=[]))
    with pytest.raises(ValueError, match="research"):
        run.build_argv("Ordinary input", str(tmp_path))


def test_independent_web_leaf_keeps_its_exec_sandbox_and_empty_mcp(tmp_path):
    run = get_harness("codex").create_run(RunConfig(transport="exec", web_research=True, mcp_servers=[]))
    argv, _ = run.build_argv("Independent research", str(tmp_path))
    assert argv[1] == "exec"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "tools.web_search=true" in argv
    assert "mcp_servers={}" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv

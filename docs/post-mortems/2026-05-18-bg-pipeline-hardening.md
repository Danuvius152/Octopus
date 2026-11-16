# Background results could fail before the model received them (2026-05-18)

Status: the four failure paths below were fixed. This report records the
2026-05-18 incidents; present behavior is maintained in the linked contracts.

## Summary

Background output could exceed Linux's argument limit before Claude started.
Other turns stopped after tool use or failed while reading a large JSONL event.
A separate teardown leak left a completed pytest command alive, so its BG chip
never left `running`. Each boundary had its own limit or completion signal, but
the pipeline treated output, process exit and a completed model turn as if they
were interchangeable.

## Impact and evidence

A `bun run test:e2e` command produced about 140 KB of stdout and its result could
not be delivered. A pytest command printed `408 passed in 63.77s` but remained
running for more than six minutes until manually cancelled. Claude invocations
also ended after tool use without a result, or lost their stdout reader on a
line above 64 KB. These observations explain the failures below; the test
summary describes that historical run, not today's installation.

## Root causes and fixes

### 1. A background result exceeded the kernel's per-argument limit

`render_delivery_prompt` framed stdout/stderr as a synthesized user prompt.
The CLI received the whole prompt as one argv element. Linux's per-argument
`MAX_ARG_STRLEN` was 131,072 bytes, so `execve(2)` rejected the approximately
140 KB result with `E2BIG`. The BG capture allowance was 200 KB per stream;
bounded capture therefore did not make a single argument safe.

The fix spilled prompts above 100 KiB to a file and passed a small read pointer,
preserving the `[bg-task-result]` marker used by the UI. Regression coverage in
[test_large_prompts.py](../../tests/test_large_prompts.py) checks the threshold,
verbatim bytes, marker and actual session submission path.

### 2. CLI premature-exit-after-tool-use

Some `claude --print` invocations emitted `tool_use`, ran the tool and exited
without another model response or a `result`. EOF alone was not a successful
turn. The continuation ID had to be captured from `init`, because the expected
result event might never arrive.

The fix allowed one continuation with `continue` when tool use had occurred,
no result had arrived and the original conversation could be resumed. It also
emitted a visible auto-resume marker. Clean completion, no-tool crashes, missing
resume identity and an exhausted recovery budget did not qualify.
[test_session_manager.py](../../tests/test_session_manager.py), including
`test_run_backend_auto_respawns_on_premature_exit_after_tool`, covers recovery
and its guards. This is a Claude-specific workaround, not a general permission
to repeat work after an uncertain result.

### 3. A successful test summary did not mean the shell had exited

The initial symptom was pytest hanging during teardown. Investigation found
that `Database._ensure_connected` silently opened a new aiosqlite connection
after `close()`. Pending message tasks got one last turn during event-loop
teardown, called `flush()` and created worker threads after the fixture had
closed the database. Those threads could outlive the loop and prevent exit.

The database fix removed silent reconnection and rejected access after close.
Fixtures were also changed to close every database they created. A tunnel
reader had a separate cancellation leak: `except Exception` did not catch
`CancelledError`; `finally` took ownership of both reader tasks' cleanup.

BG gained an idle watchdog as a separate orchestration safeguard. After the
first output byte, 60 seconds of silence could terminate the process group;
commands that had never produced output did not arm that clock. External
signal termination without an internal stop reason was classified as
`interrupted`. The watchdog bounded the symptom; repairing the database
lifecycle removed the demonstrated teardown cause.

[test_bg_tasks.py](../../tests/test_bg_tasks.py) retains the silent-process,
quiet-command and external-SIGTERM regressions. Tests must await actual process
exit as well as inspect the command's summary.

### 4. A valid JSONL event exceeded asyncio's default line budget

`asyncio.create_subprocess_exec` used the default 64 KiB StreamReader limit.
A large tool result in one JSONL line raised `LimitOverrunError` and killed the
stdout reader. Increasing the subprocess line budget to 4 MiB admitted those
events; it did not remove flow control or make event size unbounded.

The reader now lives in [harness/run.py](../../server/harness/run.py).
[test_harness_core.py](../../tests/test_harness_core.py) exercises the subprocess
reader, and [stream pressure tests](../../tests/test_session_owner_stream_pressure.py)
cover bounded downstream consumption.

## Why the earlier checks missed the failures

The pytest summary was visible before fixture teardown and process exit, so it
could not establish that BG had finished. The argv and stream incidents also
show why validation must cross the actual size and protocol boundaries. The
incident record does not reconstruct every earlier test run; it cannot support
a claim that all earlier suites were green or used a particular fixture shape.

The regressions therefore need the real submission or subprocess path, explicit
missing-terminal streams, large individual events and teardown that waits for
the original resources. Increasing the number of small successful cases would
not expose these mechanisms.

## Lessons and present ownership

- Measure bytes at the boundary that consumes them: capture size, argv size and
  JSONL line size are separate budgets.
- Distinguish tool output, provider completion and resource retirement. A test
  summary or EOF cannot stand in for all three.
- Repair the lifecycle cause as well as bound its symptoms; a watchdog cannot
  make a database safe to reopen after close.

Current spill paths are account-owned, and retained references can outlive a
session; session deletion does not imply spill cleanup. Native Codex uses its
protocol for input rather than the argv workaround. BG return admission follows
the original receipt after a lost acknowledgment instead of rerunning the shell
command. Those contracts belong to [background tasks](../implemented/background-tasks.md),
[input completion and retirement](../implemented/input-reliability.md#原请求完成与失败交接)
and [Codex](../implemented/codex-backend.md). Historical method names above are
evidence of the incident, not a current call map.

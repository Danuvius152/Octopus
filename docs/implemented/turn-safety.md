# 回合安全：超时与进程退休

> **Status:** implemented — 有界 idle 恢复、可选逻辑回合总期限及确认的进程组退休。

<a id="2-process-group-reaping-both-turns-and-run_oneshot"></a>
## Process-group reaping (both turns and `run_oneshot`)

- **Own process group:** use `start_new_session=True` in `prepare_spawn`
  ([run engine](../../server/harness/run.py)) — the single spawn seam shared by
  the streaming engine and `Harness.run_oneshot`. The child
  becomes a session/group leader, so its descendants share its pgid.
- **Reap the captured group:** the spawned PID is its group ID even after
  the leader exits. `HarnessRun.stop()` closes stdin first, then signals and
  waits for the whole group, escalating TERM to KILL when necessary. The
  application backend supervisor uses the same group-exit evidence. A direct
  child exit or pipe EOF cannot release ownership while redirected children
  can still write. After KILL, Linux procfs distinguishes inert orphan zombies
  from live members, including a zombie leader with live threads. Unconfirmed
  exit retains ownership and fails retirement. One-shot calls also confirm
  group retirement after success, failure, timeout and cancellation.

<a id="3-per-turn-watchdog-idle--overall"></a>
## Per-turn watchdog (idle + overall)

OwnerRuntime tracks validated activity and the logical turn's start. Each
attempt's captured deadline wakes its original Owner; managed reader/start
operations and retirement retain their original identities.

For native Codex, a valid `willRetry:true` notification from this connection and
attempt counts as activity only for its bound active root or associated running
child. Repeated reconnect wording can be a new notification; persistence, WS or
history replay cannot refresh the clock. Nonretry/ended-child diagnostics,
passive usage and post-closure observations do not renew activity. One retry
notice does not disable the watchdog, and no activity resets the overall start.

- Idle expiry compares the last validated activity with
  `turn_idle_timeout_seconds`; the overall deadline compares the logical turn
  start with `turn_max_seconds`. The Owner records the cause, closes steering
  and retires the original backend and in-flight I/O.
- After an idle stop, recover automatically up to `turn_idle_max_retries`
  times only when the [recovery evidence](harness-transient-retry.md#recovery-evidence-in-the-original-owner)
  permits preparation or native continuation. Missing output never authorizes
  replay. Continuation tells the model to inspect existing effects and retain
  existing jobs/delegations; it does not guarantee exactly-once effects.
- Persist and broadcast `code:"turn_timeout_retry"` for each recovery. Failed
  attempt results are held for classification and retained as nonterminal
  recovery observations. Delegations, app conversations and application builds
  keep waiting through these events; only the unique final outcome ends them.
- Exhausted/unsafe idle recovery or the overall cap surfaces a durable
  `code:"turn_timeout"` error. Timeout classification still runs before the
  auth/transient/premature-exit dispatch and uses its own retry budget. Known
  native nonrecoverable facts, including auth/quota received during idle-close
  collection, veto retry without erasing an earlier timeout cause.

Config (settings, 0 = disabled): `turn_idle_timeout_seconds` (default 300 —
generous: a single web sub-turn can legitimately take tens of seconds) and
`turn_max_seconds` (default 0, disabled). A task that keeps making progress has
no total duration limit by default. When an administrator explicitly enables
the cap, it counts the entire logical turn, including every retry, and takes
precedence when both limits are exceeded.
`turn_idle_max_retries` defaults to 2; 0 disables automatic idle recovery.
Environment overrides are `OCTOPUS_TURN_IDLE_TIMEOUT_SECONDS`,
`OCTOPUS_TURN_MAX_SECONDS`, and `OCTOPUS_TURN_IDLE_MAX_RETRIES`.
These bounds apply to both backends and delegation children.

Human native blocking questions have no question-expiry deadline. The original
QuestionOwner's `waiting_questions` pauses idle while that answer is pending;
ending the wait resumes the original watchdog. The logical turn start is never
reset, so an enabled overall cap includes human thinking time. Stop, native
resolved/terminal, attempt retirement and real failure can still end the wait.
The UI and history explain this distinction using the captured policy/mode.
MCP questions retain their default 1800-second question limit and 0-disabled
setting; legacy unknown-policy and nonblocking native questions keep compatible
expiry. Unattended work instead records `needs_input` without a long human wait.
The [original-work policy](input-reliability.md#原工作交互策略与等待) owns
classification and inheritance; browser presence is not an input to it.

<a id="4-interaction-with-existing-paths"></a>
## Interaction with existing paths

- Auth-expiry / transient-retry classification and premature-exit recovery
  retain their own budgets. Captured conversation evidence survives attempts;
  a later silent failure cannot restore the original prompt over earlier work.
- A manual interrupt cancels the logical turn, including recovery. It does not
  trigger another automatic attempt and still reaps the process group.
- A retry cannot start until the original writer is proven retired. Stop or
  authorization revocation during backoff/retirement prevents another execution;
  independent background work keeps its own owner and queue rules.
<a id="5-testing"></a>
## Testing

- Reaping: a fake/scripted child that spawns a grandchild; assert the group is
  signalled (monkeypatch `os.killpg` to record, or spawn a real `sh -c 'sleep'`
  tree and assert it dies). `prepare_spawn` sets `start_new_session=True`.
- Watchdog: idle recovery with and without output on both backends, recovery
  budget exhaustion, unsafe resume rejection, a whole-turn cap across retries,
  manual interrupt during recovery, and the original disabled-retry behavior.
- Delivery: a child that recovers emits one final delegation reply, and app
  callers/builds stay running through nonterminal errors and failed results.
- Applicable affected checks and commit/release gates follow
  [testing.md](../testing.md); this document does not maintain another suite policy.

<a id="6-what-this-defers"></a>
## Watchdog scope and ownership

Owner coordinates the activity clock, timer wakeup, question suspension, observed
budget fields and execution retirement. Display updates and progress polling do
not renew the watchdog without original valid activity. Research and BG retain
their own task limits; these turn limits are not per-tool/MCP deadlines.
Question waiting follows [the input contract](input-reliability.md#原工作交互策略与等待).

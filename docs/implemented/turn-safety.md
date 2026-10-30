# Turn safety: watchdogs and process retirement

> **Status:** implemented — Bounded idle recovery, optional logical-turn deadline and confirmed process-group retirement.

## 2. Process-group reaping (both turns and `run_oneshot`)

- **Own process group:** use `start_new_session=True` in `prepare_spawn`
  (`run.py`) — the single spawn seam shared by the streaming engine
  (`run.py:222`) AND `Harness.run_oneshot` (`harness.py:152`). The child
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

## 3. Per-turn watchdog (idle + overall)

In `_run_backend`, around the `async for event in backend.stream()`:

- Track `last_event_at` (reset on every event) and the turn `start`.
- A watchdog task (also the only timer we need) wakes periodically and, if
  `now - last_event_at > turn_idle_timeout_seconds` OR
  `now - start > turn_max_seconds`, records the reason and calls
  `backend.stop()` — which emits `_STREAM_END`, so the `async for` exits
  cleanly (no generator-cancellation hazard).
- After an idle stop, recover automatically up to `turn_idle_max_retries`
  times. With no output from any attempt, retry the original prompt from its
  original resume state. Once text (including deltas) or tool use has appeared,
  require a resume id and continue the unfinished work with instructions to
  inspect existing tool outcomes and avoid duplicating jobs or delegations.
- Persist and broadcast `code:"turn_timeout_retry"` for each recovery. Failed
  attempt results are held until classification, then tagged `retrying:true`
  along with recovery markers. Delegations, app conversations, and application
  builds keep waiting through these events; only a final failure ends them.
- Exhausted/unsafe idle recovery or the overall cap surfaces a durable
  `code:"turn_timeout"` error. Timeout classification still runs before the
  auth/transient/premature-exit dispatch and uses its own retry budget.

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

## 4. Interaction with existing paths

- Auth-expiry / transient-retry classification and premature-exit recovery
  retain their own budgets. Recovery remembers output across attempts so a
  later no-output failure cannot replay the original request over earlier work.
- A manual interrupt cancels the logical turn, including recovery. It does not
  trigger another automatic attempt and still reaps the process group.
- `start_new_session=True` matches what `codex_login` and bg-tasks already do;
  no behavior change for healthy turns beyond detaching into a pgroup.

## 5. Testing

- Reaping: a fake/scripted child that spawns a grandchild; assert the group is
  signalled (monkeypatch `os.killpg` to record, or spawn a real `sh -c 'sleep'`
  tree and assert it dies). `prepare_spawn` now sets `start_new_session=True`.
- Watchdog: idle recovery with and without output on both backends, recovery
  budget exhaustion, unsafe resume rejection, a whole-turn cap across retries,
  manual interrupt during recovery, and the original disabled-retry behavior.
- Delivery: a child that recovers emits one final delegation reply, and app
  callers/builds stay running through nonterminal errors and failed results.
- Full backend + frontend suites.

## 6. What this defers

- **Heartbeat / "still working (Nm)" UI** — a separate UX nicety (needs a
  frontend rendering decision). The timeout already removes the indefinite
  hang; a progress heartbeat is additive and tracked separately so this unit
  stays focused on correctness. (native-deep-research surfaces its own
  per-phase progress regardless.)
- Per-tool / per-MCP timeouts (finer-grained than the turn) — not needed for
  the hang; revisit only if a specific tool class warrants it.

## Historical evidence

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/turn-safety.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.

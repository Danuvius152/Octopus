# Turn safety (Layer 1): timeout + process-group reaping

> **Status:** shipped — Bounded automatic recovery after idle stops, an optional whole-turn duration cap, and process-group reaping.

## 1. Why

A session ("stock") ran a long, tool-heavy operation (the `/deep-research`
skill) and the turn **hung forever with no output until the user interrupted**.
Root cause, two Octopus gaps (independent of deep research):

1. **No per-turn timeout.** The stream consumer blocks on
   `await self._event_queue.get()` (`run.py`) and `_run_backend`'s
   `async for event in backend.stream()` (`session_manager.py`) with no
   timeout. A turn that emits no events (a wedged tool, a background workflow
   that never reports a terminal result) blocks indefinitely. The only existing
   net is the 30-min `AskUserQuestion` auto-answer, which never fires here.
2. **No process-group reaping.** The turn subprocess is spawned without
   `start_new_session=True` and `stop()` only `terminate()/kill()`s the direct
   child (`run.py:225,270`). Nested children the CLI spawns (subagents, MCP
   servers) are **orphaned** on interrupt rather than reaped.

This is also the hard prerequisite for native-deep-research.md, whose web
"leaves" are real harness sub-turns that must be individually bounded and
reaped.

## 2. Process-group reaping (both turns and `run_oneshot`)

- **Own process group:** add `start_new_session=True` in `prepare_spawn`
  (`run.py`) — the single spawn seam shared by the streaming engine
  (`run.py:222`) AND `Harness.run_oneshot` (`harness.py:152`). The child
  becomes a session/group leader, so its descendants share its pgid.
- **Reap the group:** a `_terminate_process_group(proc)` helper does
  `os.killpg(os.getpgid(pid), SIGTERM)` → wait → `SIGKILL` the group, guarded
  for `ProcessLookupError`/`PermissionError` with a fallback to the old
  per-proc `terminate()/kill()`. `HarnessRun.stop()` uses it (keeping the
  stdin-close-first graceful step); `run_oneshot`'s timeout path kills the
  group too. bg_tasks already uses process groups (`bg_tasks.py`) — same idea.

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

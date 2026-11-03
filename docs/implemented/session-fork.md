# /fork：完整会话与工作目录复制

> **Status:** implemented — 原生历史复制或 API fork、创建不确定性与显式重放降级，保留原会话和文件。

## 1. What & why

`/rewind` branches the *conversation* (goes back to a message, archives the
parent, optionally reverts files in place). `/fork` is the **filesystem fork**: it
duplicates the CURRENT session — full conversation history — onto an
**independent full copy** of its working directory, leaving the original
session and its directory completely untouched. Use it to try changes on a
throwaway copy of the project without disturbing the original.

## 2. Behavior

- New session under the same agent/backend/credential, **renamed** (`/fork
  <name>`, default `"<parent name> (fork)"`).
- Working dir = a **literal full copy** of the parent's working dir at
  `~/.octopus/fork/<basename>-<forkid>/` (incl. .git/node_modules/.venv — the
  user chose the literal copy; created with `mkdir -p`).
- **History carried over (native-copy)**: the fork **resumes the parent's real
  conversation**, not a replay. `harness.prepare_fork_copy` copies the backend's
  native history into an independent resume identity at the fork's location, so the fork's
  first turn continues with genuine context:
  - **Claude**: copy `~/.claude/projects/<parent-slug>/<id>.jsonl` →
    `<dest-slug>/<new-id>.jsonl`, rewriting each line's `cwd`→dest and
    `sessionId`→new id. (Empirically the CLI resumes a copied *real* transcript
    fine — the old "No conversation found" was specific to *synthesized* ones.)
  - **Codex App Server**: use the pinned native `thread/fork` in the same home,
    save its actual returned identity and retire its short management host before
    installing the branch. This supports both old exec and native source histories;
    branch creation sends no model input. [Native protocol details](codex-backend.md#native-history-branching).
    New Codex branches no longer copy rollout identities by hand. Saved legacy
    initializing forks without a `native_fork` key retain their original file
    cleanup strategy. A present but malformed native checkpoint preserves the
    recovery row and files; it cannot select legacy deletion by omission.
    `ForkCheckpoint` describes saved creation evidence, not a validated artifact
    or cleanup permission. Its returned `path` remains untrusted until the
    adapter checks path/cwd; the actual returned identity is saved first so that
    a later validation failure cannot lose it. Claude's typed cleanup adapter
    explicitly rejects a native checkpoint rather than silently ignoring it.
  - **Fallback**: if the parent has no native transcript yet (never ran a turn),
    `prepare_fork_copy` returns `needs_replay=True` and we use the old replay
    path — history is tiny/empty then, so no transcript-dump problem.
  This replaced the original whole-history *replay*, which dumped the entire
  transcript into the first prompt — for a large session that spilled to a file
  the model then mis-read as a task list and went off doing the wrong thing.
- The **Octopus `messages` table** (UI history) is copied independently
  (`create_fork_session`) and is unrelated to the native transcript: the DB
  feeds the sidebar/chat, the transcript feeds the model. Both are snapshotted
  at fork time and evolve per-session afterward — the same two stores every
  session already keeps, just duplicated.
  Original seq, native source, child details, final-answer references and terminal
  request keys remain branch-local historical facts. User rows carry immutable
  `copied_input` delivery/native evidence; executable submissions and recovery
  rights are not cloned. Unknown delivery and accepted but unconfirmed steer
  remain unconfirmed. Result
  reads validate the copied rows, never consult the parent to repair a bad ref.
- **Parent untouched**: NOT archived. The fork has `forked_from_session_id` set
  (lineage → nests under the parent in the sidebar). `fork_after_seq` stays set
  to the last copied seq — it doubles as the HISTORY_REPLAY cutoff, so nulling
  it would make the first backend turn replay zero context (Vera review). The
  UI instead suppresses the "@msg" badge / renders "full copy of the working
  dir" via a `fork_is_full_copy` flag derived from `fork_metadata.full_copy`.

## 3. Implementation

- `SessionManager.duplicate_session(parent_id, *, label)` — mirrors the
  `fork_session` saga MINUS the rewind/validate-target and the parent-archive,
  PLUS the dir copy:
  1. SessionOwner reserves the fork, excluding closed admission, paused dispatch,
     active work and active delegation. The reservation excludes competing input.
  2. snapshot the transcript (`load_messages` + `last_seq`) AFTER claiming the
     guard — earlier risks a fast turn copying a post-turn dir against a
     pre-turn message list. Resolve the effective parent credential for native
     history and its cleanup (Codex needs its selected CODEX_HOME).
  3. `create_fork_session(fork_after_seq = last_seq, working_dir = dest, …)`
     copies ALL messages and commits the `initializing` row, including
     `full_copy` and the cleanup credential, before copying any directory bytes.
  4. Exclusively create dest with `mkdir`, then commit its creation receipt in
     `fork_metadata.owned_copy_dir`. A preexisting dest is refused without
     cleanup. Only after the receipt commits does `copytree` populate dest in
     `asyncio.to_thread`. The original managed operation owns that writer:
     caller cancellation leaves the operation running; cancellation of the
     command joins the actual copy before compensation or reservation release.
  5. Call
     `prepare_fork_copy(parent_working_dir, parent_resume_id, parent_credential,
     dest, new_resume_id)` → prepare independent native history; set claude_session_id
     (= new id on success, None on replay-fallback) + fork_needs_replay.
     Compensation on failure: cleanup artifacts first (passing the credential so
     Codex can find its rollout store), then remove the proven independent copy,
     and only then delete its recovery row. Directory absence is idempotent;
     a real cleanup error is logged and retains the row for startup retry.
     Both compensation and startup require the initializing full-copy identity,
     matching creation receipt and allowed path scope. A path under the copy
     base alone is no deletion permission: a rewind can borrow a ready parent's
     directory there. The sweep resolves the saved credential for native cleanup.
     API creation additionally checkpoints unknown before write and the actual
     returned ID/path before subsequent work. Recovery uses that saved strategy,
     home and fork-time credential. An unknown artifact retains row and cwd for
     inspection; it is never deleted by the old hint. The original Owner also
     supervises recovery hosts for noninstalled initializing rows.
     ACK alone leaves `cleanup_allowed` closed. Trusted, confirmed host retirement
     grants that qualification; a new management operation closes it before
     starting. Unexpected turn/item/server execution or protocol corruption
     leaves actual identity, row and cwd for inspection, including after restart.
     A missing qualification never falls back to hint-based deletion.
     The profile's precise prepare/copy/cleanup protocols replace dynamic keyword
     assembly without moving this saga or changing its retirement sequence.
  6. keep `fork_after_seq` (= replay cutoff for the fallback path + the DB
     message-copy bound); set `fork_metadata.full_copy = True` + status='ready'.
     A native management host must have confirmed retirement first.
  7. do NOT archive the parent; broadcast a `session_forked` event so other
     tabs add the new session.
- Route: `POST /api/sessions/{id}/duplicate {label?}` → returns the new
  SessionInfo (carrying `fork_is_full_copy = True`).
- Frontend: `/fork [name]` slash command → POST → add the returned session to
  the store + switch to it (parent stays). Distinct from `/rewind`. Other tabs
  fetch the full SessionInfo on the `session_forked` event.

## 4. Crash safety / limits

A crash mid-copy retains the existing initializing row and its exact directory
creation receipt. Startup uses the same artifact → owned copy → recovery row
ordering as immediate compensation. If the process dies between exclusive mkdir
and committing that receipt, an existing directory and recovery row are retained
for inspection. Old unproven ownership never grants directory removal; ready
sessions are outside this recovery sweep. There is no general orphan-directory
sweeper. Destination-allocation errors also release the parent's reservation.
Literal full copy remains the user's explicit choice and retains its size cost.

## 5. Tests

- duplicate_session: dest dir created + is a real copy; all messages copied;
  parent row/dir untouched (not archived); resume state set; `fork_after_seq`
  KEPT (= last copied seq, the replay cutoff) with `fork_metadata.full_copy`
  driving the UI; native-copy success path (parent has a transcript →
  needs_replay False + new resume id) and replay fallback (no transcript);
  rejects when the parent has an active turn; compensation (copy/cleanup
  failure) leaves no orphans / leaves row+dir for the sweep.
- `test_session_duplicate.py` and `test_account_file_lifetime.py` retain the
  initializing copy receipt, parent reservation and original bytes across
  blocked copying, cancellation and deletion. Failed rewind recovery preserves
  a full-copy parent's later edits; existing/unproven directories and ready
  copies are never reclaimed as failed copies. Artifact and directory cleanup
  failures retain retry responsibility.
- harness `prepare_fork_copy` / `_fork_copy` / `_fork_cleanup` per backend:
  Claude transcript copy/id/cwd rewrite, Codex native actual-ID fork and their
  fallback/cleanup. Rollout-copy coverage applies to saved legacy recovery only;
  it is not the creation strategy for a new Codex branch. The Claude slug is
  pinned to its real-CLI mapping.
- gated real-CLI: copy → resume → recall a codeword (both backends; skips on a
  missing CLI or an environmental auth/rate-limit failure).
- route + /fork command (frontend); e2e: /fork creates a second session,
  original remains; deferred /fork while busy fires on idle.
- `test_codex_app_server_fork.py`: actual-ID checkpoint, unknown creation and
  cold recovery, original-host retirement, copied results/details after source
  deletion, product-seq steer rewind and real Git patch/rename restoration.
  `test_codex_app_server_real.py` verifies independent source/branch continuation
  and filesystem effects for both exec-origin and native-origin history.

## Historical evidence

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/session-fork.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.

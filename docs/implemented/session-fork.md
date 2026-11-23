# /fork：完整会话与工作目录复制

> **Status:** implemented — 原生历史复制或 API fork、创建不确定性与显式重放降级，保留原会话和文件。

<a id="1-what--why"></a>
## What & why

`/rewind` branches the *conversation* (goes back to a message, archives the
parent, optionally reverts files in place). `/fork` is the **filesystem fork**: it
duplicates the CURRENT session — full conversation history — onto an
**independent full copy** of its working directory, leaving the original
session and its directory completely untouched. Use it to try changes on a
throwaway copy of the project without disturbing the original.

<a id="2-behavior"></a>
## Behavior

Planning resolves the source, destination and existing symlink ancestors before
session insertion, directory creation or copying. An equal destination or one
inside the source is refused with `invalid_copy_destination`. Relative paths
and aliases use their actual filesystem relationship. Planning holds the
nearest existing parent directory open and creates no directories.
An application session's independent full copy does not inherit responsibility
for the original application's resources or deletion.

Git administration must stay inside the copy. This includes the worktree,
private and common Git directories, object alternates, nested repositories and
submodules. A self-contained layout is copied as it stands, including ordinary
file symlinks. Linked worktrees, references that would escape the copy, and
layouts whose independence cannot be established return `unsupported_git_layout`.
The error points to using a self-contained working directory. The same rule
checks the installed copy before native history preparation because the source
can change while the copy is in progress.

- New session under the same agent/backend/credential, **renamed** (`/fork
  <name>`, default `"<parent name> (fork)"`).
- Working dir = a **literal full copy** of the parent's working dir under
  `<account-root>/fork/<basename>-<forkid>/`, beside the default `workspace/`.
  It includes directories such as `.git`, `node_modules` and `.venv`. An
  installation without accounts uses `~/.octopus/fork/`. The final working
  directory is installed exclusively.
- A former global fork stays at its saved path. An ordinary archive successor
  inherits the cwd only if the current account grants allow it. For a legacy
  copy outside the current roots, an administrator can verify ownership and
  grant the account that specific directory through `extra_roots`. The archive
  result and successor creation result are reported separately.
- **History carried over (native-copy)**: the fork **resumes the parent's real
  conversation**, not a replay. `harness.prepare_fork_copy` copies the backend's
  native history into an independent resume identity at the fork's location, so the fork's
  first turn continues with genuine context:
  - **Claude**: copy `~/.claude/projects/<parent-slug>/<id>.jsonl` →
    `<dest-slug>/<new-id>.jsonl`, rewriting each line's `cwd`→dest and
    `sessionId`→new id.
  - **Codex App Server**: use native `thread/fork` in the same home,
    save its actual returned identity and retire its short management host before
    installing the branch. This supports both old exec and native source histories;
    branch creation sends no model input. [Native protocol details](codex-backend.md#native-history-branching).
    New Codex branches use the native fork identity. Saved legacy
    initializing forks without a `native_fork` key retain their original file
    cleanup strategy. A present but malformed native checkpoint preserves the
    recovery row and files; it cannot select legacy deletion by omission.
    `ForkCheckpoint` describes saved creation evidence, not a validated artifact
    or cleanup permission. Its returned `path` remains untrusted until the
    adapter checks path/cwd; the actual returned identity is saved first so that
    a later validation failure cannot lose it. Claude's typed cleanup adapter
    explicitly rejects a native checkpoint rather than silently ignoring it.
  - **Fallback**: if the parent has no native transcript yet (never ran a turn),
    `prepare_fork_copy` returns `needs_replay=True`; the retained product
    history is installed through the explicit replay path.
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
  it would make the first backend turn replay zero context. The
  UI instead suppresses the "@msg" badge / renders "full copy of the working
  dir" via a `fork_is_full_copy` flag derived from `fork_metadata.full_copy`.

<a id="3-implementation"></a>
## Implementation

- `SessionManager.duplicate_session(parent_id, *, label)` owns this sequence:
  1. SessionOwner reserves the fork, excluding closed admission, paused dispatch,
     active work and active delegation. The reservation excludes competing input.
  2. snapshot the transcript (`load_messages` + `last_seq`) AFTER claiming the
     guard — earlier risks a fast turn copying a post-turn dir against a
     pre-turn message list. Resolve the effective parent credential for native
     history and its cleanup (Codex needs its selected CODEX_HOME).
  3. `create_fork_session(fork_after_seq = last_seq, working_dir = dest, …)`
     copies ALL messages and commits the `initializing` row, including
     `full_copy`, its creation identity and the cleanup credential, before
     allocating a directory.
  4. Reserve the shared parent through allocation. Recheck the planned binding
     and publish any missing parents from an open directory chain on the same
     mount. For accounts, bind the configured users root and destination in the
     same traversal, then stage the chain in the users root when the service
     user owns it and has exclusive write access. The bound
     destination ancestor can serve as staging under the same ownership and
     permission requirements. If neither qualifies, allocation stops before any
     directory is created. Prepublication cleanup uses the staging handles.
     After installation, sync the source and destination parents and confirm
     that the public path reaches the prepared directory before allocating a
     copy. Failed confirmation leaves any installed empty parent chain in
     place and retains the initializing row for recovery.
     Allocate the copy in a private directory and commit its exact
     `fork_metadata.owned_copy_dir` receipt. Once the receipt commits, the
     original command copies through the bound directory handle. It installs
     the populated copy without replacing any existing entry and checks the
     installed directory and Git layout. Caller cancellation leaves the
     command running. Command cancellation joins the writer before compensation
     or reservation release.
  5. Call
     `prepare_fork_copy(parent_working_dir, parent_resume_id, parent_credential,
     dest, new_resume_id)` → prepare independent native history; set claude_session_id
     (= new id on success, None on replay-fallback) + fork_needs_replay.
     Compensation on failure: cleanup artifacts first (passing the credential so
     Codex can find its rollout store), then remove the proven independent copy,
     and only then delete its recovery row. A cleanup error retains the row
     for startup retry.
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
  6. Keep `fork_after_seq` as the replay and message-copy bound. Commit
     `ready` after native preparation, confirmed host retirement and the
     directory check. The durable row still marks delivery pending.
  7. Recheck the binding after the commit before installing the live session.
     Broadcast `session_forked` to the account, then acknowledge delivery on
     the original row.
- Route: `POST /api/sessions/{id}/duplicate {label?}` → returns the new
  SessionInfo (carrying `fork_is_full_copy = True`).
- Frontend: `/fork [name]` slash command → POST → add the returned session to
  the store + switch to it (parent stays). Distinct from `/rewind`. Other tabs
  fetch the full SessionInfo on the `session_forked` event.

<a id="4-crash-safety--limits"></a>
## Crash safety / limits

A crash mid-copy retains the initializing row and any committed receipt.
Startup uses the same artifact → owned copy → recovery row ordering as
immediate compensation. An observed directory of uncertain ownership is left
for inspection. An interrupted first allocation can leave an empty private
parent chain. The session sweep has no general directory ownership discovery.

A `ready` row still awaiting delivery is admitted on startup if it has its
own durable accepted input or if its saved directory binding is confirmed just
before service admission opens. When neither can be established, startup
retains the ready row, native artifact and files and leaves that session
unavailable. The saved conversation does not prove admission on its own.
See [stored fork evidence](current-storage-format.md#json-facts-and-unknown-values).

<a id="5-tests"></a>
## Tests

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
- `test_session_duplicate.py` exercises independent Git writes, initial
  parent publication, concurrent copies and both layouts. Use
  `test_service_startup.py` for pre-admission recovery,
  `test_wiki_account_portability.py` for registered files in a fork directory,
  and `web/e2e/fork-copy.spec.ts` for the browser entry.
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

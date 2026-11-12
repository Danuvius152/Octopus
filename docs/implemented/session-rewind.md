# 会话回退：分支与安全文件还原

> **Status:** implemented — 在用户消息前分支，重放保留前缀并按选择还原可归属文件改动。

## 3.1 The harness contract

The session saga owns rewind's history-replay policy for both Claude and Codex.
`can_fork` declares that the backend supports the retained product history in
the first user input, followed by native resume of the new session. Rewind has
no provider-specific native-preparation hook: neither supported backend can
prepare a native prefix at an arbitrary product-message boundary. A full-session
[/fork](session-fork.md) instead uses Harness to copy existing native history
when available, retaining its artifact and cleanup collaborators.

## 3.5 Channel discipline

The retained transcript is first-turn user-message content inside
`<fork-history status="transcript-not-instructions">`. It is not promoted to
developer instructions. The small trusted first-turn note separately explains
the branch point and remaining real-world effects. This prevents old transcript
instructions from becoming higher-priority rules and preserves ordinary
large-input handling.

Native history lines retain original seq, root/child identity and phase; helper
status records retain partial/interruption causes. Rewind can split a native
turn at a steer, so product seq is never approximated as a native turn count.
Copied input observations remain historical evidence, not permission to repeat
uncertain operations. A later explicit user request establishes a new execution.

## 5. Behavior

### 5.1 Forking from the browser

`POST /api/sessions/{id}/fork` selects `rewind_to_msg_seq=M`, optional
`revert_files` and an optional label. The server validates that M exists and is
a user message, including M=0. The new conversation copies only `seq < M`; the
selected message is prefilled for editing and reissuing. At M=0 the copied
history is empty, but effect disclosure and optional revert still cover the
entire original conversation.

The parent must be quiescent. SessionOwner's fork reservation excludes new input
while preparation and optional file operations run. A live parent execution,
queued input, unresolved question or active delegation is not a safe snapshot.
The fork inherits agent, backend, working directory and configuration bindings;
an explicit label changes its name, otherwise it retains the parent's name.
The parent is archived after successful branch creation. A rewind shares its
working directory; full-copy `/fork` leaves the parent open and copies the tree.

The browser confirms successful creation in the session list even after the
original dialog has ended. Entering the result requires that opening's surviving
foreground intent as well as a current observation; leaving and returning does
not restore it. Dialog identity, draft protection and cross-connection confirmation
follow the [frontend list contract](frontend-list-reconciliation.md).

For an application development session, shared rewind retains its exact
application ownership in both SQLite and the installed successor. Admission
and application deletion are ordered by the insertion transaction, including
a legacy builder known only through the application's current-session pointer.
The parent reservation remains held until successor installation and is released
once by its owning operation. External archive/deletion cannot retire a parent
while preparation still uses it. Application deletion retires related successors
without changing the current builder selection or inferring ownership from cwd;
the complete lifecycle is owned by [applications](applications.md).

The retained prefix must satisfy the replay message contract before insertion;
damaged message metadata refuses creation without archiving the parent.
SQLite owns the copied-input projection, including original delivery and native
input observations. Inside the original reservation, rewind materializes this
projection, captures the parent's configuration bindings and resolves inherited
Agent settings. Harness checks that exact retained snapshot, replay wrapper and
known first-turn instructions against the selected backend's input boundary.
The selected credential contributes its stored launch representation without
refresh, login or a provider probe; expiry does not turn this size check into
authentication. Actual input retains current credential authorization checks.

This check precedes child insertion, file revert, ready publication and parent
archive. A known capacity failure returns `replay_capacity_exceeded`, releases
the reservation and leaves the parent and files usable. The copying transaction
installs the same materialized snapshot without re-reading later evidence.
Chained copies retain the original provenance. Copied observations remain
history, never executable submissions or recovery permission.
Database insertion and message copying commit with no native resume identity.
Replay metadata is saved before optional file revert and publication of the
ready child. An interrupted initialization retains its row for recovery; rewind
creates no native artifact before its first input. SQLite rollback cannot undo
the optional Git effects. The original parent's account scope and retained
attachment ownership remain enforced throughout.

### 5.3.2 History replay and first-turn state

Replay renders actual saved command records within its existing per-line limit.
New App Server head/tail records begin with an explicit notice that the middle
was not archived, so even a replay prefix retains that meaning. Replay cannot
restore the omitted middle, infer completeness for old records without capture,
or substitute for the provider's native resume history. User/assistant and
root/child source semantics are unchanged; see
[command retention](codex-backend.md#saved-command-output).

Before wrapping an input, the Owner reads the committed continuation identity,
replay flag and metadata together. The first input includes the copied transcript
and its input observations, new instruction, Wiki and attachment augmentation.
The original user text stays in product history. Codex measures the actual
`turn/start` UTF-8 JSON frame with its real identities and effective controls;
controller and instruction-injection RPCs have separate budgets. Earlier checks
can reject known oversize input, but unknown identities/defaults cannot certify
feasibility. Configuration is resolved again for each new input and the bound
RPC encodes and sends the same bytes. Oversize new input is refused with advice
to shorten it; the child and pending replay remain. History is not silently
shortened, summarized or removed to fit. Claude retains stream-JSON and its
ordinary spill-file representation; preparation creates no spill file and
does not apply the Codex frame ceiling to Claude's stdin prompt.

For a backend with a single-frame ceiling, rewind preparation includes at least
4096 encoded bytes for new input beyond the retained prefix, wrapper and known
envelope. The editable prefill is not the user's future input. Each independent
RPC is checked separately. A ready child proves the copy completed and the
known plan with this reserve passed; unresolved native defaults, including
cwd-dependent configuration layers, remain conditional. Minimum-width unknown
fields can rule out a known oversize plan, but cannot certify that resolving
them will leave all 4096 bytes. Ready does not guarantee arbitrary future text,
Wiki/attachment augmentation, changed settings or renewed credentials will fit.
Real preparation and sending therefore retain the complete payload check.

Replay is consumed only after the original Harness observes its exact sent
representation in resumable native history. Codex matches saved session, bound
start turn and complete user text. Claude matches session, original opening UUID
and complete saved user representation; a steer UUID cannot substitute. With a
spill this representation is the pointer to the retained file, not proof that
the model read its full contents. ACK, init, a context sample or a result alone
does not establish installation. This avoids relying on synthesized native
transcripts, which failed the original Claude experiments, and does not promise
the parent's native prefix cache.

The original-input transaction records the observation and continuation identity,
clears `fork_needs_replay` and retires ephemeral replay/prefill metadata together.
`fork_after_seq`, durable `full_copy`/`duplicated_from`, revert evidence and the
original business outcome survive. A failed or stopped turn can have installed
history; consumption does not wait for a successful result or Owner notification.
Definitely unsent input retains replay. Possibly sent input without installation
proof blocks later wrapping and automatic continuation, including internal retry
and restart; inspect its original receipt and effects. An already installed
original retains the ordinary continuation rules. There is no separate permanent
replay ledger. UI history and native context remain distinct stores: copying one
does not establish the other.

### 5.4 In-progress turns and races

The reservation spans snapshot, preparation and revert and is released on all
exit paths. Forks and existing requests follow the shared
[execution and retirement contract](input-reliability.md#原请求完成与失败交接).
A preliminary UI preview cannot authorize later filesystem changes; safe revert
reruns its preconditions when executed.

### 5.5 Deleting the parent

Copied message metadata can refer to original attachments. Reads follow the
same-account ancestor chain when the fork has no private copy. Before removing
a parent's attachment directory, deletion copies needed files to descendant
forks. Fork creation does not put attachment filesystem writes inside a database
transaction. Deleting a fork cleans its own artifacts, not the shared parent cwd.

### 5.6 Side effects: disclose-first, revert where safe

Rewinding changes model context; external effects remain. The classifier examines
the selected user turn and every later turn. It groups known file-edit paths,
reads live BG status through the task identities in matching tool results, and
discloses other tool activity as non-reversible. Shell write recognition is
best-effort disclosure, not an exhaustive audit of every effect. Disclosure is
not revert authority: `file_edits` can include attempted, failed or uncertain
edits, while `agent_touched_paths` contains only confirmed attributable paths.

#### 5.6.3 The one revert we offer: file edits via git

Revert requires a Git repository, a known clean tree at the selected message,
unchanged HEAD, safely parsed current status, and no dirty paths outside those
attributed to the agent. Historical tool arguments require one saved path
meaning: only absolute paths without parent traversal can grant authority.
Relative paths without a recorded base, and paths containing `..`, remain
disclosure only; resolving today's symlinks cannot reconstruct their execution.
Preflight's explicit relative arguments mean repository-relative only, never
both cwd-relative and repository-relative. Git paths identify directory entries,
including symlinks, rather than their current targets. Both rename endpoints
and unusual filenames remain visible to status parsing.
Unknown or mixed ownership refuses revert with a reason while keeping the
conversation fork usable.

The turn-start owner captures one complete NUL-delimited `git status
--porcelain=v1 --untracked-files=all --ignored=matching` observation for both
ordinary input and steer. Only a fully parsed, warning-free successful observation
can prove ordinary cleanliness. `assume-unchanged`, `skip-worktree` and other
unobservable index entries prevent a clean proof at capture and refuse current
execution; the operation never clears those flags to obtain permission.

The same observation records then-existing ignored path coverage in the private
nullable `messages.git_ignored_baseline` JSON (`version: 1`, `paths`). NULL means
unknown; a valid empty collection is complete evidence. The full UTF-8 JSON,
including its structure, must fit 262144 bytes. Exceeding that budget discards
the entire ignored-path proof while retaining independently reliable HEAD/clean
facts. This is a persistence budget, not a bound on Git traversal or output
memory. No file contents or historical ignore rules are saved. Message copying,
fork-of-fork and restart preserve the original evidence; browser messages omit
the private field. Evidence exits with the message.

Preview and execution check a baseline for every attributable path, even when
current ordinary dirty status is empty. An exact HEAD blob with mode 100644,
100755 or 120000 supplies its original file baseline, including a tracked file
that now matches ignore rules. Tree/gitlink entries and HEAD symlink/gitlink
ancestors do not supply file or absence proof. HEAD inspection uses repository
root coordinates, complete NUL parsing and exact literal path comparison.

Without a supported HEAD entry, original absence requires a clean original
observation, complete ignored coverage, current same-worktree membership and
no original coverage of the path itself or an ancestor by path components.
This permits ordinary untracked files, staged additions and rename targets;
both rename endpoints still need attribution. Original coverage continues to
block a symlink later replaced by a directory, a changed ignore rule or a
force-added file. A current ignored file without HEAD, Git metadata, nested
worktrees, directories and submodule contents cannot gain absence authority.
Any unsupported path refuses the entire operation before stash/restore, lists
the uncovered paths and reasons, and leaves the successor conversation usable.

This preflight attributes paths, not individual byte edits within a file. If
a human and the Agent both edited the same recognized path, the path comparison
alone cannot prove which hunks belong to each. More precise snapshots, preview
and same-file attribution remain in
[history and recovery](../plans/session-history-and-recovery.md); known paths
must not be described as complete per-edit provenance.

Claude edit authorization requires a non-error result paired by the original
request, attempt and canonical tool call. Its raw call ID must match and have
exactly one start in that saved attempt, including starts before the rewind
boundary. Reused IDs cannot turn an unrelated late success into edit proof.
Native `fileChange` authorization instead requires a non-error `completed` item
and its full saved producer/native association; a complete item without a start
is sufficient. Started, failed or declined patches remain disclosure only. An
MCP result containing similar JSON supplies no attribution. Matching start/result
observations count once. These checks do not authorize unrelated human edits or
claim that external effects were rolled back.

The operation stashes before restoring attributable files and records its outcome
in `fork_revert_record`. Each snapshot has a unique marker. The operation finds
exactly that stash commit, or retains the OID returned by `stash create`, and
verifies the full commit OID before saving `stash_ref` or continuing explicit
restore. Failed, missing or ambiguous lookup stops this path; it never borrows
an earlier stash. Stashing itself may already have changed the worktree, so a
failed lookup is not a rollback claim. Later stash pushes do not change the
saved OID. Legacy `stash@{n}` values remain readable but are unstable historical
selectors, not identities of today's snapshot. An OID does not guarantee object
retention after deletion or Git garbage collection.

`fork_revert_record.ran` becomes true when a mutating Git operation starts or
when preflight proves a completed no-op. It is not a success flag. A failed
stash lookup or later restore can therefore retain `ran: true`. Failure before
mutation and refusal retain `ran: false`.

Failure does not erase the successfully created fork or pretend file restoration
succeeded. This does not roll back commits, send/unsend
messages, undo database writes, cancel original BG jobs or reverse provider calls.

The shared Git helper still has an execution-retirement gap: timeout kills only
the parent without awaiting exit, and caller cancellation does not retire its
process group. Anchor capture, preview/preflight and stash/restore use that helper;
a returned timeout or cancelled call is therefore not proof that Git or its hooks
have stopped touching the tree. This limits isolation around failed operations,
not the path-ownership checks above
(see [the shared Git subprocess helper](../../server/fork_helpers.py#L80-L105)).

#### 5.6.4 First-turn context

The trusted note tells the model which effects occurred after the branch point
and whether file revert actually completed. The transcript itself remains in
the user channel. The note is framing, not a new execution authority.
It consumes the complete durable result. Refusal explains that no file revert
ran; post-stash failure and `unknown_post_crash` warn that files may already
have changed and require Git/recovery inspection. Neither is described as an
unexecuted operation or a rollback. Available snapshot identity and the actual
failure reason remain in the note.

#### 5.6.5 Persistence before first use

Prefill, effect summary and preparation identity survive a restart before the
first user turn. The durable revert record remains after ephemeral first-turn
state clears.

#### 5.6.7 Crash recovery

An `initializing` fork is cleaned using its exact saved identity; failed cleanup
preserves retry responsibility. A crash during `reverting` produces
`unknown_post_crash` and a usable ready conversation with an explicit inspection
message. Recovery does not repeat uncertain Git effects or claim the stash/revert
finished. It persists and refreshes the same unknown-result first-turn note
before use, including an already live successor. Rewind borrows the parent's directory, including when the parent is
itself a full copy under the fork base. Recovery never derives directory
ownership from that prefix. Only an initializing full-copy row with its matching
creation receipt may remove its independent directory; cleanup must finish
before deleting the recovery row. The [full-copy contract](session-fork.md#4-crash-safety--limits)
owns copying, cancellation and unproven-directory recovery.

## 8. Verification and boundaries

Code: [fork lifecycle](../../server/sessions/forking.py),
[replay/classification/revert](../../server/fork_helpers.py),
[Claude](../../server/harness/claude_code.py) and [Codex](../../server/harness/codex.py).
Regression entry points: `tests/test_session_fork.py`, `tests/test_fork_helpers.py`,
`tests/test_fork_ownership.py`, `tests/test_session_fork_installation.py`,
`tests/test_fork_native_copy.py`, `tests/test_codex_app_server_fork.py`,
`tests/test_rewind_baselines.py`, `tests/test_fork_message_snapshot.py` and
`web/e2e/fork.spec.ts`. Ownership regressions compare actual user/external file
bytes after ambiguous paths or failed edits; snapshot regressions interleave
later stashes and lookup failures without falling back to an old reference.
The baseline matrix starts at public input/steer with a real Owner, Git and
SQLite for both offline backends, and compares file bytes, persisted records,
preview and the actual successor's trusted instructions. Storage publication
and browser projection regressions cover private evidence retention/exclusion.

Mid-execution rewind, branch merge, dirty-tree snapshots and provider-specific
undo are outside the current contract. Such changes require their own design
because concurrent or externally committed effects cannot be inferred from
conversation text. Current real-runtime validation follows [testing.md](../testing.md).

The [original design and experiments](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/session-rewind.md)
preserve version-bound rationale, including the superseded synthesized-Claude-
transcript strategy. They do not establish current performance or deployment.

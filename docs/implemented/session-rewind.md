# 会话回退：分支与安全文件还原

> **Status:** implemented — 在用户消息前分支，重放保留前缀并按选择还原可归属文件改动。

<a id="31-the-harness-contract"></a>
## The harness contract

The session saga owns rewind's history-replay policy for both Claude and Codex.
`can_fork` declares that the backend supports the retained product history in
the first user input, followed by native resume of the new session. Rewind has
no provider-specific native-preparation hook: neither supported backend can
prepare a native prefix at an arbitrary product-message boundary. A full-session
[/fork](session-fork.md) instead uses Harness to copy existing native history
when available, retaining its artifact and cleanup collaborators.

<a id="35-channel-discipline"></a>
## Channel discipline

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

<a id="5-behavior"></a>
## Behavior

<a id="51-forking-from-the-browser"></a>
### Forking from the browser

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

<a id="532-history-replay-and-first-turn-state"></a>
### History replay and first-turn state

Replay renders actual saved command records within its existing per-line limit.
New App Server head/tail records begin with an explicit notice that the middle
was not archived, so even a replay prefix retains that meaning. Replay cannot
restore the omitted middle, infer completeness for old records without capture,
or substitute for the provider's native resume history. User/assistant and
root/child source semantics are unchanged; see
[command retention](codex-backend.md#saved-command-output).

Before wrapping an input, the Owner reads the committed continuation identity,
replay flag and metadata together. The first input includes the copied model
history and its input observations, new instruction, Wiki and attachment
augmentation. The last [ticket handoff](input-reliability.md#ticket-handoffs)
in the copied prefix determines its earliest model message. Replay uses the
selections through that handoff. An older excerpt without a verified position
prompts a check of the original restrictions before related work. A legacy
projection with just the original batch request can replay that request using
the copied input identity. The product copy preserves the whole selected prefix.
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

<a id="54-in-progress-turns-and-races"></a>
### In-progress turns and races

The reservation spans snapshot, preparation and revert and is released on all
exit paths. Forks and existing requests follow the shared
[execution and retirement contract](input-reliability.md#原请求完成与失败交接).
A preliminary UI preview cannot authorize later filesystem changes; safe revert
reruns its preconditions when executed.

<a id="55-deleting-the-parent"></a>
### Deleting the parent

Copied message metadata can refer to original attachments. Reads follow the
same-account ancestor chain when the fork has no private copy. Before removing
a parent's attachment directory, deletion copies needed files to descendant
forks. Fork creation does not put attachment filesystem writes inside a database
transaction. Deleting a fork cleans its own artifacts, not the shared parent cwd.

<a id="56-side-effects-disclose-first-revert-where-safe"></a>
### Side effects: disclose-first, revert where safe

Rewinding changes model context; external effects remain. The classifier examines
the selected user turn and every later turn. It groups known file-edit paths,
reads live BG status through the task identities in matching tool results, and
discloses other tool activity as non-reversible. Shell write recognition is
best-effort disclosure, not an exhaustive audit of every effect. Disclosure is
not revert authority: `file_edits` can include attempted, failed or uncertain
edits, while `agent_touched_paths` contains only confirmed attributable paths.

<a id="563-the-one-revert-we-offer-file-edits-via-git"></a>
#### The one revert we offer: file edits via git

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
verifies the full commit OID before continuing explicit restore. A successful
`stash store` establishes a recovery OID as soon as the original process has
retired. An unsuccessful object verification clears that OID. A timed-out
verification stops restore and keeps the OID when the retired process exited
successfully with complete output confirming the object in the bound
repository. Failed, missing or ambiguous lookup stops this path without
borrowing an earlier stash. Stashing itself may already have changed the
worktree before lookup failed. Later stash pushes do not change the
saved OID. Legacy `stash@{n}` values remain readable but are unstable historical
selectors, not identities of today's snapshot. An OID does not guarantee object
retention after deletion or Git garbage collection.

`fork_revert_record.ran` becomes true when a mutating Git operation starts or
when preflight proves a completed no-op. It is not a success flag. A failed
stash lookup or later restore can therefore retain `ran: true`. Failure before
mutation and refusal retain `ran: false`. A partially persisted result
omits the field when startup of a pending write is still unconfirmed.
Recovery without the original result treats that interrupted write as
potentially run and reports unknown file effects.

Failure does not erase the successfully created fork or pretend file restoration
succeeded. This does not roll back commits, send/unsend
messages, undo database writes, cancel original BG jobs or reverse provider calls.

Anchor capture, preview, preflight and stash/restore use the
[same Git execution owner](../../server/fork_git.py). It retains the pending
launch, direct process, process group and both output pipes. Git's process
handle is captured before attaching the asynchronous pipe readers so a
failed attachment still stops and joins the original process group. Leader exit and
pipe EOF are followed by a group check. A deadline or cancellation requests
termination through that owner. If retirement remains unconfirmed when the
caller leaves, the original owner keeps observing it and blocks service drain.

A rewind reserves the worktree's directory identity through the ready commit
and registration. Parent archival follows after release of that reservation.
Anchor capture and preview also use the reservation to order their Git
observations with rewind admission. A Git launch uses the verified open directory
even if its pathname changes while launch is pending. Directory identity and
Git ancestry are read from that descriptor. Preflight, anchor capture and
file operations use the reserved repository's worktree and Git directory.
Inherited Git repository selectors are cleared. Status, stash creation, file
restore and reftable operations use a private Git directory view. Its native
common locator reaches the reserved common directory through descriptors so Git can
classify nested repositories independently. Git resolves configuration in the
original repository. The view keeps primary repository format settings separate
from resolved includes. The view retains command scope and preserves valueless
settings. Child Git uses the native executable lookup order and helper scripts.
If a child Git opens another repository, including through explicit directory
options, the launcher releases the view's index and worktree overrides and
restores the original configuration environment. Filters and hooks restore the
repository's native launch environment, including the actual Git directory when
discovered through a gitfile. Shell filters receive Git's expanded command as
both their script and `$0`. The callbacks remain in the original Git process
group until retirement is confirmed.
Before entering a callback, Octopus verifies the worktree and Git directories
against the descriptors held by the original command. The hook dispatchers
look up each hook when Git invokes it, so an earlier hook can install or remove
a later one. The owner retains the existing hook directory alongside the
repository descriptors. The launcher checks the hook directory before and
after looking for the script. For an existing script, the second check follows
validation of the resolved worktree and Git directories. If lookup is denied
in a stable directory, Git's ignored-hook advice setting applies.
The launcher executes the native hook path, letting the kernel enforce execution
permissions and select the script interpreter. Only a format error
(`ENOEXEC`) falls back to Git's configured shell. Shell hooks retain their
native script path and arguments. This uses Git's ordinary path resolution
between the final identity check, kernel execution and any interpreter read.
The retirement check can detect a lasting binding change. A hook in a repository
with a gitfile or separate common directory inherits the retained common
directory, including when it calls Git by absolute path. The hook's `git`
launcher releases that override for an
explicit Git directory in another repository. An absolute invocation that
selects another repository needs to clear `GIT_COMMON_DIR` in its environment.
Filters receive the native repository environment so they can run Git in another
repository using its own discovery rules. A callback binding failure remains
attached to the command result even if the repository path is restored before
retirement.
A failed inspection keeps the Git exit and output
collection status without recording configuration values or marking a write
as started.
The view holds the complete HEAD and directs index replacement to the original
Git directory. It writes a full index to keep shared-index data out of the
temporary directory. Git's configured split-index preference remains in force
for subsequent native operations. Other commands receive the verified common
directory explicitly. Descriptors and the private view remain available until
the original process group retires. Status disables optional index writes.
A changed binding at launch stops the command. A change observed after retirement
is reported separately from the exit code and collected output. Temporary view
cleanup failures are logged independently of retirement.
For a missing session subdirectory, the nearest existing ancestor establishes
whether the session still belongs to a fenced Git worktree.
A fenced directory keeps its retirement fence across the physical ancestor chain,
including when a Git marker is absent or an inner subdirectory contains an empty
`.git` directory.
An unconfirmed Git exit stops the revert, including at the second root lookup
before any stash or restore. The candidate and its copied history stay
`reverting` and the parent stays in place. The worktree fence also applies
to rewinds with file restore disabled. Continued observation may establish
retirement and release that fence without replacing the parent. A confirmed
Git error keeps its diagnostic, file-effect record and any verified stash
identity under the ordinary revert result contract. The stored Git observation
keeps the exit code, output, timeout and confirmed retirement separate. A busy
database before the write begins leaves the original owner holding that result
for a retry of the commit.
A successful `stash store` preserves its snapshot identity when cancellation or
the binding check prevents worktree restore. A later object verification can
withdraw that identifier. The original execution's result applies this rule
even when cancellation delays settlement.

<a id="564-first-turn-context"></a>
#### First-turn context

The trusted note tells the model which effects occurred after the branch point
and whether file revert actually completed. The transcript itself remains in
the user channel. The note is framing, not a new execution authority.
It consumes the complete durable result. Refusal explains that no file revert
ran; post-stash failure and `unknown_post_crash` warn that files may already
have changed and require Git/recovery inspection. Neither is described as an
unexecuted operation or a rollback. Available snapshot identity and the actual
failure reason remain in the note.

<a id="565-persistence-before-first-use"></a>
#### Persistence before first use

Prefill, effect summary and preparation identity survive a restart before the
first user turn. The durable revert record remains after ephemeral first-turn
state clears.

<a id="567-crash-recovery"></a>
#### Crash recovery

Startup requires a successful scan of incomplete forks before the service can
become ready. Recovery cleans each `initializing` fork using its exact saved
identity. If cleanup fails, its row and copied messages remain for the next
startup to retry, and recovery continues with the other rows.

Before launching Git for a revert, the candidate records its worktree and,
when available, the identity of its managed writer. Startup preserves an
unconfirmed candidate and its copied history outside the executable session
registry, including legacy rows without writer evidence. A legacy row without
a recorded Git root fences a candidate whose root is a physical ancestor of
the legacy working directory. The parent retains
its prior state. A verified empty managed scope and a matching installation
from a different invocation can prove that the original writer retired.

After retirement is confirmed, recovery commits the result, first-turn note
and `ready` together. If the file effects are still unknown, the result
`unknown_post_crash` directs the user to inspect the worktree and stash.
Recovery uses any already recorded result and verified stash identity.
When the saved path points to a different worktree, recovery retains the
candidate's history and proof without admitting it or fencing unrelated work.
Startup, archive restoration, anchor capture, first backend input and further
rewinds check the saved directory until native input consumes the preparation
metadata.
A restore with a pending marker and a mismatched directory returns to the
archive at startup. A committed history whose bound directory is unavailable
can be read and retried through the archive views. The same binding still
needs to match before the history can run. Deletion confirmation reports
the same archive state as those views. An in-progress application deletion
keeps its own retirement and recovery responsibility.
Failure to commit a covered candidate's result blocks startup. No recovery
path repeats stash or restore.

Rewind borrows the parent's directory, including when the parent is itself a
full copy under the fork base. Recovery never derives directory
ownership from that prefix. Only an initializing full-copy row with its matching
creation receipt may remove its independent directory; cleanup must finish
before deleting the recovery row. The [full-copy contract](session-fork.md#crash-safety--limits)
owns copying, cancellation and unproven-directory recovery.

<a id="8-verification-and-boundaries"></a>
## Verification and boundaries

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

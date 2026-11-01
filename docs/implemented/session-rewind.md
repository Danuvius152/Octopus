# Session rewind: conversation branching and safe file revert

> **Status:** implemented — Rewind before a user message, replay the retained prefix and optionally revert attributable file changes.

## 3.1 The harness contract

`Harness.prepare_fork` returns a native resume handle and/or `needs_replay`.
Current Claude and Codex rewind profiles use history replay; a full-session
[/fork](session-fork.md) instead copies an existing native transcript when available.
Callers use the capability and returned artifact, not a backend-name branch.

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

Database insertion and message copying commit before external transcript/file
operations. Preparation failure compensates its owned artifacts before removing
the row; failed cleanup retains the initializing identity for recovery. SQLite
rollback cannot undo filesystem effects. The original parent's account scope
and retained attachment ownership remain enforced throughout.

### 5.3.2 History replay and first-turn state

The first input carries the retained transcript and the user's new instruction.
After the native backend supplies its own session/thread identity, later turns
resume that identity. Replay has a heavier first-input cost and does not promise
the original native prefix cache. It avoids relying on externally synthesized
native transcripts, which failed the original Claude experiments.

`fork_after_seq` persists the copy/replay cutoff. Ephemeral replay and prefill
metadata clear after the first result; durable `full_copy` and revert evidence
survive. UI messages and native context are distinct stores: copying one does
not prove that the other has been prepared.

### 5.4 In-progress turns and races

The reservation spans snapshot, preparation and revert and is released on all
exit paths. Forks and existing requests follow the shared
[execution and retirement contract](architecture-contract.md#输入完成与资源退休).
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
best-effort disclosure, not an exhaustive audit of every effect.

#### 5.6.3 The one revert we offer: file edits via git

Revert requires a Git repository, a known clean tree at the selected message,
unchanged HEAD, safely parsed current status, and no dirty paths outside those
attributed to the agent. Paths are normalized against cwd and repository root;
rename source/destination and unusual filenames cannot disappear in parsing.
Unknown or mixed ownership refuses revert with a reason while keeping the
conversation fork usable.

Explicit native `fileChange` paths feed this same preflight, including both
rename endpoints and a complete item with no preceding start. The Harness
decoder checks the saved producer kind and original native item association;
an MCP result containing similar JSON supplies no file attribution. Matching
start/result observations count once. Known patch paths do not authorize
reverting unrelated human edits or imply that external effects were rolled back.

The operation stashes before restoring attributable files and records its outcome
in `fork_revert_record`. Failure does not erase the successfully created fork or
pretend file restoration succeeded. This does not roll back commits, send/unsend
messages, undo database writes, cancel original BG jobs or reverse provider calls.

#### 5.6.4 First-turn context

The trusted note tells the model which effects occurred after the branch point
and whether file revert actually completed. The transcript itself remains in
the user channel. The note is framing, not a new execution authority.

#### 5.6.5 Persistence before first use

Prefill, effect summary and preparation identity survive a restart before the
first user turn. The durable revert record remains after ephemeral first-turn
state clears.

#### 5.6.7 Crash recovery

An `initializing` fork is cleaned using its exact saved identity; failed cleanup
preserves retry responsibility. A crash during `reverting` produces
`unknown_post_crash` and a usable ready conversation with an explicit inspection
message. Recovery does not repeat uncertain Git effects or claim the stash/revert
finished. Full-copy cleanup removes only its recognized private destination.

## 8. Verification and boundaries

Code: [fork lifecycle](../../server/sessions/forking.py),
[replay/classification/revert](../../server/fork_helpers.py),
[Claude](../../server/harness/claude_code.py) and [Codex](../../server/harness/codex.py).
Regression entry points: `tests/test_session_fork.py`, `tests/test_fork_helpers.py`,
`tests/test_fork_ownership.py`, `tests/test_session_fork_installation.py`,
`tests/test_fork_native_copy.py` and `web/e2e/fork.spec.ts`.

Mid-execution rewind, branch merge, dirty-tree snapshots and provider-specific
undo are outside the current contract. Such changes require their own design
because concurrent or externally committed effects cannot be inferred from
conversation text. Current real-runtime validation follows [testing.md](../testing.md).

The [original design and experiments](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/session-rewind.md)
preserve version-bound rationale, including the superseded synthesized-Claude-
transcript strategy. They do not establish current performance or deployment.

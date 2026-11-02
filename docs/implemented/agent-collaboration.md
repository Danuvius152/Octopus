# 命名 Agent 委派：会话与工作轮次

> **Status:** implemented — 同账户异步协作、原回执完成、独立返回诊断与精确轮次取消。

## 1. Invocation and identity

`mcp__ask_agent__ask` accepts exactly one of `name` (new child conversation) and
`delegation_id` (new round in the retained child). It returns immediately; results,
questions and errors return to the caller in later inputs. `answer`, `cancel`
and `list` operate through the parent-scoped delegation API.

The target is a non-archived agent in the same account. Missing/ambiguous names,
self-delegation, an ancestor-agent cycle or depth beyond three are refused.
An unavailable ancestor cannot be assumed safe: the walk uses retained database
identity and rejects missing/cyclic ancestry. The child inherits the parent's cwd
and receives the request plus optional resolved file paths. It does not receive
the parent's transcript. Its persona, credential and memory belong to the target
agent. MCP defaults apply only when creating agents; saved tool selections remain.

Conversation identity and round identity are different: the child session is the
`delegation_id`; every round has a new stable `work_id`. Follow-up requires a
terminal preceding round. It can explicitly restore archived history but cannot
reuse a hard-deleted child. Concurrent independent requests use separate children.
The two modes share one tool to avoid a redundant model-facing interface.

### 5.2 Sub-session lifecycle

A delegation child session keeps the ordinary session retention policy:

- **A terminal round leaves the conversation open.** Success, failure,
  cancellation and reply delivery do not archive the child. Restart restores
  the conversation and classifies its old round without replaying its reply
  or running the child. Humans can open it from the delegation card and submit
  a new ordinary request; that request does not resend the old caller's reply.
  An explicit parent follow-up creates a new round/work in the same conversation.
  Explicit Archive and the schedule-only idle policy still retire resources
  through SessionOwner. Setup failures before admission retain their cleanup
  policy. Already archived history stays read-only until explicitly continued;
  startup does not mass-unarchive it. Sidebar delegation filters still apply.
- **A delegation with a sub-delegation in flight does not finalise.**
  The load-bearing half of the note above. `ask_agent` is asynchronous, so
  Vera's turn-1 `result` arrives the instant she has asked Pete — it is not
  her answer. The original receipt's completion classification therefore skips terminal
  injection while `_has_running_children(sid)` holds **or** the record's
  `_awaiting_sub_relay` flag is set, and finalises on the next non-error
  result (the turn Pete's reply wakes her for). Without this Octo received
  "awaiting Pete's response" as Vera's reply and her session was archived
  mid-chain, so the relay could never land — the 3-hop backend test could not
  pass and had been skipping itself. An **error** result still finalises
  immediately, so a crashed middle agent can't hang its caller.

  The flag exists because `_has_running_children` samples the registry at the
  instant the turn ends, and that instant is a race: if Pete is quick, or
  Vera's own wrap-up is slow, Pete is already `completed` when Vera's turn-1
  `result` lands and nothing looks in flight — so she finalised with "I've
  asked Pete" as her answer and Pete's real reply, already queued into her
  session, was dropped on a record that was no longer `running`. What decides
  the deferral is that she *asked* someone during the turn, not whether they
  are still working when it ends, so `start_delegation` marks the caller's own
  record and completion classification consumes the mark on the deferred turn (a relay
  turn that asks again re-arms it). The real-CLI 3-hop test only caught this
  on runs slow enough to invert the two events; `test_delegations.py` now pins
  both orderings, plus the one-hop case that must still finalise on its first
  result.

Otherwise: same harness, same MCP set, same credentials, same memory
dir as Vera's other sessions. Memory writes from concurrent
delegations to the same agent share the agent's memory dir — see
[concurrency and rationale](#concurrency-rationale-and-verification).
The split is reflected in `SessionManager`:
`_AUTO_ARCHIVE_ORIGINS` is only `("schedule",)`, while
`_AUTO_ARCHIVE_ELIGIBLE` includes `"delegation"` for explicit
post-terminal archival (`server/session_manager.py:416`).

### 5.3 Reply delivery: the original request completion

Each delegation round is a `DelegationRunState` with a stable `work_id`,
parent/target identities and a confirmed round state. A follow-up creates a
new round record in the same conversation; an old cancellation or completion
continues to refer to its captured original record.

`_watch_completion()` observes the exact `SubmissionReceipt` for initial,
follow-up, question and terminal-relay inputs. `_complete_request()` consumes
its one completion through the existing round CAS/coordination path. Nested
classification happens before any awaited reply-text SELECT, so a slow
initial read cannot let a later relay redefine the initial turn as a reply.
The callback-to-command handoff is owned by `confirm_recording()` without
making it wait for unrelated requests still running.

A confirmed success selects text from the exact committed request and its
final attempt, then submits `[agent-reply:<name> delegation=<id>]` to the
parent. No second `captured_text` copy is maintained. Failure, cancellation
or interrupted classification produces the corresponding `[agent-error:…]`.
Stable `agent-terminal:{work_id}` admission identity, persisted
`_terminal_disposition` and the in-flight guard prevent duplicate delivery.

Terminal and question returns use the session's complete
[`admit_return` operation](architecture-contract.md#公共返回接管). The source keeps
its original work/delivery identity, reentry guard, round CAS or child notice
writer, retry policy and completion observation. Pure preparation reads the
exact request/final-attempt reply or revalidates the original round and pending
question; it neither restores the parent nor writes diagnostics or starts work.
An existing receipt bypasses material reads even after the child is deleted or
the parent Owner fails. Without a receipt, a deleted child produces the existing
`reply_unavailable` error while keeping the child's successful outcome; a retained
child missing its exact terminal remains an integrity error. A confirmed empty
reply retains the existing empty-text placeholder.

Request/carrier/terminal/attempt association and committed body selection belong
to the shared [session result reader](architecture-contract.md#原请求结果读取).
Delegation alone owns continuous concatenation/strip, its empty-answer text and
agent-error delivery meaning; it does not maintain another association rule.

Permanent integrity refusal records `stopped`. After a lost acknowledgment,
timeout or unavailable channel, the common operation checks the original receipt
once. If admission remains unconfirmed, the source records `unknown` on its
captured control before awaiting the diagnostic write, then ends this observation.
`terminal_delivery_error` stays independent of the child's business `error`.
Only existing settlement reentry can observe again through the same operation,
with preparation returning a skip; a later refusal cannot erase the original
uncertainty. There is no extra source-side lookup, polling task, return
resubmission or child rerun. Only proven pre-effect busy/capacity/quota refusals
without an unknown history retry, under the original identity.

An observed disposition remains in the original control object if recording
fails, so reentry can finish that recording without submitting again. Conflicts
and programming errors retain their original exceptions; cancellation propagates,
and authoritative storage uncertainty uses the existing fail-stop boundary.
Source business success, return admission and parent completion remain distinct.
REST/MCP list results, `delegation_delivery` events and the exact-round card
expose disposition/error separately; the card calls the business state
`completed`, without claiming that the parent has processed the reply. A late
list snapshot cannot overwrite a delivery event received after its fetch began.
Restart retains diagnostics but never replays old replies; old empty dispositions
still recover as `not_replayed`. A follow-up owns a new work identity and fresh
question delivery controls.

The existing command registry retains explicit checks for those diagnostic
writes. Neither a domain-error type nor a finished coroutine proves that its
write is complete. An already occurring relay/settlement reentry registers its
new command before taking over the same original write; the old failed command
can then retire, while the new owner still blocks drain until the write is
confirmed. Round CAS and child question notices have separate recording
targets: losing the parent relationship closes only the round CAS writer.
Deleting the original child closes both writers through the verified deletion
path. A follow-up closes the preceding
writer only when its own command has not confirmed rollback; its staging CAS
alone is reversible. A confirmed refusal that restores the preceding round
preserves its unfinished diagnostic responsibility even if another command
temporarily stages a new round during cleanup. No new return ledger,
polling task or alternate writer is introduced. Full masked exception chains
remain in the existing logs even if a later receipt clears the UI observation.

Business outcome and notification delivery are independent. A failed child
notification prefix cannot erase its acknowledged success or leave its
delegation permanently running. A writer ending without an acknowledged
terminal yields unavailable; it does not fabricate a success/failure terminal
or replay the input. Public deletion between classification and reply reading
retains the original successful result. The same stable relay delivers an
explicit `reply_unavailable` error to the still-open caller, so a nested parent
can finish without invented reply text or child replay. Normal deletion does
not turn command/database drain into an invariant failure. The database's
session-deletion entry notifies the round owner synchronously after confirmed
commit. Parent deletion leaves the independent child writable: its pending
notice failures still block drain until recorded or their own target retires,
and new valid questions retain a stopped-return notice and remain answerable.
A refused deletion or an unrelated session deletion closes neither writer.
This retires only finished diagnostic commands; in-flight
callbacks and processes retain their original retirement owners.

The broadcast listener only forwards `question_request` for the captured
work and retires that question's relay observation on answer/close once its
required diagnostic recording is settled.
`assistant_text`, `result` and `error` are no longer parallel delegation
completion writers. Progress and history remain visible through the normal
session surfaces.

### 5.5 `mcp__ask_agent__answer` — closing the loop

The answer tool on the `ask_agent` server posts to
`POST /api/sessions/{parent_sid}/delegations/{child_sid}/answer` with
`question_id` and complete `answers`: an ordered list, or a mapping keyed by
question ID or zero-based index. Each entry contains `selected` labels (all
choices for a multi-select question) or `text` for a free-text answer. The
legacy `choice` argument remains supported for a single-question batch.

Server-side: `DelegationManager.answer_pending_question` drains the
oldest pending entry on the child by calling the same
`SessionManager.answer_question` path used by the UI. The model is the
parent agent; the websocket UI is unaware. Both producers (human
click, parent tool call) compete to drain the same queue; first one
wins. If both happen, the second one 409s. An incomplete batch is rejected
without closing the pending questions; other questions are never padded with
empty answers. Passing the batch identity prevents a delayed answer from
consuming a later question.

The original Question Owner owns the shared
[validation and resolution contract](architecture-contract.md#问题结算).
This entry converts question IDs/positions and retains its historical text strip;
it does not clear malformed selection types before validation. Authentication
supplies account/agent provenance, and the actual delegation supplies the parent
relationship. Saved answer structure and the live event agree; a parent agent's
answer is not labelled as a human answer.

If the parent's model decides it can't answer, it has three options
expressible in its normal language/tool loop:

- Call its **own** `ask` tool to ask its caller (recursing one hop
  up). When the answer comes back, call `mcp__ask_agent__answer` with
  it. This is the "I don't know — let me ask the user" path.
- Call `mcp__ask_agent__cancel(delegation_id, reason="…")`. The child gets
  an exception in its `ask` call and finalises via the error path.
- The user can open the child session and answer through the ordinary
  question UI; the same question identity and timeout rules apply.

Each original work/question owns a live relay control, with an in-flight guard
and independent stopped/unknown observation before any diagnostic await. A
duplicate event cannot submit during that await or after an unconfirmed return.
The original receipt takes priority; only a definite pre-effect capacity refusal
permits another submission, with the question revalidated before it. Permanent
refusal or unknown is explained in the original child's existing delivery notice,
identified by work/question, so it does not depend on the refused parent
transcript. The original question remains answerable while pending. A child
notice failure remains a failure with its original owner/exception; it neither
recreates the question nor reopens return submission. Ending a question discards
its live relay control only after required diagnostic recording has settled;
an unfinished original write keeps its owner reference. Restart does not resurrect callbacks or a question
queue. Closing a question after a lost ACK cannot undo any already admitted
parent input; delayed answers still name that original batch.
If the question closes during parent restoration or while queued, the admitted
input retains that batch identity. The original Owner expires it before starting
an answer model turn. Parent Stop remains paused through restoration and new
returns; human correction does not resume the backlog, and explicit Resume
continues the successful admission order.

### 5.6 `mcp__ask_agent__cancel` and `mcp__ask_agent__list`

`mcp__ask_agent__cancel(delegation_id, reason?)` →
`POST /api/sessions/{parent_sid}/delegations/{child_sid}/cancel`.
The owned cancel command captures the selected round and its original work
branch and synchronously closes every captured work and run lease before the
first await. Saving an ancestor therefore cannot leave a middle descendant
authorized to create more work. It then CAS-updates running rounds to cancelled
and retires their captured session, BG and Research
resources before reporting cancellation. A completed answer stays completed
while its residual work retires; a later follow-up's independent work is not
cancelled. Cancelling the HTTP waiter does not withdraw this owned command.
An ordinary relationship/CAS refusal is reported after all captured resources
are cleaned; physical storage uncertainty retains the fail-stop boundary.
Stable work identity and terminal disposition protect one parent injection.

Setup already in flight rechecks its original caller after session creation and
round registration, retiring or rolling back unadmitted work when revoked.
Closed authority never short-circuits original resource lookup. If revocation
aborts an original Harness spawn before CloseWork reaches its SessionOwner,
that Owner uses its existing interruption and retirement path; an expected
cancelled child start does not cancel the Owner itself. Deterministic save/spawn
barriers in `tests/test_delegation_cancel_conflict.py` and the work/command
regressions retain real subprocess exit, CAS refusal and cancelled-waiter evidence.

The parent gets an injected
`[agent-error:vera delegation=… reason=cancelled]`. Descendant
terminal turns are delivered to their immediate parents before the
root cancellation is injected upward, so the one-hop invariant remains
true while the chain unwinds.

`mcp__ask_agent__list()` → `GET /api/sessions/{parent_sid}/delegations`,
returns the live + recently-finished `DelegationRunState` records for
this parent, capped at 25, most recent first. Symmetric with
`mcp__bg__list`.

## Concurrency, rationale and verification

Delegation uses ordinary sessions for transcript, native resume and target-agent
configuration, with a separately persisted round outcome and exact original-work
retirement. No automatic transcript copying or implicit per-agent serialization
is added. Concurrent sessions share that agent's Markdown memory and upstream
credential; file edits must coordinate, and upstream limits are not multiplied.

Question answers must name the original batch and supply complete answers. Child
questions travel one hop to the caller, which can answer or ask its own caller.
Legacy tool-approval records are history, not a second approval channel.

Source: [DelegationManager](../../server/delegations.py),
[routes](../../server/routers/delegations.py), [tool](../../server/mcp_servers/ask_agent.py).
Regression entry points: `tests/test_delegations.py` and the shared
[completion/retirement checks](../testing.md).
Native subagents have a separate [observation contract](native-subagents.md).
Per-agent delegation allowlists and automatic memory-write locking are outside
the current contract; adding them requires a concrete policy or concurrency need.

## Historical evidence

The [original design and evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/agent-collaboration.md) retain their original version and conditions. Current verification follows [testing.md](../testing.md); this document does not establish production deployment.

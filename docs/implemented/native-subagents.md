# 原生子任务：观测、卡片与详情

> **Status:** implemented — 原生 helper 观测、App Server 工作卡片与只读已保存详情，含原资源寿命内晚事件。

The implemented scope is observation and presentation, including Claude
held-process late events and ordinary Codex App Server capture through bounded
retirement. A card does not prove that the parent consumed a child result or
that a delegation receives a later parent report. Codex root success may coexist
with an unfinished helper retired as interrupted. Arbitrary post-final recursive
continuation remains a separate, unfulfilled capability goal in
the [paused execution scope](../plans/runtime-execution-scope.md).

## 1. Observation sources

The Claude parser normalizes `system/task_*` events; Codex exec normalizes
`collab_tool_call` items. The App Server driver observes original native
thread/turn/item events and collaboration tools. Progress, completion and naming have different native
shapes, so the common event carries observations without imposing one provider's
lifetime on another. The initial protocol probes used Claude 2.1.274 and Codex
0.132.0; their timings and advertised feature names are historical evidence,
not a current CLI inventory. Supported parsing is defined in the profiles and
parser regression cases below.

## 2. A sub-agent is not a delegation

Octopus already has agent-to-agent work: a **delegation** is a real session,
owned by another agent, that outlives the turn and can be reopened, answered
and continued (`agent-collaboration.md`). A native sub-agent is managed inside
the provider runtime and has its own native identity. Its lifetime need not end
with the parent's first turn; §7 describes Claude's asynchronous case. This
observation model does not itself determine the parent request's completion.

It remains an **observation**, without a separate runnable Octopus session.
Legacy cards attach to the originating tool call. App Server cards retain their
original request/attempt, child thread, actual work turn, creation parent and
triggering tool provenance where available.

## 3. One shape for both harnesses

[`SubagentUpdate`](../../server/harness/events.py) is the current schema carried
by `HarnessEvent(type="subagent")`. It retains native task/tool identity,
canonical `tool_call_id`, status and stop reason, prompt/summary, optional usage
and accumulated progress steps. Partial updates merge with the prior observation
rather than clearing omitted fields. Status distinguishes running, completed,
failed, cancelled, interrupted and unknown.

For App Server, `card_id` distinguishes work rounds and multiple receivers of
one tool call. Resource creation can supply a pending observation before the
actual child turn is known; a subsequent actual turn binds that card. Later turns
of the same child have separate cards. A `started` tool item's completion proves
the start action was recorded, not that the child finished. `interacted` can
represent either a mailbox-only message or a triggering follow-up; it never
creates a work round without an actual child turn. A wait retains the original
work it observed, so its late response cannot complete a newer turn or rewrite
the child's creation parent. None of these facts proves parent consumption.

Binding updates that same pending work object in place: its card identity stays
stable while its actual turn and source provenance become known. A captured wait
therefore converges with direct child-terminal evidence on the original work,
including a wait that finished before binding. A second turn remains a distinct
work; neither a late old wait nor duplicate terminal evidence completes it. The
existing single output writer owns these updates, without a second completion
ledger or lock.

The Claude parser keeps a `task_id → (tool_use_id, name)` map for the life of
the run, because `task_updated` arrives with only a `task_id` — the one event
that can't identify itself.

## 4. Progress is broadcast, state is remembered

Legacy progress events are **not persisted**, for the same reason token deltas
aren't: they're a live view of something whose durable record already exists
(the `Task` tool call and its result). Persisting them would double every
sub-agent in the transcript.

The session keeps the last observation per card identity in memory, bounded to
64 cards with oldest-first eviction, and the session snapshot
(`GET /api/sessions/{id}`) carries them. Legacy cards use canonical
`tool_call_id`; App Server cards use `card_id` and typed native provenance.
A server restart loses this live cache. A missing card or server restart does not
prove native success; request completion and resource retirement have their own
[contract](input-reliability.md#原请求完成与失败交接).

App Server status observations are durable `native_subagent` messages. Complete
child text and tools retain their original transcript rows, with the body stored
once. Status rows do not copy prompt/summary bodies. Typed `runtime_metadata`
carries request/attempt plus native thread/turn/item, root association, known
parent and phase; turn observations have no invented item ID. A tool's optional
`tool_call_id` remains independent provenance: plain native text does not acquire
one. Tool consumers still require valid tool provenance.

Backend coalescing, tool indexing, WS and browser partials use the same scoped
item identity. A complete item clears only its own partial. Snapshot reconciliation
preserves native partials updated after the read began; an old snapshot cannot
erase them merely because no complete row has advanced seq yet. Service-generation
changes clear the previous connection's partials. Legacy records without native
metadata retain their old interpretation; no ancestry is inferred from text.

## 5. The card

Legacy cards are **keyed on the spawning tool call's id, never on its name**. The name
is not stable: this CLI version advertises `Task` in its init event and then
emits the call as `Agent`, and Codex's is `subagent:<tool>`. A card that
matched names would have quietly stopped rendering on a CLI upgrade — the
worst failure mode available, because everything else would keep working.
Every tool call mounts a card; the ones with no sub-agent behind them render
nothing.

App Server renders one card for each observed child work round. Only the display
projection hides child text from main chat; raw transcript rows, original seq,
global high water and tool audit remain intact. Cards rebuild their summary from
the collected child text with matching request/attempt/thread/turn. Historical
pagination and realtime output use the same association. If a window has child
text or tools but lacks the older status row, its card remains unconfirmed. A typed live
snapshot can supply a missing display anchor without inventing a stored seq;
loading the original row deduplicates that anchor.

Legacy cards render where the spawning tool call sits in the transcript: the sub-agent's name, what it's doing right now, its token and
tool counts as they climb, elapsed time, and — when it lands — the summary,
collapsed to a few lines with the full text one click away. Failure says so
with its reason rather than a chip that simply stops moving.

### Saved App Server details

Expanding a native card reads the collected body and tool items through
`GET /api/sessions/{session_id}/native-details/{anchor_seq}`. The client sends
an original product message sequence, never an arbitrary native thread ID.
Browser reads explicitly select `view=browser`; message previews and full-item
GETs follow the [browser representation contract](frontend-list-reconciliation.md#浏览器有界预览与按需全文).
The byte-limited page retains the earliest contiguous matching items and uses
the last actually returned seq as its forward cursor. An individual saved item
can be expanded separately; these bodies stay local to that detail view.
If an item's original association could not fit the preview, expansion displays
the full readonly saved record, including runtime metadata, without changing the
live helper or restoring an input form.
Within one read transaction, the server checks the exact account/session,
resolves request/attempt/root and child thread/turn, and reads only matching
saved rows. Archived sessions use the same reader. A spawn observation can
precede its child turn; resolution follows that same card's saved binding,
without falling back to all turns on the child thread. Conflicting identities
or duplicate sequences fail the query. A window with only child body or tools
can supply its own real saved reference. A snapshot alone cannot manufacture one.

Each page rechecks ownership and its original target. `after_seq` must belong
to that precise child work. The UI keeps pages separately from main chat and
deduplicates by saved seq, so a tool call and its result sharing one native
item both survive. Refresh, target/account/service changes, close/reopen and
disconnect/reconnect invalidate pending reads, including a reconnect to the
same service. Detail reads do not advance chat high water, alter its display
cache, add usage or change saved messages.

Available items, uncollected history and query errors are distinct. Saved
partial output remains readable with the helper's actual status and stop
reason; neither helper completion nor a readable body proves parent consumption.
Large live prompt/summary hints are marked as truncated. They are not saved in
the native card row, so its seq must not promise a full-message read of those
omitted hints. Only collected child items have saved body references; an
uncollected helper remains explicitly unavailable without starting a host.
Malformed saved content is a query failure, not uncollected history. Necessary
complete-item recording failures still follow the original execution failure
contract, rather than being excused as unavailable optional details.

The endpoint only reads the original messages; there is no second body store,
native history host or optional live-host supplement. Opening, refreshing or
paging the panel cannot execute a model, send child input, extend a lease or
idle budget, or hold retirement. It does not recover uncaptured history.

## 6. Agents that bring their own sub-agents

`--agents '<json>'` registers session-scoped sub-agents on the Claude CLI:
`{"reviewer": {"description": "…", "prompt": "…", "tools": […], "model": "…"}}`.
The saved agent `subagents` configuration is rendered by the Claude profile.
An empty configuration leaves the CLI built-ins available; saved user definitions
are not rewritten by startup.

The current Octopus Codex profile does not render these saved custom
definitions. This describes the integration, not every available native feature.
The remaining mapping and policy guarantees belong to the
[custom-role plan](../plans/codex-native-roles.md).

## 7. Work that outlives its turn

Claude Code decides on its own to run some sub-agents **asynchronously**. The
tool returns `Async agent launched successfully` immediately, the parent turn
ends, and the sub-agent keeps working inside the held process. When it lands,
the CLI wakes the agent to report it — a whole turn's worth of text nobody
asked for at that moment.

Octopus used to drop every one of those events. `HarnessRun` closes its stream
at the turn's `result`, and everything after that was discarded, so:

* the card spun forever, because the sub-agent's completion never arrived;
* **the answer never arrived either** — the agent's follow-up report went to
  the floor;
* and if the user happened to send the next message first, the async
  sub-agent's events landed in *that* turn's stream instead, attributed to
  whatever was open.

So a held run now has an **idle handler**: events that arrive with no turn in
flight go to the session rather than nowhere. They're handled exactly like
in-turn events minus the turn machinery — persisted if they map to a message,
broadcast either way.

Session status deliberately stays `idle` through all of this: there is no
`_active_task` to interrupt, and showing "running" would offer a stop button
that stops nothing.

Token deltas are the one thing the idle handler drops. A delta is a live view
of a block that is about to arrive complete; out of turn there is nothing live
to view, and a trailing delta repaints a stale partial on top of an answer the
UI has already painted. (Found by an e2e run under load, where a short reply's
completed block beat its own deltas.)

The other half of the same problem: a sub-agent lives *inside* the CLI
process, so when the idle reaper stops it (or a restart does), anything still
marked running is over. Releasing a process closes out its runs — a card that
keeps spinning after its process is gone is a lie.

This is the same hole native cron ticks fall through, and the same fix covers
them.

<a id="opt-in-app-server-capture"></a>
### App Server capture

App Server has no held-process idle execution path. Root terminal or Stop revokes
execution and question admission immediately, while the original attempt keeps
its collection identity through finite reader/proof cleanup. Late complete items,
tool results and denied approvals remain associated with that attempt. This
does not renew tool grants or permit another root input.

After the original process group and readers have retired, unfinished observed
helpers receive interrupted with the recorded reason, including parent_done or
user_stop. Confirmed completed children retain their actual outcome. EOF or an
interrupt ACK alone cannot produce this synthetic ending. The observation map is
bounded independently of the 64-card display cache; evicting card 65 cannot erase
items or resource responsibility.

On output failure, the stream retains its accepted prefix and then reconciles
known final observations from that same bounded map before its reliable error.
This includes real child terminals withheld by a full queue; only unfinished
work becomes interrupted after proven retirement. Missing exit proof instead
retains the original failure/resource responsibility and does not manufacture
helper interruption. A card cannot remain running merely because its final
event encountered output pressure. Root success remains a separate fact and is
not presented as proof that all child work was understood or summarized.

Repeated child started/completed notifications do not refresh the active root's
idle budget. Once execution admission closes, late child items and states retain
their original association without extending cleanup. The first cancellation
cause, including parent_timeout, survives a later native interrupted terminal.
Child bodies and helper status remain separate from the root's selected reply;
the [result contract](input-reliability.md#原请求结果读取) owns that selection.

## 8. Cost

The card displays the usage reported by the native subagent events. It does not
create a second billing record or add those counters to a parent result again.
Usage interpretation depends on the actual provider event; Codex timing and
aggregation follow the [Monitor contract](codex-performance-monitor.md).

## 9. Testing

Source owners: [native event schema](../../server/harness/events.py),
[session observations](../../server/sessions/events.py),
[resource retirement](../../server/sessions/processes.py), the Claude/Codex
profiles and `SubagentCard`. Current regression entry points are
`tests/test_subagents.py`, `tests/test_subagents_real.py` and the UI card tests.
Live provider selection follows [testing.md](../testing.md); the presence of a
Claude test is not evidence of current local Claude validation.

App Server regressions in `tests/test_codex_app_server_output.py` and
`tests/test_codex_app_server_questions.py` cover multi-receiver/multi-turn
identities, stale waits, mailbox-only interaction, late items, real process-group
retirement, cache eviction and output overflow before/after child terminal.
The pending → wait-start → child-bind → wait-complete regression uses complete
framed stdio through Run, Owner and SQLite, without a direct child terminal; it
checks the same saved card, child provenance and the unchanged selected root
answer. `tests/test_native_work_identity.py` adds old-wait/new-turn isolation and
duplicate-evidence boundaries. These controlled cases prove the internal identity
invariant, not reachability of this ordering in the pinned live provider; the
[version reference](../reference/codex-app-server-g0.md#pending-child-work-identity-evidence)
keeps that evidence boundary.
The nested fixture replays selected original G0-Base frames; it is not a fresh
live-provider run. Frontend source-adjacent tests and
`web/e2e/native-output.spec.ts` cover partial interleaving, sparse windows,
reconnect and refresh; the browser fixture does not establish native I/O semantics.

`tests/test_native_child_details.py` covers delayed spawn binding, distinct
rounds, tools and paging, account/refusal/integrity boundaries, archived reads,
and pending detail delivery across actual Stop retirement. Component tests
exercise stale initial/next pages on refresh, target changes and same-service
reconnect. The browser case opens saved details after completion and reload,
with an execution audit proving that viewing makes no model request.


Parser snapshots for both harnesses (every event, including the anonymous
`task_updated`), the session-manager path (broadcast-only, snapshot-carried,
bounded), the out-of-turn path (§7: post-turn events persisted and broadcast,
runs closed out when the process goes, an event for a deleted session
ignored), `--agents` argv rendering, the card's states in the UI, and
real-CLI tests that drive an actual `Task` through the harness, register a
sub-agent an Octopus agent defined, and — the regression that prompted §7 —
let an **asynchronous** sub-agent finish after its turn has ended, asserting
both its completion and the agent's follow-up report survive.

## 10. Limits

* **Custom sub-agent definitions for Codex** are not rendered by the current
  profile. A transport change must verify its own supported native surface.
* **Opening a sub-agent's full native transcript.** Collected App Server child
  body and tools are readable in its saved details. Uncollected transcript
  history remains unavailable through this interface.
  Claude writes one under the
  session's `tasks/` dir and Codex's child threads are real threads on disk;
  reading them is a file-tailing feature, not an event-stream one.
* **Steering a running sub-agent** (Codex's `send_input`, Claude's
  backgrounded tasks). The model can already do it from inside the turn; a
  human doing it from the UI needs a target selector and a policy for what
  happens when the parent turn ends first.

## Historical evidence

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/native-subagents.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.

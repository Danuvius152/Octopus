# Native sub-agents, surfaced

> **Status:** implemented — The CLIs' own sub-agents surfaced as one event and one card — including the ones that finish after the turn that started them.

The implemented scope is observation and presentation,
including Claude held-process late events. It does not prove that a parent
receipt waits for its native descendants or that a delegation receives a later
parent report. Current Codex exec may retire an unfinished helper after parent
success; Claude's idle handler preserves late messages without reopening the
receipt. The selected [runtime roadmap](../plans/engineering-runtime-roadmap.md#codex-native-tail)
requires complete managed native tails for the Codex migration and permits a
shared lifecycle refactor. That migration has not been implemented.

## 1. Observation sources

The Claude parser normalizes `system/task_*` events; the Codex parser normalizes
`collab_tool_call` items. Progress, completion and naming have different native
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

So it gets its own vocabulary rather than being forced into sessions: an
**observation** attached to the parent tool call, with no row of its own.

## 3. One shape for both harnesses

[`SubagentUpdate`](../../server/harness/events.py) is the current schema carried
by `HarnessEvent(type="subagent")`. It retains native task/tool identity,
canonical `tool_call_id`, status and stop reason, prompt/summary, optional usage
and accumulated progress steps. Partial updates merge with the prior observation
rather than clearing omitted fields. Status distinguishes running, completed,
failed, cancelled, interrupted and unknown.

The Claude parser keeps a `task_id → (tool_use_id, name)` map for the life of
the run, because `task_updated` arrives with only a `task_id` — the one event
that can't identify itself.

## 4. Progress is broadcast, state is remembered

Progress events are **not persisted**, for the same reason token deltas
aren't: they're a live view of something whose durable record already exists
(the `Task` tool call and its result). Persisting them would double every
sub-agent in the transcript.

But a browser reload mid-run must not blank the card, so the session keeps the
last observation per canonical `tool_call_id` in memory (bounded, oldest dropped) and the
session snapshot (`GET /api/sessions/{id}`) carries them. A server restart
loses this in-memory projection. A missing card or server restart does not
prove native success; request completion and resource retirement have their own
[contract](architecture-contract.md#输入完成与资源退休).

## 5. The card

**Keyed on the spawning tool call's id, never on the tool's name.** The name
is not stable: this CLI version advertises `Task` in its init event and then
emits the call as `Agent`, and Codex's is `subagent:<tool>`. A card that
matched names would have quietly stopped rendering on a CLI upgrade — the
worst failure mode available, because everything else would keep working.
Every tool call mounts a card; the ones with no sub-agent behind them render
nothing.

Rendered where the spawning tool call sits in the transcript: the sub-agent's name, what it's doing right now, its token and
tool counts as they climb, elapsed time, and — when it lands — the summary,
collapsed to a few lines with the full text one click away. Failure says so
with its reason rather than a chip that simply stops moving.

## 6. Agents that bring their own sub-agents

`--agents '<json>'` registers session-scoped sub-agents on the Claude CLI:
`{"reviewer": {"description": "…", "prompt": "…", "tools": […], "model": "…"}}`.
The saved agent `subagents` configuration is rendered by the Claude profile.
An empty configuration leaves the CLI built-ins available; saved user definitions
are not rewritten by startup.

The current Octopus Codex exec profile does not render these saved custom
definitions. This describes the integration, not every available native feature.

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
* **Opening a sub-agent's own transcript.** Claude writes one under the
  session's `tasks/` dir and Codex's child threads are real threads on disk;
  reading them is a file-tailing feature, not an event-stream one.
* **Steering a running sub-agent** (Codex's `send_input`, Claude's
  backgrounded tasks). The model can already do it from inside the turn; a
  human doing it from the UI needs a target selector and a policy for what
  happens when the parent turn ends first.

## Historical evidence

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/native-subagents.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.

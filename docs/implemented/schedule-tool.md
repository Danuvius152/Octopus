# Agent 自设计划工具

> **Status:** implemented — 按会话作用域创建、列出、改期和暂停所属 Agent 的计划。

## 2. The recurrence is stated, not parsed

`/schedule` reads English, because a human typed it: `schedule_ai` tries a
rigid `<interval> <prompt>` fast path and otherwise spends a one-shot model
call turning "summarize my unreads every morning at 9" into a cron.

The agent path deliberately does not do that. The caller is already a
model: it can write `0 9 * * 1-5` itself, and asking it to means one tool
call instead of a tool call plus a second CLI process, a second parse and a
second way to fail. `build_explicit_schedule` is therefore the same
validation `validate_parsed` applies to the AI's JSON — minimum interval,
five cron fields that APScheduler accepts, a one-time run that is actually
in the future — applied to arguments, with messages written for whoever
passed the bad value:

```
Pass exactly one of `cron`, `interval_seconds` or `run_at` (got: cron, interval_seconds).
`cron` must be a 5-field crontab expression: '<minute> <hour> <day-of-month> <month> <day-of-week>' (e.g. '0 9 * * 1-5' for weekdays at 9am).
```

A model that gets one of those back fixes the call and retries. That is the
whole error-handling design.

**Timezone.** The browser sends its own zone with `/schedule`, so a human's
"9am" is their 9am. An agent has no browser, and defaulting to UTC would
quietly move every clock-time schedule. So an omitted zone means the
*host's* zone (`local_timezone()`: `TZ`, then `/etc/timezone`, then
`/etc/localtime`, then UTC), and a zone that *is* passed and isn't a real
IANA name is an error rather than a silent fallback to UTC — substituting a
different time without saying so is worse than refusing.

**Labels.** `describe_cron` puts the common shapes into words — "Weekdays at
09:00", "Every 15 minutes", "Every Fri at 17:00" — because the label is what
the sidebar and the Schedules page show, and "0 9 * * 1-5" printed twice
tells the user nothing. Shapes it would paraphrase badly (named weekdays,
month fields, steps in odd places) fall back to printing the expression.

## 3. Four tools

Named for what they do to a schedule, not to the API:

| Tool | What it is for |
|------|----------------|
| `mcp__schedule__create(prompt, cron\|interval_seconds\|run_at, name?, timezone?, in_session?)` | A new schedule for this agent |
| `mcp__schedule__list()` | Ids, recurrences, next fire times — call it before changing anything |
| `mcp__schedule__update(schedule_id, …)` | Pause/resume, re-word, re-time |
| `mcp__schedule__delete(schedule_id)` | Remove it |

Two things in the docstrings are load-bearing, because they are the only
place the model learns them:

* **The prompt must be self-contained.** When it fires, this conversation's
  context is gone and nobody is at the keyboard — "do the thing we
  discussed" fires into the void. (The fire itself says so too: it arrives
  prefixed `[scheduled:<name> — <recurrence>]` with "nobody is waiting to
  answer questions" — scheduled-runs.md §3.)
* **Pausing is not deleting.** "Stop doing X for now" means
  `update(enabled=False)`; a paused schedule stays in the list and can be
  resumed, a deleted one has to be rebuilt.

## 4. The route: the agent is derived, never passed

The shim POSTs to `/api/sessions/{sid}/schedules`, alongside
`…/delegations` and `…/research`. The session id comes from
`OCTOPUS_SESSION_ID`, injected when the harness spawns the MCP process; it
is not a tool parameter, so the model cannot get it wrong or aim it
somewhere else. The route resolves the session's owning agent and scopes
everything to it:

* `GET` lists that agent's schedules — not the box's.
* `PATCH` / `DELETE` on a schedule belonging to another agent return 404,
  not 403: from inside this session it may as well not exist.

`/api/schedules` (the account management surface used by the UI) resolves
`ScopeUser` and calls `load_schedules(user_id=...)`: it sees the current
account's schedules, not every account on the host.

**Switching recurrence.** A schedule has exactly one recurrence but three
columns, so a PATCH that sets one has to clear the other two — otherwise a
schedule moved from `interval_seconds=600` to `0 9 * * *` keeps its
interval and fires on whichever the runner reads first. `schedule_updates`
revalidates the whole recurrence and writes all four columns (including the
recomputed label); a bare `timezone` re-reads the existing cron in the new
zone. The same PATCH contract is available to authenticated callers. The
Schedules page currently exposes enable/disable, deletion and Wiki-mode
controls; a general prompt/recurrence editor remains a
[management proposal](../plans/schedule-management.md).

## 5. Where a fire lands

`in_session` (default true) sets `origin_session_id` to the conversation the
agent set the schedule up in, so each fire appends there and the user sees
the run where they asked for it. `in_session=False` gives each fire its own
throwaway session — the right choice for a noisy routine that shouldn't
fill a chat. Both modes, and the overlap guard that keeps fires from
stacking, are unchanged from scheduled-runs.md §1-2.

## 6. The list moves while you watch

Every schedule mutation now broadcasts `{"type": "schedules_changed"}` and
the client refetches through `schedules.refresh()` in the
[schedule list module](../../web/src/lib/scheduleList.ts). Without it, a schedule an agent
sets mid-conversation is invisible until the user reloads — which looks
exactly like the tool having failed. The payload is deliberately empty:
one code path, whether the change came from this browser, another tab, or
an agent.

The Runner owns this notification with the definition write and trigger
reconciliation, so losing the caller does not lose the change. Registration
failure returns HTTP 503 with `code=schedule_registration_unconfirmed`, the
original `schedule_id` and a string `detail` explaining that configuration may
already be saved. The MCP error preserves that identity and instruction. Read
the original schedule before another change; repeating creation can duplicate
the definition. A successful definition control does not prove a fire ran or
that an earlier execution retired. The
[Runner contract](scheduled-runs.md#definition-controls-and-shutdown) owns the
ordering and shutdown boundary.

## 7. On by default

`schedule` joins `ask`, `bg`, `ask_agent` and `research` in the default
per-agent MCP set, in the `CREATE TABLE` default for new databases, in the
UI's `BUILTIN_MCP` list for newly created agents. Existing agents keep their
saved tool choices across startup and upgrades; users enable additional tools
explicitly in their agent configuration.

## 8. Tests

* **The tool surface** (`test_schedule_mcp.py`) — request shape, the session
  id coming from the env, unset arguments omitted so the server's
  "exactly one" check sees what the model chose, a 422 relayed verbatim,
  an unreachable host reported as one, and the four exposed names.
* **The routes** (`test_schedule_tool.py`) — each recurrence kind, the
  validation refusals, scoping (another agent's schedule is invisible and
  untouchable), recurrence switching surviving a reload, pause/resume
  against the live runner, and that every mutation broadcasts.
* **The recurrence validator** (`test_schedule_ai.py`) — host-timezone
  resolution, an unknown zone refused, `describe_cron` on the shapes it
  claims and `None` on the ones it doesn't, the interval floor, a past
  `run_at`, a naive `run_at` read in the schedule's zone.
* **A real model, both harnesses** (`test_schedule_mcp_real.py`) — the only
  test that settles the actual question. Claude is asked for "every weekday
  at 9am" and the resulting cron has to fire Mon-Fri at 09:00 (asserted by
  running the trigger, not by string-matching `1-5`); asked to "stop the
  queue poll for now, I want it back later", it has to find the id itself
  and pause rather than delete. Codex creates a 30-minute interval from its
  own prompt blurb.
* **The live path** (`@llm` e2e) — a user asks in chat, the sidebar count
  moves without a reload or a navigation, and the row is on the Schedules
  page with the right recurrence.

## 9. Limits

* **Natural language in the tool.** `create` takes a recurrence, not
  "every other Tuesday". A model that can't express something in cron can
  say so; adding the AI parse here would put a second model call inside
  every tool call to cover a case we haven't seen.
* **Schedules for *other* agents.** The scoping is deliberate: an agent
  arranges its own work. Delegation already exists for asking someone else
  to do something, and "schedule Vera for 9am" raises questions about who
  owns and can cancel it that nothing yet needs answered.
* **General editing from the Schedules page.** PATCH accepts prompt and
  recurrence changes, while the page exposes enable/disable, deletion and
  Wiki-mode controls. Prompt/recurrence editing and its conflict/unknown-result
  behavior belong to the [management plan](../plans/schedule-management.md).

## Historical evidence

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/schedule-tool.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.

# Agent 自设计划工具

> **Status:** implemented — 按会话作用域创建、列出、改期和暂停所属 Agent 的计划。

## 2. The recurrence is stated, not parsed

`/schedule` reads English, because a human typed it: `schedule_ai` tries a
rigid `<interval> <prompt>` fast path and otherwise spends a one-shot model
call turning "summarize my unreads every morning at 9" into a cron.

The agent path deliberately does not do that. The caller is already a
model: it can write `0 9 * * 1-5` itself, and asking it to means one tool
call instead of a tool call plus a second CLI process, a second parse and a
second way to fail. Both paths supply candidates to `build_explicit_schedule`:
the AI adapter `validate_parsed` checks the declared kind and passes all
recurrence fields through, including conflicting ones. The shared validator
checks the prompt, exactly one recurrence, representable interval, five cron
fields, timezone and one-time future rule. Shared creation also validates internal
candidates; no model output is an already-admitted definition. Errors explain
what the caller should pass:

```
Pass exactly one of `cron`, `interval_seconds` or `run_at` (got: cron, interval_seconds).
`cron` must be a 5-field crontab expression: '<minute> <hour> <day-of-month> <month> <day-of-week>' (e.g. '0 9 * * 1-5' for weekdays at 9am).
```

These validation errors describe a refused candidate that the caller can correct.
They are distinct from a later registration error, where the definition may
already be saved and creation must not be blindly repeated (§6).

**Timezone.** The browser sends its own zone with `/schedule`, so a human's
"9am" is their 9am. An agent has no browser, and defaulting to UTC would
quietly move every clock-time schedule. So an omitted zone means the
*host's* zone (`local_timezone()`: `TZ`, then `/etc/timezone`, then
`/etc/localtime`, then UTC), and a zone that *is* passed and isn't a real
IANA name is an error rather than a silent fallback to UTC — substituting a
different time without saying so is worse than refusing.

**One-time admission.** A new `run_at` must be strictly later than the server's
current time plus five seconds. An offset-bearing datetime keeps its offset;
a naive datetime uses the schedule's zone. Browser `now` only helps the model
interpret the request: validation uses the server clock after parsing. A model
call that crosses the requested time returns 422 and creates no schedule.

The definition transaction repeats the same rule after acquiring the writer and
after its SQL and cursor cleanup. The last check, immediately before issuing
COMMIT, is the admission decision; expiration during those waits rolls back
without registering a trigger or announcing a saved definition. Time spent
waiting for COMMIT confirmation or trigger registration does not turn an
admitted write into a reversible refusal. Registration uncertainty retains its
[separate result](#6-the-list-moves-while-you-watch).

**Labels.** `describe_cron` puts the common shapes into words — "Weekdays at
09:00", "Every 15 minutes", "Every Fri at 17:00" — because the label is what
the sidebar and the Schedules page show, and "0 9 * * 1-5" printed twice
tells the user nothing. Shapes it would paraphrase badly (named weekdays,
month fields, steps in odd places) fall back to printing the expression.

### Cron calendar dialect

The five fields are minute, hour, day of month (DOM), month and day of week
(DOW). DOW accepts three-letter names, lists, positive steps, Sunday as 0 or 7,
and ranges that wrap around the seven-day week. A standalone DOW `?` is a
wildcard alias; `?` is not accepted in the other fields. Minute, hour, DOM and
month keep the existing APScheduler `CronTrigger` vocabulary.

Ordinary numeric ranges whose written start is no greater than their end expand
in 0…7 and apply the step before Sunday aliases merge. Wrapping ranges and ranges
containing names instead use the seven-day ring, visiting Sunday once:

| DOW | Matching weekdays |
| --- | --- |
| `0-7`, `1-7` | Every day |
| `0-7/2` | Sun, Tue, Thu, Sat |
| `6-1/2` | Sat, Mon |
| `7-1/2`, `7-0` | Sun |
| `fri-mon/2` | Fri, Sun |

DOM/DOW composition follows the original leading-star flags from the
[Vixie parser](https://github.com/vixie/cron/blob/master/entry.c) and
[matcher](https://github.com/vixie/cron/blob/master/cron.c). If either field
starts with `*`, both day sets must match; otherwise either may match. The DOW
`?` alias counts as a wildcard. Thus `*/2,1` retains a star flag while `1,*/2`
does not, even though their expanded sets agree. For example, `0 9 1 * mon`
runs on the first of each month and every Monday, while `0 9 */2 * mon` runs
only on Mondays with an odd day of month. These explicit extensions and the
retained APScheduler field vocabulary do not imply every crontab dialect is
supported.

The shared `cron_trigger()` returns one `BaseTrigger`: a `CronTrigger` for the
intersection or an [OrTrigger](https://apscheduler.readthedocs.io/en/3.x/modules/triggers/combining.html)
with separate DOM/DOW branches for the union. Both branches share minute, hour,
month and timezone; an impossible date in one branch does not hide the other.
The Runner registers one business job, so branch overlap produces one fire per
instant. Timezone and DST follow the existing `CronTrigger` behavior, including
distinct UTC instants in a repeated local hour.

**Calendar compatibility.** The range and day-combination corrections change
future dates for affected stored expressions as well as new ones. Stored cron
text retains its original stars and list order across reloads. The Schedules
list and API `next_run_at` read the registered trigger's corrected next date;
they do not use a separate preview calculation. Previously missed periodic
dates are not backfilled. This does not change overdue one-shot recovery.

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
zone. An explicitly supplied `run_at` is a new candidate, even when equal to
the stored value, and must pass the five-second rule through write admission.
A timezone-only reread, prompt edit or enable/disable keeps an existing overdue
once value valid; startup catch-up is also a read of existing intent, not a new
creation. The same PATCH contract is available to authenticated callers. The
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

After a schedule mutation commits and trigger reconciliation succeeds, the Runner
attempts `{"type": "schedules_changed"}` and the client refetches through
`schedules.refresh()` in the
[schedule list module](../../web/src/lib/scheduleList.ts). Without it, a schedule an agent
sets mid-conversation is invisible until the user reloads — which looks
exactly like the tool having failed. The payload is deliberately empty:
one code path, whether the change came from this browser, another tab, or
an agent. This invalidation currently broadcasts globally, not just to the
owning account; the subsequent list fetch remains account-scoped. It carries
no schedule content but still reveals that some schedule changed.

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

Intervals require whole seconds, a 60-second minimum and a value representable
in SQLite's signed 64-bit integer. The shared `interval_trigger` also constructs
the actual APScheduler trigger and checks its first fire through datetime,
timezone and timestamp conversion; there is no separate business-year ceiling.
The rigid parser, AI candidates, explicit creation and timing edits use this
same validation. Interval validation and registration both use the existing host
timezone source; an interval does not acquire a new wall-clock zone contract.

The definition transaction repeats interval validation before and after the SQL
and cursor cleanup, including re-enabling an invalid stored interval. Refusal
rolls back without replacing the previous definition or trigger. Disabling,
quarantining and operational run timestamps do not require making an invalid
recurrence runnable first. The Runner registers the returned trigger instead of
reconstructing a second formula. On reload, a known invalid recurrence is
isolated by the original expected-definition CAS; conflict rereads protect a
replacement definition. Unexpected storage/registration failures retain their
separate unconfirmed result rather than being disguised as bad user timing.

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
  claims and `None` on the ones it doesn't, whole-second/storage/first-fire
  interval boundaries, rigid and AI rejection, a past `run_at`, and a naive
  `run_at` read in the schedule's zone. Route/Runner tests also preserve the
  old definition on rejection and exercise edit/delete during quarantine CAS.
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

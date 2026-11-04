# 计划触发：执行身份与重叠控制

> **Status:** implemented — 计划 fire 的独立身份、重叠保护、触发标记与原执行入口。

## 1. Two modes, unchanged

A fire runs the schedule's prompt in one of two places under the
[Agent lifecycle](agent-refactor.md#creation-names-and-lifecycle):

* **the origin session**, when the schedule came from a `/schedule` chat
  command and that conversation is still live — the result lands where the
  user already is; or
* **a throwaway session** under the agent, auto-archived on idle, when there
  is no origin (continuity across fires comes from agent memory).

Completion uses the original submission receipt and execution retirement before
updating the completion timestamp and attempting automatic archive. For native
App Server, a root terminal freezes execution while its original resources retire;
that interval is not another parent computation. Empty final text and helper
partials do not replace the root outcome or require a result-text query. Failed
notification leaves the committed outcome intact. Stop pauses dispatch and keeps
that conversation visible under the existing archive policy.

### Definition controls and shutdown

The Runner owns each create, update and deletion before its first database
wait. The owned task commits the definition with the existing permission and
expected-definition checks, then reconciles the trigger under the registration
lock. Reconciliation rereads the current definition, so an overtaken edit cannot
restore a later deletion or re-enable a disabled schedule. Deletion commits
before removing its trigger. Cancelling the HTTP/MCP waiter does not cancel this
responsibility or its list-change notification.

A registration failure is observed with the original schedule ID and cause;
the saved definition remains authoritative. There is no automatic business replay
or compensating create. The [tool contract](schedule-tool.md#6-the-list-moves-while-you-watch)
describes the actionable error returned to callers.

Shutdown closes new control and fire admission and pauses launches, while already
owned controls may still register their committed definitions. It joins these
controls before destroying APScheduler, then joins the original captured callback
owners within the same absolute deadline. An unconfirmed control timeout preserves
the scheduler and task references for another join; it does not allow storage
teardown underneath them. Callback failure and retirement evidence remain separate.
An expected-definition conflict or closure of the original requesting work is a
typed, rolled-back domain refusal: the caller keeps its original 409, and the
finished control does not become a cleanup failure. Unexpected write failures,
registration failures and unfinished controls still fail shutdown.

Archive keeps origin repointing inside its original lifecycle operation. Triggers
hold only the schedule ID and each fire rereads the database origin, so repointing
needs no re-registration and preserves the original cadence, including an archive
that reaches this step after Runner admission closed.

Regressions in `test_schedules.py`, `test_scheduler.py`,
`test_knowledge_schedules.py` and `test_session_manager.py` cover cancelled waiters
after actual writes, competing definitions, HTTP/MCP registration failures,
shutdown timeout/rejoin and archive followed by a real successor fire.

## 2. A schedule never has two runs outstanding

A schedule that runs every 5 minutes and takes 10 used to start a second run
on top of the first, then a third — until the box gave out. Nothing noticed,
because each fire was its own session.

The guard is per mode, because "busy" means different things:

* **Throwaway mode**: the previous fire's session is recorded
  (`last_run_session_id`, written *before* the turn starts so an in-flight fire
  is visible to the next tick). If that session is still running or has a
  queue, the tick is skipped and logged. A session that is gone, or idle, is
  never a reason to skip — the guard must not be able to wedge a schedule.
* **Origin mode**: queuing behind the *user's* turn is the design, so a
  running session is fine. Queuing behind a *previous fire* is a pile-up, so a
  tick is skipped when the session already has something waiting.

Skipping keeps the schedule's cadence rather than shifting it: the next tick
tries again. (VM0 answers the same situation with "Previous run is still
active"; the difference is that Octopus can queue in the chat case, so it only
refuses the second one.)

One-time schedules use a different admission rule: a human backlog retains
the fire rather than skipping it. The target session, complete input and
consumption of the schedule commit in one database transaction. A one-shot
is deleted at that admission point; callback return and model completion do
not decide whether it was consumed. An overdue, unaccepted one-shot is
registered on startup. A transient refusal retries the same source identity;
an already accepted or uncertain execution is never submitted as a new fire.
See [input reliability](input-reliability.md) for the shared input contract.

## 3. A scheduled turn says it is one

Every other machine-injected turn in Octopus announces itself —
`[bg-task-result]`, `[agent-reply:<name>]`, `[app:<name>]`. A schedule's did
not, so an agent woken at 07:00 could not tell its own daily run from
something the user had just typed. It now arrives as:

```
[scheduled:<name> — <recurrence>]
This turn was started by a schedule, not by the user. Do the work and report
the result; nobody is waiting to answer questions.

<the schedule's prompt>
```

That last sentence is the point: a scheduled run that ends by asking a
clarifying question has asked nobody, and blocks until the question times out.

## 4. "Last run" opens the run

`last_run_session_id` is also what the UI needed: the Schedules page could say
*when* a schedule last fired but not *what happened*, which is the actual
question. The row is now a link into that session — for throwaway fires, the
auto-archived session the turn ran in.

## 5. What this defers

* **Per-fire history.** One pointer, to the latest run. A real run list means
  a `schedule_runs` table; the sessions themselves already hold the history,
  and nothing yet needs to page through it.
* **Catch-up for recurring fires.** Recurring schedules wait for the next
  tick instead of replaying every tick missed during downtime. A backlog
  policy for recurring work remains a separate product choice; unaccepted
  one-time schedules retain their original fire as described above.
* **A skipped-fire signal in the UI.** The skip is logged; surfacing it means
  deciding whether a skipped tick is worth a badge, which needs a real case of
  someone being surprised by one.

## Historical evidence

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/scheduled-runs.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.

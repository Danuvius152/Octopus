# 计划触发：执行身份与重叠控制

> **Status:** implemented — 计划 fire 的独立身份、重叠保护、触发标记与原执行入口。

<a id="1-two-modes-unchanged"></a>
## Execution modes

A fire runs the schedule's prompt in one of two places under the
[Agent lifecycle](agent-refactor.md#creation-names-and-lifecycle):

* **the origin session**, when the schedule came from a `/schedule` chat
  command and that conversation is still live — the result lands where the
  user already is; or
* **a throwaway session** under the agent, auto-archived on idle, when there
  is no origin (continuity across fires comes from agent memory).

Temporary-session completion waits on the original submission receipt and
execution retirement before attempting automatic archive. Empty final text and
helper partials do not replace the root outcome. Failed notification leaves the
committed outcome intact. Stop pauses dispatch and keeps the conversation visible
under the archive policy.

### Time fields and contract discrepancy

| Mode | Current write boundary | Stored value |
| --- | --- | --- |
| Origin session | `start_message` returns an accepted receipt, without waiting for completion | `last_run_at=now`, captured at fire entry; `last_run_session_id` is the origin |
| Temporary session | Save the session ID before submission; update time only after `_completion_recorded` confirms committed completion and execution retirement | The same fire-entry `now`, not the later completion time |

The completion-time and single-outstanding-fire requirements are not jointly
implemented by these branches. The meaning of `last_run_at` and
the scope of overlap prevention require a product decision. The behavior above
is an implementation observation, not approval of a weaker guarantee. Consumers
should use the original request completion and execution evidence to determine
completion, rather than inferring it from this timestamp.

### Definition controls and shutdown

The Runner owns each create, update and deletion before its first database
wait. The owned task commits the definition with permission, expected-definition and
[new once admission](schedule-tool.md#the-recurrence-is-stated-not-parsed)
checks, then reconciles the trigger under the registration
lock. Reconciliation rereads the current definition, so an overtaken edit cannot
restore a later deletion or re-enable a disabled schedule. Deletion commits
before removing its trigger. Cancelling the HTTP/MCP waiter does not cancel this
responsibility or its list-change notification.

Target validation observes one captured definition. Startup and archive/restore
ask the Runner to reconcile the schedule ID. A suppressed fire can request this
check while its original registration still owns the schedule. Under the
registration lock the Runner rereads the current definition before deciding
whether to register, leave suspended or remove the trigger. A completed restore
owns its trigger even if an earlier callback is still waiting on a target read.

Temporary Agent archive keeps the enabled definition for restoration. Permanent
target or knowledge invalidity, including an invalid stored recurrence or one-shot time,
disables only the definition actually checked and records its reason. The
expected-definition comparison includes Agent and origin session as well as text,
timing and permission. A conflicting edit or deletion restarts reconciliation
from the current row instead of disabling or recreating the old definition.

A registration failure is observed with the original schedule ID and cause;
the saved definition remains authoritative. There is no automatic business replay
or compensating create. The [tool contract](schedule-tool.md#the-list-moves-while-you-watch)
defines whole-second, storage and actual first-fire validation for intervals.
New timing definitions and re-enabling are checked in the writer transaction;
registration uses the same constructed interval trigger. Known invalid stored
recurrences are disabled with `invalid_recurrence` through expected-definition
CAS, while invalid one-shot times keep `invalid_run_at`. Conflicts reread the
replacement instead of disabling it. Unknown add-job or storage errors remain
registration failures, not timing refusals. The list-change notification is attempted only after successful
reconciliation, so that failure can leave browsers unaware of the saved change.

Shutdown closes new control and fire admission and pauses launches, while already
owned controls may still register their committed definitions. It joins these
controls before destroying APScheduler, then joins the original captured callback
owners within the same absolute deadline. An unconfirmed control timeout preserves
the scheduler and task references for another join; it does not allow storage
teardown underneath them. Callback failure and retirement evidence remain separate.
An expected-definition conflict, closure of the original requesting work or an
expired new once candidate is a typed, rolled-back domain refusal. The caller
keeps the original 409 for conflict/closure or 422 for invalid timing; the finished
control is neither an unexpected control error nor a cleanup failure. Existing
overdue definitions retain their catch-up behavior. Unexpected write failures,
registration failures and unfinished controls still fail shutdown.

Archive keeps origin repointing inside its original lifecycle operation. Each
fire rereads the database origin, so repointing preserves the original trigger
and cadence, including an archive that reaches this step after Runner admission
closed.

Regressions in `test_agents_api.py`, `test_schedules.py`, `test_scheduler.py`,
`test_knowledge_schedules.py` and `test_session_manager.py` cover cancelled waiters
after actual writes, competing definitions, HTTP/MCP registration failures,
shutdown timeout/rejoin and archive followed by a real successor fire. The
restored-Agent regression releases the old archived-target observation only
after restore completes, then checks the current trigger and a real later fire.

### Captured fire definitions

A timer carries its Runner registration, including the schedule ID and original
trigger parameters. Each date arm has a distinct APScheduler job ID. Periodic
registrations share the job ID across edits to preserve the executor's instance
limit. A callback checks the current registration, enabled definition and
trigger parameters before capturing the target, prompt and knowledge contract.
After a periodic fire is captured, it can finish with those values across later
edits, pauses and deletion. A pause removes future triggers and retains the
definition. Deletion removes both.

Reading the snapshot is not durable input admission. In particular, a one-shot's
target/input admission transaction still compares its captured definition with
the current row before consuming it. An edit, pause or deletion before that
transaction can refuse admission. Once accepted, its original input, completion
and resource retirement have their own identities. The
[one-shot recovery boundary](#overlap-checks-and-one-shot-admission) remains
in force; periodic history is not backfilled by calendar corrections.

<a id="2-a-schedule-never-has-two-runs-outstanding"></a>
## Overlap checks and one-shot admission

For periodic schedules, [Runner](../../server/scheduler.py) checks each mode
separately. Temporary mode checks the previous session's status, pending queue,
request and execution. Origin mode checks only the pending queue: an active
fire with an empty queue does not block the next fire. A pending human input
also makes that check true; it does not identify which input is a prior fire.
These checks cannot establish a universal single-outstanding-fire guarantee.
A skipped periodic tick retains the ordinary cadence.

One-time schedules queue behind a human backlog. SQLite obtains write ownership
and compares the enabled date definition, original `run_at`, target Agent,
origin, name, recurrence label, prompt and knowledge context with the values used
to prepare the input. It checks the original time against aware UTC. Stored
timestamps without an offset use UTC. Any temporary session, the complete input
and the deletion of the schedule commit together. The transaction checks the
original Runner registration before committing. A committed definition write
revokes the old date registration before trigger reconciliation starts. The
Runner keeps the old job ID until removal is confirmed. Schedule reads omit the
next run time while that revoked job awaits reconciliation. A pause followed by
re-enabling cannot restore the old registration's admission authority. The
command that creates the temporary target observes its original
execution and owns automatic archive, even if the callback loses the response.

The durable source is `schedule:{id}:{run_at}`, using the original stored string.
Its receipt can be observed even when the target or Agent configuration has
changed. A new submission validates the selected Agent before admission. An
overdue unadmitted date is registered on startup.

A healthy transient failure, invalid Agent configuration or an early wake after
clock rollback leaves the definition enabled. After an unconfirmed response the
Runner observes the original receipt and joins any still-pending submission for
that source. Under its registration lock it verifies that the original
registration still owns the enabled date, the source is unconsumed, no successor
job is armed and scheduling remains open. It then registers the later of the
original time and a five-second cooldown. The next callback prepares from the
current definition. A confirmed missing definition ends the callback. Loss of
storage trust stops recovery through the service health boundary.
See [input reliability](input-reliability.md) for the shared input contract.

<a id="3-a-scheduled-turn-says-it-is-one"></a>
## A scheduled turn says it is one

Each scheduled input has a source marker:

```
[scheduled:<name> — <recurrence>]
This turn was started by a schedule, not by the user. Do the work and report
the result; nobody is waiting to answer questions.

<the schedule's prompt>
```

Every new fire captures `interaction_policy="unattended"` in its original work
context, even if a human authored the definition. A later human input in the same
session captures human policy; the session's origin does not classify all future
work. BG, Research and delegation descendants/returns keep the fire's original
policy.

If a necessary decision has no authorized default, the original question closes
as `needs_input` immediately rather than leaving an unanswered human form.
The run reports completed independent work and the exact missing decision, and
stops dependent steps. A child may first seek a bounded answer from its actual
parent under the [delegation contract](agent-collaboration.md#unattended-questions).
The schedule's original admission and outcome rules do not change: no extra
fire, automatic replay or second consumption of a one-shot is created. A user
can supply a new explicit input, and recurring schedules retain their next
ordinary trigger. A model's successful final response is not proof of the
requested external effect.

<a id="4-last-run-opens-the-run"></a>
## "Last run" opens the run

`last_run_session_id` links the schedule row to its latest session. Temporary
runs remain readable through that link after automatic archival; origin runs
link to the continuing origin session.

<a id="5-what-this-defers"></a>
## Current limits

* **Per-fire history.** The schedule stores only its latest session pointer;
  there is no dedicated per-fire history view.
* **Catch-up for recurring fires.** Recurring schedules wait for the next
  tick instead of replaying every tick missed during downtime. A backlog
  policy for recurring work remains a separate product choice; unaccepted
  one-time schedules retain their original fire as described above.
* **Skipped-fire visibility.** Skips are logged and have no dedicated UI signal.

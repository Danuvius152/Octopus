# Input admission and recovery

The database owns the complete accepted input and its execution status.
SessionOwner decides when it can run. Acceptance commits the text,
attachment identities and byte hashes, request context, source, order and
stable identity together. The dispatch queue holds an identity and display
projection; execution reads the saved input. Model and tool effects begin
only after the claim commits.

## Receipts and results

Supply one `client_request_id` for an input, and retain it when confirming a
lost response. Repeating the same identity and content returns the original
fact; different content conflicts. An explicit refusal creates no input row,
so the user can retry that identity after fixing the cause. A response or
execution whose result is unknown must be checked under its original identity.
The browser uses cryptographic randomness and preserves the draft if none is
available.

| State | Meaning |
| --- | --- |
| `pending` | Accepted and not yet claimed. |
| `running` | Its execution claim committed; this alone does not prove model delivery. |
| `done` | Settled. Read `outcome.status` for the business result. |
| `withdrawn` | Withdrawn before execution; eligible inputs can be restored for review. |
| `unknown` | Execution or delivery cannot be confirmed; no automatic replay. |

Delivery evidence has its own `delivery_state`. An uncertain steer remains
unknown even when the carrying request has a successful result. For ordinary
single-input execution, the exact terminal and input settlement commit in one
transaction. Notification failure cannot reverse a committed outcome.

On restart, unclaimed inputs become withdrawn and claimed inputs become
unknown. Missing legacy payloads and completed legacy rows without an exact
terminal remain review-only. Historical refusal receipts stay read-only.
Managed startup retirement and current-generation retirement keep their
existing authority; hiding a saved input proves neither execution exit nor
permission to dispatch.

## HTTP and WebSocket

`POST /api/sessions/{session_id}/submissions` and WebSocket `send_message`
share the same authenticated admission path. The HTTP request contains
`client_request_id`, `content`, optional `attachment_ids`, `wiki_mode` and
`recovery_of_client_id`. A refusal has `accepted: false`; an uncertain
acknowledgment also has `state: "unknown"` and must not be treated as proof
that the input was never accepted.

`GET /api/sessions/{session_id}/submissions/{client_id}` returns the original
input, attachment evidence and execution result. The collection GET returns
saved-input notices with a stable `before` cursor. It associates exact
terminals, filters and sorts before applying the page limit in SQL.

## Independent returns and one-shot schedules

Background tasks, Research and named delegation rounds keep their original
work and delivery identities. They submit independent results as later
inputs; an acceptance acknowledgment follows the input commit. Resending a
lost acknowledgment observes the same input. The original request may finish
first, and a paused backlog remains paused. Native idle output only appends
to its original conversation; it does not reopen a completed receipt.
The original source task retains unacknowledged delivery and retries under
that source identity, confirming committed admission before retrying. Retry
does not repeat the command, research pipeline or delegation round. Shutdown
ends these live delivery owners; retained source results and durable input
facts remain available for review, without replaying uncertain work.
If the destination Owner permanently fails, sources first confirm whether the
input was already accepted. An accepted input keeps its original receipt.
An unaccepted return ends delivery and keeps the original result: background
output stays in its task record, Research reports failed delivery, and a
delegation exposes `terminal_disposition: "unavailable"` while its completed
reply remains in the child conversation. A capacity refusal continues under
the same delivery identity.

A one-shot's target session, input and schedule consumption commit together.
Human backlog does not consume an unaccepted fire. Startup catches up an
overdue, unconsumed one-shot; a transient refusal retries the same source.
An accepted fire is not recreated after restart, including when its execution
is unknown. Confirming the original fire returns its original session and
receipt, including after automatic archive. Recurring schedules retain their
existing overlap policy.

## Saved inputs

Review shows the saved text, attachment metadata and the reason an outcome
is unknown. Restore only fills the composer. The user's send creates a new
input whose single `recovery_of` link identifies its source; source eligibility
and the unique direct successor are checked in the admission transaction.
Legacy forward links are read during migration and have no runtime writer.
An unknown input has no one-click restore-and-run action.

Saved inputs **Dismiss notice** commits only a persistent visibility edit,
then best-effort notifies the original Owner. An unavailable or retired Owner
does not veto the edit or get recreated. **Stop** interrupts the active request
and pauses dispatch; **Resume processing** explicitly resumes the backlog.
The control prompt's **Dismiss** only hides that frontend prompt.

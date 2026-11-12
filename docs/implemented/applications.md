# 应用：托管、构建与删除

> **Status:** implemented — 账户所属应用的独立构建、后端运行和分阶段删除。

## 1. Product and ownership

An application is an account-owned web app built by an agent, hosted from its
managed directory and opened in an iframe. Static files work directly; an
executable `start.sh` enables a supervised backend under the
[backend contract](application-backends.md). A running app can use the separate
[agent conversation API](app-agent-access.md).

## 2. Stored identity and directories

The builder uses an ordinary session (`origin='application'`). A new application
gets its immutable `app_id` before allocating
`workspace.paths_for(owner).applications/<slug>--<full app_id>`. The slug is
readable context; the complete stored ID separates applications with the same
display name. Renaming does not move resources, and existing rows retain their
stored `app_dir`.

Code, durable `.data` and rebuildable `.runtime` are separate siblings of that
stored address, including when code uses an alias. The siblings are not derived
from the code alias's final target. Application status, backend status and
deletion progress describe independent facts.

Existing application rows retain resource claims, including archived rows,
unfinished deletion and missing code directories. Allocation checks the whole
canonical code/data/runtime family against every retained row in the account
and refuses existing candidate entries, including dangling links. Kept files or
partially removed code do not make the original family available for reuse.
Allocation holds resource coordination until its row is saved; completed
deletion releases that claim only after its remaining effects and row removal.
The rows remain the persistent authority; there is no second resource registry.

## 3. Serving the app

`GET /apps/{app_id}/{path}` streams a file out of `app_dir`:

* the path is resolved and checked to be inside `app_dir` (`..`, absolute
  paths and symlinks that escape are 404s, not 403s — we don't confirm what
  exists outside);
* a directory (or the bare app root) resolves to `entrypoint`;
* `Cache-Control: no-store`, so a rebuild is visible on reload;
* auth accepts a bearer header, `?token=`, **or** the `octopus_app_token`
  cookie. The cookie exists because an `<iframe>` cannot send an
  `Authorization` header, and the app's own sub-resource requests
  (`<script src>`, `<link href>`, `fetch`) have to authenticate too. The SPA
  sets it (same-origin, `path=/apps`) right before mounting the iframe.

The iframe is sandboxed with `allow-scripts allow-same-origin allow-forms
allow-popups allow-modals allow-downloads`. `allow-same-origin` is deliberate:
without it the app gets an opaque origin and `localStorage` throws, which
breaks the most common kind of small web app there is. What we withhold is
`allow-top-navigation`, so a buggy app can't navigate the Octopus tab away
from under the user. The apps are written by the user's own agent, which
already has shell access to the host — the sandbox is a blast-radius limiter,
not a trust boundary.

## 4. Lifecycle

```
POST /api/applications           create row + dir + build session, send brief
      ↓ (status=building)
  agent's turn runs in the build session
      ↓ original request completion with its recording/outcome facts
ApplicationManager evaluates that build's entrypoint and outcome
      ↓
status = ready (entrypoint exists)  |  failed (it doesn't)
```

Creation is one Manager-owned task shielded from its HTTP waiter. Before any
resource effect, the existing agent lifecycle owner checks active admission and
registers preparation. It exclusively creates the code directory, saves the App
row and prepares its build session. The existing App projection coordination
covers row → session → binding, so a concurrent build or delete cannot replace
an unfinished first binding. A preexisting directory is never claimed for cleanup.

Preparation hands its stable App/session to the same original task before
releasing that registration. `application_created` is delivered outside agent
registration and App coordination, before submitting the initial input. This
permits a subscriber to archive the builder without waiting for itself and
prevents an old building announcement following the completed build. The input
phase takes fresh agent admission; archive closes new admission and drains work
already registered. Nested session/input operations keep their own gates. Caller
cancellation does not cancel either phase. Notification failure does not roll
back resources or start another build. This includes a creation subscriber
cancelling its notification: the same owned operation continues input handoff.

Failure handling follows the confirmed effect. Before the App commits, only the
directory this operation exclusively created is cleaned; cleanup errors retain
the exact path and reason. After commit, the stable App ID and known session
relationship are retained with a failed diagnostic. A known unaccepted input
permits conditional cleanup of a dedicated empty session: its original Owner
checks both admitted inputs and retained messages, then closes new admission
before deletion. A failed or retired Owner cannot certify emptiness. Conditional
refusal cannot replace an explicit delete; successful proof joins the existing
shared deletion task. That task alone retires resources and deletes rows. Only
confirmed deletion clears the App's session reference; cleanup failure retains
the relationship and explanation. A conditional refusal without an error needs
no projection write. After waiting for cleanup, an error can be appended only
while the status, error and update stamp still identify the original failure;
it cannot contaminate a newer build's completion.

Initial submission uses one preallocated request/delivery identity. A positive
receipt from that exact SQLite input proves admission. A pending original
completion retains projection authority. If the observed completion is already
unavailable, including an Owner ending between admission commit and in-memory
receipt registration, creation instead saves failed with the accepted input's
original request ID and an explicit unavailable diagnostic. It retains the
resources and does not replay the input. Missing input is not proof that Submit
was never handed off. Unconfirmed admission
keeps App/session/request identity in a failed diagnostic without replay or
deleting possible execution resources. An admitted input with a live completion
keeps projection authority instead of taking this diagnostic path. Failure
projection rechecks the App/session and current input in the writer transaction.
Known unaccepted preparation failure can project only before any message input
has been admitted, including an input whose terminal committed but whose App
observer is delayed. Cleanup diagnostics must still match the original status,
error and update stamp. They cannot overwrite another admitted build.
Authoritative storage distrust retains the shared fail-stop boundary.

`ApplicationManager` observes each original request
completion through `SessionManager.on_request_complete`, including direct
chat in the build session. An acknowledged business outcome triggers the
existing entrypoint check: present means ready, absent means failed with the
turn's error when available. A turn can fail after writing a usable app;
the entrypoint rule still applies. An unavailable/unconfirmed outcome instead
produces failed with an explicit reason. Failed result notification cannot
leave the build permanently building. The shared ownership and completion
rules are in the [original completion contract](input-reliability.md#原请求完成与失败交接);
deployment is verified separately.

New message admission into the exact current builder sets `building` and clears
the previous error in the same SQLite transaction as the input. Direct chat and
paused queued input use that boundary too. Duplicate input, compact, another
builder or a rolled-back admission does not manufacture a new build. Creation
still has its distinct preparation phase; request_build has no pre-admission
status write or stale-status restore.

Metadata PATCH uses the App's existing effect owner and rechecks account,
deletion and builder binding. Changing `entrypoint` while work is admitted may
save metadata, but cannot project a terminal state or advance `last_built_at`.
The narrow terminal transaction checks the same owner/binding/entrypoint, latest
independent message and pending/running inputs against their actual committed
terminal associations. It compares a committed completion's exact outcome;
carried steer completions do not evaluate again or grant another backend hook.
Only an eligible artifact evaluation can update build time and grant that hook.
The timestamp remains a build projection, not backend health or deployment proof.

An unavailable or recording-failed completion instead saves an explicit failure
diagnostic without build time or hook. When the original Owner has ended, its
shared immutable `ended_owner_inputs` snapshot allows unavailable diagnostics to
ignore exactly those leftover inputs within the same session, including queued
and carried members. Snapshot-external work still blocks an old diagnostic;
carrier links do not expand that set. The snapshot changes neither committed
outcomes nor persistent input state. This distinction lets a failed Owner's
latest input converge instead of remaining `building` indefinitely.

For App Server, completion follows the bound root and its original resource
retirement. A remaining helper partial neither proves a valid app nor forces a
build failure. The entrypoint check still decides the artifact status, including
a usable artifact written before root failure or Stop. This consumer reads no
final-answer string and adds no result-text query.

Queued, dispatch and message-running notifications read the current committed
App projection; they do not write `building` from delayed progress. Dispatch
also announces idle preparation before attachment/Git waits; empty dispatch
changes do not create build notices. Each App's notification owner serializes
updated/deleted delivery and rereads the current row after awaited subscribers.
It holds no App projection lock across subscribers, so reentrant new input or
delete can complete and the final notification reflects their newer fact.
Backend retirement is separately owned and does not delay notification; an old
terminal snapshot is not retained across its hook wait. Captured resource
identity and retirement rules are in [backend lifecycle](application-backends.md#6-lifecycle).

Each transition broadcasts `application_created` / `application_updated` /
`application_deleted` over the same WS bus every client already listens to, so
the sidebar and the app view update live.

`test_application_creation_owner.py` uses controlled native stdio, the real
SessionOwner and SQLite to cover lost acceptance responses before/after commit,
completion with/without an entrypoint, preparation versus followup build,
subscriber archive/cancellation, archive/caller cancellation at effects, and
conditional versus explicit deletion in both directions. It distinguishes an
accepted input with a live pending completion from an Owner ending just after
admission commits, and completes another build while the original empty-session
cleanup waits. Existing projection-only model
fixtures do not stand in for input-admission proof.
The same tests cover direct-chat commit before receipt registration, metadata
during building, rejected/duplicate/compact input, primary plus delivered steer,
failed Owner with queued input and steer, snapshot-external admissions, blocked
hooks and reentrant/slow subscribers. They assert final consumer state as well
as database state; a correct row alone does not prove notification ordering.

## 5. REST surface

| route | purpose |
|---|---|
| `GET /api/applications` | list (sidebar) |
| `POST /api/applications` | create + kick off the build |
| `GET /api/applications/{id}` | one row |
| `PATCH /api/applications/{id}` | rename / re-icon / change entrypoint |
| `POST /api/applications/{id}/build` | another build turn (`{prompt}`) |
| `DELETE /api/applications/{id}` | drop the row; `keep_files=true` keeps the dir |
| `GET /apps/{id}/{path}` | the app itself (cookie/bearer/query auth) |

`DELETE` is a server-owned operation. Concurrent requests share cleanup; a
cancelled HTTP waiter does not cancel it, and conflicting `keep_files` choices
are refused. The row's independent `deletion` fact records `retiring` or `files`,
the original file-retention choice and any failure. Build status does not stand
in for deletion progress.

New App Wiki effects share the application's update/deletion coordination in
`ApplicationManager.run_effect`. Each finite effect rechecks the owned row,
unfinished deletion and account availability under that App's lock. Deletion
that closes admission first refuses the old request; an effect admitted first
keeps the lock until its actual exit, and deletion waits. This applies to both
ordinary deletion and `keep_files=true`, including unfinished deletion phases.
Unrelated applications have separate locks.

The App bridge captures its original Wiki caller and ApplicationManager before
waiting for the request body, but body parsing holds no effect lock. Synchronous
file work retains the owned task and real thread exit through HTTP cancellation.
Maintenance submission protects commit and worker handoff rather than the later
model run; cancel, retry and verify retain their own finite completion boundaries.
The [Wiki operation contract](wiki-development-summary.md#受保护操作与入口) owns
those phases and the independent registration-generation checks. Deleting an App
does not delete Wiki content, History or registration, or revoke a previously
admitted independent Wiki job.

After closing admission, deletion checks resource ownership before retirement
advances its phase, then checks again under resource coordination before file
cleanup and row removal. Canonical families must not overlap within one
application or with another retained application. Allocation reserves missing
resource addresses too; deletion checks existing content trees that it can
actually remove, treating absent contents as completed effects. Required symlink entries at
their physical parents, including chained links, are also protected: removing
one application's tree cannot break another's stored access path. Independent
applications may share a parent link without claiming its whole target tree.
Conflicts or unresolvable ownership report application IDs and paths and retain
the row, current phase and retry responsibility; cleanup does not guess an owner
or move legacy directories.

The operation first closes input/start admission, then joins the original
development executions, exact delegated rounds and independent BG, application
conversations, and backend process group/readers. Session archive or deletion
retires a builder's independent work before its association can disappear.
New builders and archive successors retain `sessions.app_id`; before archiving,
deleting or replacing the application's reference to a legacy builder, its exact
ownership is retained in that field. This includes builders archived before the
format handoff and a restore that races the replacement. All captured sessions
receive close commands even when another session refuses retirement; no such
failure permits file cleanup. No cwd or empty-work-ID
guess identifies ownership, and no deleted history is reconstructed. A completed
delegation may still own BG; an unrelated later human task in its child survives.

Shared-directory rewind successors inherit the exact application relationship
in the same SQLite transaction that admits the derived session. That transaction
also retains a legacy current builder's known relationship and checks deletion
admission; matching working directories never establish ownership. The returned
identity constructs the in-memory successor before publication. A committed
successor enters deletion's enumeration; a deletion that commits first refuses
new derivation. The parent's fork reservation protects preparation until the
successor is installed, and each operation releases only the reservation it
still holds. Archive, session deletion and successful application release use
this relationship regardless of the session's origin label. Full copies own
independent resources. The current builder pointer and Iterate's active-session
reuse policy do not follow rewind successors.

Only confirmed retirement allows file cleanup. All code, `.data` and `.runtime`
targets must be inside the account's managed root before the first removal.
Missing targets are complete; other filesystem errors preserve the application
row, remaining files and retry responsibility. Cleanup is not a filesystem
transaction. Only completed cleanup permits row deletion and its success event.

`application_resources` owns the common interpretation used by exclusion checks,
cleanup and account transfer: stored address, code/data/runtime role, actual
content tree and required link entries. Siblings derive from the stored code
address; `..` is retained until symlinks resolve. After writer retirement,
cleanup obtains a fresh observation under resource coordination. It removes
exclusive real contents and application-owned links inside the managed tree,
retaining other applications' required links and account-level access aliases.
An ancestor alias never grants ownership of its entire target tree.

Removal order keeps unfinished resources discoverable from their stored paths.
Contents precede their access links; an inner link precedes its outer aliases;
a tree containing another resource's required link follows that resource.
Completed access chains are removed before unrelated content cleanup proceeds.
Unsafe cyclic dependencies are refused before file effects. A failed removal
retains the original files phase; retry reobserves what remains and continues
without replaying confirmed retirement or persisting another resource ledger.

`keep_files=true` skips file removal, with the same retirement and resource
conflict requirements. An external legacy address alone does not prevent that
choice from completing; managed-root restrictions protect allocation and actual
file removal.

An unfinished deletion remains closed after restart; startup neither runs the
app nor resumes file deletion. Explicit retry continues its unconfirmed phase.
The app view displays the reason and a retry action. Account transfer preserves
this lifecycle fact without restoring execution authority; see
[storage handoff](current-storage-format.md) and [account transfer](../backup-and-migrate.md#account-export-and-import).
Build histories remain readable and retain their ordinary archive/restore/input
behavior after successful deletion. A later new instruction may recreate their
working directory; this change retires original writers, not future human work.

## 6. Build brief and UI

`ApplicationManager.compose_build_prompt` specifies the entrypoint, code/data
ownership, static serving and optional foreground backend/install scripts. A
follow-up build reuses the conversation. The source prompt is authoritative;
documentation does not maintain a second copy of runtime instructions.

The brief directs new App clients to the authenticated host-provided
`agent/client.js` module and its original-request recovery contract. It supplies
an inline module example rather than requiring each App to implement SSE or
retry logic; installed applications are not rewritten. The API, timeout receipt
and client behavior live in [App Agent access](app-agent-access.md).

The application view provides reload/open/archive controls, backend status,
application conversations and an Iterate popover with the development-session
link. The iframe does not replace ordinary chat state; chat remains mounted
while another main view is selected. Application and session list updates use
the [shared business reconciliation contract](frontend-list-reconciliation.md).

## 8. Verification and limits

Code: [ApplicationManager](../../server/applications.py),
[routes](../../server/routers/applications.py),
[ApplicationView](../../web/src/components/ApplicationView.tsx).
Regression entry points include `tests/test_applications.py`,
`tests/test_app_backend_capacity.py`, `tests/test_app_agent.py` and
`web/e2e/applications.spec.ts`.

Public publishing, application version rollback and permanent closure of retained
development conversations are not part of this contract. Successful deletion
retires the original writers; a later human instruction in retained history can
create new work and recreate its directory.

## Historical evidence

The [original design and evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/applications.md) retain their original version and conditions. Current verification follows [testing.md](../testing.md); this document does not establish production deployment.

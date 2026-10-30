# Applications: hosting, builds and deletion

> **Status:** implemented — Account-owned applications with independent build, backend and deletion lifecycles.

## 1. Product and ownership

An application is an account-owned web app built by an agent, hosted from its
managed directory and opened in an iframe. Static files work directly; an
executable `start.sh` enables a supervised backend under the
[backend contract](application-backends.md). A running app can use the separate
[agent conversation API](app-agent-access.md).

## 2. Stored identity and directories

The builder uses an ordinary session (`origin='application'`). Code paths are
allocated below `workspace.paths_for(owner).applications`, stored on the row and
not renamed with the display name. Code, durable `.data` and rebuildable
`.runtime` are separate siblings. Application status, backend status and deletion
progress describe independent facts.

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

`ApplicationManager` observes each original request
completion through `SessionManager.on_request_complete`, including direct
chat in the build session. An acknowledged business outcome triggers the
existing entrypoint check: present means ready, absent means failed with the
turn's error when available. A turn can fail after writing a usable app;
the entrypoint rule still applies. An unavailable/unconfirmed outcome instead
produces failed with an explicit reason. Failed result notification cannot
leave the build permanently building. The shared ownership and completion
rules are in the [architecture contract](architecture-contract.md#输入完成与资源退休);
deployment is verified separately.

The broadcast subscriber only derives running progress from current execution.
Per-session projection updates coordinate without holding that coordination
across subscriber callbacks; after awaited backend rebuild work, the consumer
checks that it still represents the original request. Thus a late old running
frame or old completion cannot overwrite the next build's status.

Each transition broadcasts `application_created` / `application_updated` /
`application_deleted` over the same WS bus every client already listens to, so
the sidebar and the app view update live.

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

Only confirmed retirement allows file cleanup. All code, `.data` and `.runtime`
targets must be inside the account's managed root before the first removal.
Missing targets are complete; other filesystem errors preserve the application
row, remaining files and retry responsibility. Cleanup is not a filesystem
transaction. Only completed cleanup permits row deletion and its success event.
`keep_files=true` skips file removal, with the same retirement requirement.

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

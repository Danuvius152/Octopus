# 应用后端：运行与进程监督

> **Status:** implemented — 应用脚本、端口、代理与进程组监督，保留代码、数据和依赖的不同归属。

<a id="2-the-contract-two-scripts-not-a-manifest"></a>
## Backend declaration and scripts

**An Application has a backend if it contains an executable `start.sh` at its
root.** That is the whole declaration.

```
install.sh    optional — prepare dependencies. Run once, and again after a
              rebuild. Must be idempotent.
start.sh      required for a backend — start the server in the FOREGROUND,
              listening on 127.0.0.1:$PORT.
```

The builder chooses the interpreter, dependency commands and lockfiles in
these scripts. Octopus allocates the port, supplies environment variables,
supervises the process group and proxies requests.

**`start.sh` must not daemonize.** The process it execs is the process we
supervise; a script that forks and returns leaves the serving process unowned.
The build prompt requires foreground execution; [readiness checks](#lifecycle)
also require the supervised process to remain alive.

<a id="3-three-directories-three-owners"></a>
## Three directories, three owners

```
<applications_dir>/<slug>--<app_id>/          code    — the agent's output, rewritten by a rebuild
<applications_dir>/<slug>--<app_id>.data/     data    — the app's own state, never touched by a rebuild
<applications_dir>/<slug>--<app_id>.runtime/  runtime — installed dependencies, ours to delete
```

These are newly allocated paths under the
[application identity contract](applications.md#stored-identity-and-directories).
For existing applications, use the stored code address and its actual siblings.

The split exists because the three have different lifetimes and different
owners. The code directory belongs to the building agent and is the only one it
writes to. The data directory belongs to the *running* app — a cloned repo, a
SQLite file, uploads — and must survive the agent rewriting the app around it;
that is the whole reason it is a sibling rather than a subdirectory, since a
subdirectory is inside what a rebuild may clobber.

The runtime directory is separate from data because a `.venv` or `node_modules`
is **derived**, not application state. This keeps rebuildable packages out of a
data-only backup. Runtime also contains [diagnostic logs](#logs), however: removing
it loses those logs, and removal is not a safe way to stop a live backend.
Confirm retirement and preserve any needed diagnostics before clearing runtime
files; use the supervised reinstall action for ordinary dependency preparation.

Neither `.data` nor `.runtime` is ever served statically — only the code directory
is reachable through `/apps/{id}/…`, exactly as today.

<a id="4-what-the-scripts-are-given"></a>
## What the scripts are given

Both scripts run with `cwd` = the code directory and this environment:

| variable | meaning |
|---|---|
| `APP_ID` | the application's id |
| `APP_DIR` | the code directory (= cwd) |
| `APP_DATA_DIR` | the data directory; create it if you need it |
| `APP_RUNTIME_DIR` | where to install dependencies |
| `OCTOPUS_AGENT_API` | this application's scoped `/apps/{id}/agent` endpoint; see [agent access](app-agent-access.md) |
| `OCTOPUS_APP_TOKEN` | scoped bearer for that endpoint; never the account or installation token |
| `PORT` | **start.sh only** — the port to bind on 127.0.0.1 |

Plus a minimal inherited environment: `PATH`, `HOME`, `LANG`, and nothing else
from the server process. The server's own environment holds the Octopus auth
token, credential material and tunnel config; a backend has no business seeing
any of it, and inheriting it wholesale is the kind of thing that is invisible
until it isn't.

A recommended `install.sh`, also shown by the build prompt:

```bash
#!/usr/bin/env bash
set -euo pipefail
python3 -m venv "$APP_RUNTIME_DIR/venv"
"$APP_RUNTIME_DIR/venv/bin/pip" install -q -r requirements.txt
```

```bash
#!/usr/bin/env bash
set -euo pipefail
exec "$APP_RUNTIME_DIR/venv/bin/python" -m server --port "$PORT"
```

`exec` matters: it makes the interpreter *become* the script's process, so the
pid we supervise is the server itself and a signal reaches it rather than a
shell that has stopped listening.

<a id="5-dependency-installation"></a>
## Dependency installation

`install.sh` runs:

* before the first start, and
* after any build turn that touched the app (the agent may have added a
  dependency), and
* on an explicit "reinstall" from the UI.

It runs with a timeout and captured output. A failed install shows
`install failed` with the last lines of output in the Backend panel.

Install has network access.

<a id="6-lifecycle"></a>
## Lifecycle

**Start is lazy.** The backend starts on the first request that needs it, not
when the app row is created and not at server boot. A workspace with a dozen
applications should not be a workspace with a dozen idle servers.

**Readiness is a TCP accept on `127.0.0.1:$PORT`**, polled until a timeout
(30s). No health-endpoint convention: every server binds a port, not every
server has `/health`, and a port that accepts is the thing the proxy actually
needs. A backend that never binds is reported as such, with its log — this is
what a daemonizing `start.sh` looks like from the outside.

**Idle stop.** With no active request or transferred work retaining a use,
the process stops after 15 minutes. This is the current `IDLE_TIMEOUT_S` constant
in [app_backends.py](../../server/app_backends.py), not an environment setting.
The next request starts it again. The admission bound is at most four admitted backend lifecycles, including startup,
running processes and unconfirmed retirement. The supervisor reserves a slot
atomically on its event loop before spawn; only confirmed spawn failure or
retirement of the original process group, output readers and request cleanup releases it.
Dependency installation does not consume a backend slot.

If `start.sh` disappears or loses its executable bit, `ensure_running` checks it
inside admission/lifecycle coordination. Retained process, spawn, readers, uses
or a slot transfer to the existing retirement owner before reporting `absent`.
Process/group and reader retirement precede request-use cleanup outside the
lifecycle lock; only confirmed cleanup releases the slot. Observer cancellation
does not cancel that owner, and failure retains its resources and capacity claim.
After the join, script validity is checked again: a restored script can start,
while a still-absent script clears the installation fact. Script removal itself
is not a stop API. Real backend and Wiki 202-follower regressions cover removal,
chmod, active uses, uncertain cleanup, cancellation and restoration.

Build evaluation captures an opaque identity for the existing install/start/
process lifecycle before committing its projection. Only a successful conditional
projection grants the rebuild hook. The identity survives that startup and its
retirement; RUNNING reuse does not replace it. A new accepted resource lifecycle
does. The hook compares identity and seals retirement without an intervening
await, then joins the original retirement owner outside App coordination. It can
cancel a blocked installation promptly and cannot retire a replacement startup
or process. Capturing no resource is a no-op, not permission to look up one later.
This identity is process-local and grants neither build outcome nor input authority.

Before reading a body, `BackendSupervisor.acquire` selects and registers the
original port/process without an intervening await. The same `BackendUse`
protects body waits, upstream send, response consumption and confirmed cleanup.
Returning a response object or receiving headers does not release it. Ordinary
stream cleanup belongs to the actual ASGI invocation, including header-send
failure and cancellation before iteration; an unstarted response remains owned
and can be closed by the supervisor. Response and client cleanup are both
attempted, including when one fails. Unconfirmed cleanup retains the registration.
`app_proxy.own_response` retains the original stream-close acknowledgment across
HTTPX automatic body closure: `Response.is_closed` is set before cleanup awaits
and cannot prove that a failed close completed. Both transport paths join the
same close task rather than replaying cleanup or trusting that flag.

Proxy routing removes the verified application and mount prefix from ASGI
`raw_path`, preserving the encoded suffix without reparsing decoded characters
as URL syntax. A missing optional `raw_path` encodes the literal path once;
inconsistent prefixes return HTTP 400 before acquiring a backend. Query pairs
retain their order and duplicates, excluding the Octopus token. URL construction
is inside the original use's cleanup boundary, so even an invalid or excessively
long target cannot leave a use blocking drain, idle reaping or capacity eviction.
Real ASGI-to-loopback regressions cover encoded punctuation, Unicode, mount
paths, write bodies and construction failure.

Wiki polls also capture each streamed response before reading it. A successful
close releases that local transport responsibility while retaining the job use;
failed close receipts survive later successful polls and continue blocking
retirement. Long jobs do not accumulate already closed response registrations.

When full, admission stops an eligible RUNNING instance with no active uses in least-recently-used
order under that instance's lifecycle lock, with no requesting-app lifecycle
lock held. Requests for the same app serialize admission, including eviction,
so concurrent opens share a single start without retiring extra victims.
Admission never nests two apps' lifecycle locks. Startup and retirement do not
hold a global capacity lock. Idle reaping also skips the same active uses.
If all slots are starting, busy or retiring, the new request gets HTTP 503 without forwarding its
body. A Wiki mutation waiting for its file lock readmits only the original
backend; it never invokes lazy startup again. Proxy captures an immutable port
and process target before awaiting the request body. The URL and check after
lock acquisition use that same target, rejecting missing, stopped, replaced or exited
processes with HTTP 503 before dispatch. A replacement backend retains its own
lifecycle and is not stopped by refusal of the old writer.
Capacity refusal does not mark the application failed or start crash backoff;
there is no capacity wait queue. Failed retirement retains the original slot,
including a process handle that arrives after its startup waiter is cancelled.
Shutdown seals admission, joins captured operations and retires their resources;
it reports incomplete cleanup instead of reusing an unconfirmed slot.

For a late startup handle without normal output pumps, that same retirement
owner starts concurrent pipe draining before waiting. Pipe backpressure can
delay the original wait even after leader and group exit. Normal output pumps
instead retain their EOF window; if it expires, a truncation diagnostic is
recorded and cancellation is requested, then their actual exit is joined.
Only after those consumers exit can the shared raw drain take over, confirming
both pipe EOFs, `proc.wait()` and the original waiter before capacity is released.
The raw drain discards descriptor-cleanup bytes; it does not reconstruct lost
log output. A timeout or reader that has not exited retains the original
instance, references and slot for reentrant retirement. Group exit is never
gated on successful pipe consumption or mistaken for completion of that work.

Explicit stop, rebuild, delete and forced shutdown seal admission before joining
the original process and consumers. Process exit is confirmed under the lifecycle
lock; consumer interruption and cleanup run outside it. A Wiki abort waits only
for that original process-exit proof, so cleanup cannot wait on its own full
retirement. Consumers already cleaning up are joined without cancelling their
caller and masking its original error. Timeouts retain the original handle,
registration and slot with closed admission. A later stop can join cleanup that
finished late without signalling a process group already proven retired.
Successful stop reopens ordinary lazy-start rules; rebuild invalidates installation
before reopening, and deletion remains sealed. Natural drain reads active uses
and does not interrupt them. A captured response may finish after its original
process exits: consuming those bytes sends nothing to a replacement port.

These checks protect managed lifecycles and reject known target loss before a
send. A TCP port is not an identity credential for arbitrary same-host processes;
this contract does not provide operating-system isolation.

Application deletion seals install/start admission before joining the original
backend state. A late spawn, unconfirmed group exit or undrained reader keeps
deletion in its retirement phase, preserving files and the application row.
The supervisor's original handle and slot remain available for retry. The
[application lifecycle](applications.md#rest-surface) owns the persisted
unfinished-deletion fact; startup and app routes keep it closed until explicit
retry, including when files are being retained.

Regression coverage in `tests/test_app_backend_capacity.py` exercises four real
STARTING processes and a refused fifth proxy request, failed and cancelled
startup, late spawn handoff, failed retirement, concurrent LRU admission, shared
startup and admission, installation isolation and mutation re-admission.
Process-group exit evidence supplements observable HTTP and backend behavior.

**Rebuild restarts.** A build turn rewrites the code; the running process is
still executing the old one. On build completion: stop, reinstall if needed,
and let the next request start it fresh.

**Crash is not retried in a loop.** A process that exits on its own is recorded
with its exit code and last output, and the app is marked `backend failed`. The
next request tries once more; repeated failures back off. A crash loop that
silently eats CPU while the UI says nothing is worse than an app that says it's
broken.

**Shutdown stops everything**, and stops the process *group* — a backend that
spawned children (a `git` process, a worker) must not outlive the server, for
the same reason the harness terminates groups rather than pids.

<a id="7-routing"></a>
## Routing

```
/apps/{id}/api/…   → proxied to 127.0.0.1:$PORT/api/…   (backend)
/apps/{id}/…       → static file from the code directory (today's behaviour)
```

One prefix, chosen over the cleverer "serve a static file if it exists, else
proxy". Fallback routing means whether a request reaches your backend depends on
whether a file happens to share its path — a rename in the code directory
silently changes routing. A fixed prefix is predictable and greppable, and an
agent can be told it in one line.

The proxy carries the existing app-cookie auth (`_authorized`), buffers the
request body, streams ordinary backend responses as raw bytes, and forwards the
method, path, query and a conservative header set. Static app content uses
`no-store`, with a short cache for the discovered icon. Proxied cache headers
pass through except on 404, where the site sets `Cache-Control: no-store`.

The reserved `token` query parameter authenticates to Octopus and is removed
in every occurrence before forwarding. All other pairs retain their original
order, including interleaved repeated keys, empty values and Unicode text.
Authentication headers and cookies are excluded from the forwarded header set.

For a protected Wiki mutation, `app_bridge`/`AppMutation` own admission, original
backend revalidation, dispatch, response consumption, effect recording and cleanup.
The proxy consumes the managed result. Decoded managed responses drop encoded
representation headers; ordinary responses retain their compressed bytes and
matching headers. Both paths forward the remaining upstream fields in their
original order with their value bytes intact. Repeated fields such as
`Set-Cookie` and `Vary` retain each separate occurrence. The route bypasses
the site's GZip middleware. Site CORS still handles preflight and sets its
policy headers on applicable responses. When that policy needs an origin
vary and the response varies on neither `Origin` nor `*`, it appends a
separate `Vary: Origin` field.

A valid 202 transfers the same client and mutation to the
existing job follower, retaining the lock and original job ID after the HTTP
consumer leaves. Malformed receipts, mismatched job identity, unknown terminal
status or uncertain dispatch use the original abort/retirement path and preserve
file effects for reconciliation. The proxy does not orchestrate transfer flags or finish/abort
ordering. Its ordinary exception path also retains transport cleanup across
repeated cancellation.

`tests/test_app_proxy_wiki_target.py` covers real stop/restart and spontaneous
exit during waits, including another HTTP service reusing the old port with zero
body dispatch after refusal. It also covers transferred 202 work, consumer
cancellation, invalid receipts, ordered repeated fields and the cookie jar.
`tests/test_application_proxy_encoding.py` covers raw/decoded bodies and
repeated-cancellation cleanup.
`tests/test_app_request_lifetime.py` additionally covers ordinary slow bodies,
capacity, idle/drain, unstarted responses, ASGI failures and control operations.
`tests/test_wiki_app_request_lifetime.py` observes real 202 work, file effects,
follower transfer/refusal, partial receipts, cancellation and old-port reuse.

The Wiki follower holds the same use through actual job settlement and journal
cleanup, and validates the original target before every poll. A 202 receipt is
not mutation success. Transfer registers the follower without yielding or
releasing the use; a rejected transfer keeps cleanup with the original caller.
Abort stops that captured instance, never a later instance with the same app ID.
Confirmed cleanup can retire an observation error while retaining its diagnostic;
failed journal or transport cleanup remains a drain blocker.

The supported remote entry is this authenticated proxy. A conforming backend
binds `127.0.0.1`, so it is not directly exposed on the network; the tunnel exposes
Octopus, not its port. Same-host processes can still connect directly, and
Octopus does not enforce the script's bind address with an OS sandbox.

<a id="8-logs-because-a-dead-backend-with-no-output-is-useless"></a>
## Logs

Every `install.sh` and `start.sh` invocation appends to the same
`<slug>--<app_id>.runtime/logs/backend.log`, alongside supervisor lifecycle messages.
There is no automatic rotation, capacity limit or capacity-based deletion.
The supervisor preserves decoded output text, including whitespace and newline
boundaries, independently of the display tail. UTF-8 decoding is incremental;
invalid sequences, including an incomplete sequence at EOF, use replacement
characters. This is a text diagnostic, not a byte-for-byte archive. A disk-write
failure does not stop the application, so completeness is not guaranteed when
the filesystem rejects a write.

The original supervisor consumes the merged stdout/stderr pipe in reads of at
most 4 KiB, without waiting for a newline. The UTF-8 decoder retains at most
three incomplete bytes. Display assembly retains less than 4,096 characters
between reads and at most 8,192 while processing a read. A long line becomes
4,096-character payload fragments with a `[continued]` marker; an unfinished
short fragment waits for a newline or EOF. Neither splitting nor display
eviction truncates the disk text. Lifecycle display messages are bounded by
the same payload limit.

The existing in-memory deque holds at most 200 fragments, each with at most
4,096 payload characters plus its fixed source tag and continuation marker.
The public `log_tail` also has at most 200 entries; after eviction its first
entry says `[earlier output omitted from display]`. These are character and
entry bounds, not a disk limit or an exact total-process memory budget. The
application **Backend** panel shows this tail with state, port and uptime.

Process/group exit, output EOF and supervisor retirement are separate facts.
After confirming process and group exit, cleanup allows readers five seconds
to finish EOF, including decoder finalization and a final unterminated fragment.
On timeout it records that remaining output may be truncated, requests
cancellation and allows another five seconds for reader exit. A reader still
running keeps its reference and blocks retirement and slot release. Outer
operation and lifecycle-lock waits allow these two cleanup windows in addition
to the existing process/group grace periods, including a concurrent stop joining
idle reaping that already holds the lifecycle lock. A later stop can join late
cleanup.

<a id="9-status-two-axes-not-one"></a>
## Build and backend status

| Fact | States and authority |
| --- | --- |
| Application build | `building / ready / failed`; the original committed completion and entrypoint check, as defined in [applications](applications.md#lifecycle) |
| Backend | `absent / installing / stopped / starting / running / failed`; supervised installation and process lifecycle |

A ready build can have a failed backend. A backend can still serve earlier code
while a build is running. Neither state substitutes for the other.

<a id="10-security-posture-stated-plainly"></a>
## Host execution and account authorization

Managed scripts run as the Octopus service host user. Account and route
authorization do not provide OS process isolation; a process can access files
available to that user. See [account trust boundaries](multi-tenancy.md).

Scripts receive a minimal environment, bind loopback and use an authenticated
proxy. Data/runtime directories are per application and are not served as static
files. Stop joins the original process group and readers; uncertain retirement
retains the handle and capacity claim. There is no per-app OS sandbox or
independent CPU/memory quota.

<a id="11-what-agents-are-told"></a>
## Build instructions and current limits

The actual builder instructions live in
[`ApplicationManager.compose_build_prompt`](../../server/applications.py).
They describe the static browser entrypoint and optional managed scripts.
The frontend loads without a build step. Static applications use browser
storage; optional backends use executable scripts and keep persistent state in
`APP_DATA_DIR` under the lifecycle described above.

<a id="12-what-this-defers"></a>
There is no independent application-secret configuration interface. Backend
port selection does not support a server that ignores the supplied `PORT`.

Implementation and regressions: `server/app_backends.py`,
`tests/test_app_backend_start_retirement.py`, `tests/test_app_backend_capacity.py`.

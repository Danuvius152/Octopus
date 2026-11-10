# 应用后端：运行与进程监督

> **Status:** implemented — 应用脚本、端口、代理与进程组监督，保留代码、数据和依赖的不同归属。

## 2. The contract: two scripts, not a manifest

**An Application has a backend if it contains an executable `start.sh` at its
root.** That is the whole declaration.

```
install.sh    optional — prepare dependencies. Run once, and again after a
              rebuild. Must be idempotent.
start.sh      required for a backend — start the server in the FOREGROUND,
              listening on 127.0.0.1:$PORT.
```

The obvious alternative is a manifest (`octopus.json` with `cmd`, `runtime`,
`port`), which is what the original request proposed. A manifest forces Octopus
to understand runtimes: which interpreter, which dependency file, which install
command, which lockfile, what to do when a project needs two of them. That is
the bulk of the work, and it is work we would be doing *on behalf of* an agent
that already knows the answer — it just wrote the app.

A script inverts that. The agent knows it needs `pip install -r
requirements.txt` or `npm ci`; it writes that line. Octopus never learns what
Python is. Our surface stays: allocate a port, set some environment, run two
scripts, supervise one process, proxy one path prefix.

**`start.sh` must not daemonize.** The process it execs is the process we
supervise; a script that forks and returns leaves us supervising a corpse and
the real server unowned. This is the one rule an agent is most likely to get
wrong, so it goes in the build prompt in those words, and the readiness check
(§6) catches it in practice.

## 3. Three directories, three owners

```
<applications_dir>/<slug>--<app_id>/          code    — the agent's output, rewritten by a rebuild
<applications_dir>/<slug>--<app_id>.data/     data    — the app's own state, never touched by a rebuild
<applications_dir>/<slug>--<app_id>.runtime/  runtime — installed dependencies, ours to delete
```

These are newly allocated paths under the
[application identity contract](applications.md#2-stored-identity-and-directories).
For existing applications, use the stored code address and its actual siblings.

The split exists because the three have different lifetimes and different
owners. The code directory belongs to the building agent and is the only one it
writes to. The data directory belongs to the *running* app — a cloned repo, a
SQLite file, uploads — and must survive the agent rewriting the app around it;
that is the whole reason it is a sibling rather than a subdirectory, since a
subdirectory is inside what a rebuild may clobber.

The runtime directory is separate from data for a reason worth stating: a
`.venv` or `node_modules` is **derived**, not user state. Keeping it out of the
data directory means "reinstall from scratch" is
`rm -rf <slug>--<app_id>.runtime` with no risk to anything the user cares about,
and means a backup of the data directory doesn't carry a few hundred megabytes
of packages.

Neither `.data` nor `.runtime` is ever served statically — only the code directory
is reachable through `/apps/{id}/…`, exactly as today.

## 4. What the scripts are given

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

## 5. Dependency installation

`install.sh` runs:

* before the first start, and
* after any build turn that touched the app (the agent may have added a
  dependency), and
* on an explicit "reinstall" from the UI.

It runs **with a timeout and its output captured** (§8). A failed install is a
first-class state, not an exception: the app shows `install failed` with the
last lines of output, because "it doesn't work" with no log is the failure mode
the original request specifically called out as miserable.

Install has network access. That is not a new capability — the building agent
already has it — but it is worth being explicit that this is the one place
Octopus itself runs a network fetch on an app's behalf.

## 6. Lifecycle

**Start is lazy.** The backend starts on the first request that needs it, not
when the app row is created and not at server boot. A workspace with a dozen
applications should not be a workspace with a dozen idle servers.

**Readiness is a TCP accept on `127.0.0.1:$PORT`**, polled until a timeout
(30s). No health-endpoint convention: every server binds a port, not every
server has `/health`, and a port that accepts is the thing the proxy actually
needs. A backend that never binds is reported as such, with its log — this is
what a daemonizing `start.sh` looks like from the outside.

**Idle stop.** After `APP_BACKEND_IDLE_TIMEOUT` (default 15 minutes) with no
active request or transferred work, the process is stopped. The next request starts it again.
Same reasoning as the CLI-process reaper in `inline-steering.md` §7, and the
same hard bound: at most four admitted backend lifecycles, including startup,
running processes and unconfirmed retirement. The supervisor reserves a slot
atomically on its event loop before spawn; only confirmed spawn failure or
retirement of the original process group, output readers and request cleanup releases it.
Dependency installation does not consume a backend slot.

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
[application lifecycle](applications.md#5-rest-surface) owns the persisted
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

## 7. Routing

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
method, path, query and a conservative header set. `no-store` stays on app content; the
backend's own cache headers pass through for its responses.

The reserved `token` query parameter authenticates to Octopus and is removed
in every occurrence before forwarding. All other pairs retain their original
order, including interleaved repeated keys, empty values and Unicode text.
Authentication headers and cookies are excluded from the forwarded header set.

For a protected Wiki mutation, `app_bridge`/`AppMutation` own admission, original
backend revalidation, dispatch, response consumption, effect recording and cleanup.
The proxy consumes the managed result. Decoded managed responses drop encoded
representation headers; ordinary responses retain their compressed bytes and
matching headers. A valid 202 transfers the same client and mutation to the
existing job follower, retaining the lock and original job ID after the HTTP
consumer leaves. Malformed receipts, mismatched job identity, unknown terminal
status or uncertain dispatch use the original abort/retirement path and preserve
file effects for reconciliation. The proxy does not orchestrate transfer flags or finish/abort
ordering. Its ordinary exception path also retains transport cleanup across
repeated cancellation.

`tests/test_app_proxy_wiki_target.py` covers real stop/restart and spontaneous
exit during waits, including another HTTP service reusing the old port with zero
body dispatch after refusal. It also covers transferred 202 work, consumer
cancellation and invalid receipts. `tests/test_application_proxy_encoding.py`
retains the raw/decoded encoding contract and repeated-cancellation cleanup.
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

A backend is reachable **only** through this proxy. It binds `127.0.0.1`, so it
is not exposed on the network, and the tunnel exposes Octopus, not the app.

## 8. Logs, because a dead backend with no output is useless

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

## 9. Status: two axes, not one

Today `status` is `building | ready | failed`, derived from whether the
entrypoint exists. That stays exactly as it is — it describes the *build*.

The backend needs its own axis (`absent | installing | stopped | starting |
running | failed`) rather than being folded into the same field. An app can be
perfectly built and have a crashed backend, or be mid-rebuild with a backend
still serving the old code. Collapsing those into one enum produces a status
line that is wrong in one of the two cases.

## 10. Security posture, stated plainly

`start.sh` runs arbitrary code as the Octopus user. This is **not** a new trust
boundary: the agent that wrote the script already runs with
`--dangerously-skip-permissions` in that same account, and could have run the
same command directly. The honest summary is that Octopus runs code from trusted account owners; API ownership checks
do not provide an operating-system sandbox.

What this design does add, and what it deliberately does not:

* **Adds:** a backend is bound to loopback and reachable only through an
  authenticated proxy; scripts get a minimal environment, not the server's;
  the data and runtime directories are per-app and never served; process groups
  are killed on stop so nothing outlives its app.
* **Does not add:** a sandbox. No namespaces, no seccomp, no resource limits
  beyond the process cap. An application can read the user's home directory if
  it tries. Anyone deploying this multi-tenant needs a container per app, and
  that is a different design — noted in §12 rather than half-done here.

## 11. What agents are told

`compose_build_prompt` includes the entrypoint and backend rules:

> If the app needs server-side work — running a program, talking to a service
> that blocks browser requests, or storing more than a few megabytes — write an
> executable `start.sh` at the root that starts a server **in the foreground**
> on `127.0.0.1:$PORT` (use `exec`, don't background it). Put dependency
> installation in `install.sh`, installing into `$APP_RUNTIME_DIR`. Keep the
> app's own data in `$APP_DATA_DIR` — a rebuild rewrites your code, never that
> directory. The frontend reaches the backend at `api/…`, relative to the page.

## 12. What this defers

* **A sandbox per application.** §10. Real isolation means a container or a
  user namespace per app, plus an image story; it is a larger design and the
  single-user posture doesn't force it yet.
* **Fallback routing** (serve static if present, else proxy), which would let a
  framework own every path. §7 explains why the fixed prefix is the safer
  default; this needs a real app that wants it.
* **Multiple processes per app** (a worker alongside the server). One script,
  one process; a second one is a manifest by another name.
* **Declared resource limits** (memory/CPU per backend). The process cap bounds
  the count, not the appetite.
* **Secrets for backends** — an app needing an API key or an SSH key has no way
  to get one that isn't checked into its own directory. This is the next thing
  worth building, and it is deliberately not smuggled into this design: it needs
  its own storage, its own UI, and a decision about what the agent sees.

## Historical evidence

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/application-backends.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.

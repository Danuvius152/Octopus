# 运行中应用与 Agent 的对话

> **Status:** implemented — 按应用令牌授权的 Agent 对话、实时返回与原执行完成。

<a id="2-the-shape-one-api-mounted-where-the-app-already-lives"></a>
## The shape: one API, mounted where the app already lives

```
/apps/{app_id}/agent/agents                      GET    who can I talk to
/apps/{app_id}/agent/conversations               GET    my threads
/apps/{app_id}/agent/conversations/{cid}         GET    one thread's messages
/apps/{app_id}/agent/conversations/{cid}/requests/{rid} GET original request status
/apps/{app_id}/agent/conversations/{cid}         DELETE forget a thread
/apps/{app_id}/agent/ask                         POST   one question, one answer (JSON)
/apps/{app_id}/agent/chat                        POST   the same turn, streamed (SSE)
/apps/{app_id}/agent/client.js                   GET    authenticated host client module
```

It sits under `/apps/{id}/` for the same reason the backend proxy does: the
page reaches it at `agent/…` relative to itself, with no base URL to configure
and no CORS. `ask` and `chat` are the same operation with two deliveries —
a backend script wants one JSON blob, a chat UI wants tokens as they land.

Successful JSON replies include `conversation_id` and `request_id`. After SSE
admission, `X-Octopus-Conversation-Id` and `X-Octopus-Request-Id` carry the same
identities as the initial `conversation` event. The streaming route bypasses
the response compression middleware so it can forward headers independently
of the body. A caller can keep them even if the body ends before its first event.
An `ask` response timeout returns HTTP 504 with structured `detail` containing
`code=response_wait_timeout`, `conversation_id`, `request_id`, `status_url` and
`operation_terminal=false`, including on a first call without a conversation ID.
SSE returns the same recovery fields in an error event. Recovery URLs include
the original HTTP mount prefix. The server observes the
original status for at most five seconds: a result already committed can be
reported even if live notification failed, while lookup failure or a bad answer
reference keeps the timeout receipt and an observation error. A response timeout
does not declare that the business operation failed or start another operation.
Query that original request instead of resubmitting it. After a restart, an
unavailable outcome is explicitly `unknown`.
SSE events carry request/attempt identities; a `reset` event clears text from an
abandoned attempt. Legacy replies retain their completed attempt's text blocks.
For App Server, `progress` carries root commentary/deltas, `message` carries
complete root final items, and `done.reply` uses the terminal's selected answer.
Child bodies remain in the product transcript. Helper interruption is structured
run state and is not appended as model prose.

Ask/SSE consumes the original request's single
completion. A successful `done` requires both an acknowledged business result
and its delivered notification prefix. Failed or unavailable notification
ends the app request with an explicit delivery error, preserving any already
committed transcript/outcome for the original status lookup; it never replays
the operation. App body delivery is a required subscriber: an ordinary exception
or callback-raised cancellation fails that original publication. JSON `ask`
returns HTTP 502 with structured `detail` carrying `code=reply_delivery_failed`,
`conversation_id`, `request_id` and `status_url`; SSE emits an `error` with the same
recovery fields and no successful `done`. Already displayed fragments remain
visible. Poll the original status URL for the committed result; live delivery
does not reconstruct a successful reply from storage. A legitimately empty answer
can still succeed. Browser/progress observer failures do not fail body delivery.
Cancelling an app waiter does not cancel an admitted command.
Result-recording conflicts and unconfirmed recording remain explicit errors.
Their messages direct the caller to inspect the original conversation; they do
not offer a saving retry or establish that the operation had no effects.
The SSE observation attaches before the request is admitted. Its buffer counts
complete UTF-8 SSE frames, including the frame in the current ASGI write. The
response listens for disconnects and applies the write deadline to headers,
every frame and the end of the body. Overflow or an encoding error drops the
waiting frames and stops accepting more. An ASGI write already in progress can
finish within its existing deadline. If overflow precedes the first frame, the
response still sends the identity headers when the connection permits it.

HTTP timeouts, cancellation and disconnects end only the response observation.
The ASGI response clears the buffer even if the body never starts. JSON `ask`
waits on the original completion directly. The original completion observer
continues independently of response cleanup.
The application keeps the original execution in its active
count and conversation list until its receipt and original driver finish,
including cleanup after a failed SessionOwner. A reusable host may remain alive;
releasing the ordinary request slot does not require host exit. Admission is serialized per app
through conversation creation and turn registration, so concurrent requests
cannot exceed either the execution or conversation cap.
This is distinct from `ApplicationManager`'s build-status consumer, which also
observes the original completion, including direct chat in the build session.
Application deletion closes conversation creation and input admission before
retirement. Per-conversation retirement errors propagate to application cleanup;
they cannot be logged and skipped before files are removed. Archived conversation
rows are deleted without restoring a model. Development histories are retained;
application-owned conversations are removed. See the
[application lifecycle](applications.md#rest-surface) for failure, retry and restart.
See the [original completion contract](input-reliability.md#原请求完成与失败交接)
for the shared ownership and retirement rules. Deployment is verified separately.

Historical request status uses the shared
[session result association](input-reliability.md#原请求结果读取): ordered fragments
from legacy records are joined with two newlines without trimming the whole
saved reply, including failed outcomes. New App Server status and live replies
share the selected root items and formatting: ignore empty fragments, join with
two newlines and strip the whole reply. Conversation refresh projects those same
selected native rows; commentary and child text remain available in the full
product conversation. Invalid/missing refs return an explicit error, including
HTTP 409 on historical lookup, without borrowing other text. A valid empty
selection returns an empty reply. Live ask/SSE retains attempt resets and requires
delivered notifications. Legacy records cannot prove every lost body row.

Conversation refresh groups saved rows by their runtime request ID and a
dictionary-valued attempt context's request ID in one pass. Different IDs may
associate the same row with both requests; equal IDs contribute it once, in
original order. Null, string or array attempt context remains opaque tool input
and cannot break a valid runtime association. The common committed-result
selector still owns native final references; grouping avoids rescanning the
whole conversation for every request and adds no persistent answer cache.
`tests/test_app_agent.py` checks the public history path, constant full-history
passes, dual-ID association and opaque input compatibility.

Without a saved outcome, `running` and `queued` require the original live,
nonfailed Owner and its current execution or queue entry. A retained App turn or
run request can still hold capacity during cleanup but cannot establish running.
`waiting_for_input` reads only the current human question matching that request
and attempt. This status read neither expires a question nor acquires answer
authority; another request's question does not count.

## Original-request recovery client

Apps can import the host module from an inline module in their entrypoint:

```html
<script type="module">
  import { createAgentClient } from "./agent/client.js";
  const client = createAgentClient();
  // Call only on an explicit user action, using the normal chat request body.
  async function send(body) {
    try {
      return await client.chat(body, { onEvent: (event) => console.log(event) });
    } catch (error) {
      if (error.receipt?.request_id) showCheckStatus(error.receipt);
      throw error;
    }
  }
  function showCheckStatus(receipt) {
    const button = document.createElement("button");
    button.textContent = "Check status";
    button.onclick = async () => console.log(await client.status(receipt));
    document.body.append(button);
  }
</script>
```

`ask` and `chat` send one POST. On `response_wait_timeout` they perform at most
one GET, bounded to ten seconds, and return the original receipt with an
`observation` or `observation_error`; an unfinished observation remains available
for an explicit later `status(receipt)`. They never loop or resend the POST.
The SSE parser reads the response headers first and also recognizes the
complete initial `conversation` event from older servers. Either channel can
supply the original identity. Valid fields in both channels must agree, even if
only one channel supplies a complete pair. A mismatch or a conflicting
recovery URL raises `sse_protocol_error` without a queryable receipt. On EOF,
network failure or abort, `error.receipt` carries `operation_terminal=false` and any confirmed
identity. Without that identity the outcome remains unknown. An immutable
abort reason is retained as `cause`. The parser handles chunked UTF-8 and
LF/CR/CRLF frames and releases its reader before a timeout lookup.

Backend consumers can pass `baseUrl: process.env.OCTOPUS_AGENT_API` and
`headers: { Authorization: "Bearer " + process.env.OCTOPUS_APP_TOKEN }` to
`createAgentClient`; the supplied base works with or without a trailing slash.
The default base is relative to the served module. The client builds the
status GET from the same API base as the POST. A recovery URL in the receipt
must identify the same origin, application, conversation and request. Its mount
prefix may differ from the caller's prefix, such as when an application backend
uses loopback. The status response must confirm the original identity. The
authenticated module is served with no-store from the packaged source also
exercised by the JS tests. It needs no package publication and does not migrate
installed Apps.

<a id="3-a-conversation-is-a-session"></a>
## A conversation is a session

An app conversation is an Octopus session (`origin='app'`, with nullable
`sessions.app_id` naming its owner). It uses the shared transcript persistence,
harness resume, tools, MCP servers and event bus. The normal chat view exposes
the full conversation for inspection.

`app_id` rather than an id encoded in `origin`: it's how a route proves a
conversation belongs to the app that asked for it, how the sidebar filters
them out, and how deleting an application takes its conversations with it.

<a id="4-two-credentials-because-two-callers"></a>
## Two credentials, because two callers

The page inside the iframe already authenticates with the `octopus_app_token`
cookie, so its `fetch("agent/ask", …)` needs nothing new.

A backend script gets a **scoped token** instead:
`HMAC-SHA256(master_key, "app:<app_id>")`, handed to it as `OCTOPUS_APP_TOKEN`
with `OCTOPUS_AGENT_API` as the base URL. It opens exactly one application's
`/apps/<id>/…` surface and is useless anywhere else in Octopus. This keeps the
promise `script_env` already makes — the server's environment, and the master
token in it, never reach app code (see [script environment](application-backends.md#what-the-scripts-are-given)) — while still
letting a backend hold a conversation. It needs no storage: derived from the
master key, so rotating that rotates every app's token with it.
Signature-shape rejection and the legal account-token fallback follow the shared
[scoped authentication contract](multi-tenancy.md#keys-and-secrets). A malformed
App signature is an authentication failure, not a server comparison exception.

<a id="5-what-the-agent-is-told"></a>
## What the agent is told

The first turn of a conversation carries a preamble naming the app, then every
turn is `message` plus optional `context` (what the app wants the agent to look
at — the article, the selection, the row). Three things the preamble fixes,
because each is a way the conversation would otherwise go wrong:

* **Reply as prose.** The person is in an app, not a terminal.
* **Don't touch the app's code.** Changing the app happens in its build
  session, where it's reviewable, not through a message the app composed.
* **Work in the app's data directory.** `<slug>.data/` is the session's
  working dir, so files the agent writes land in the app's own state and
  survive a rebuild.

<a id="6-limits"></a>
## Limits

An app is a program; a program can loop. Each cap returns a specific error
rather than letting Octopus absorb the cost:

| cap | value | why |
|---|---|---|
| concurrent turns per app | 4 | bounds admitted executions and their original driver lifetimes; a turn is not always a fresh process |
| conversations per app | 50 | a thread-per-request bug is otherwise invisible |
| message | 32k chars | |
| context | 200k chars | roughly 50k tokens; beyond this, summarize first |
| `ask` wait | 300s | a real turn can be slow; a hung one shouldn't pin a socket |
| SSE buffer | 256 frames and 1 MiB | includes the frame being sent |
| individual SSE ASGI write | 10s | a stalled write ends the response |

<a id="7-ui-chrome-that-gets-out-of-the-way"></a>
## UI: chrome that gets out of the way

The top-right **Iterate** control opens a popover containing the change request,
build-session link and last error. It leaves the application page available
while the controls are closed.

Conversations started by the app are *hidden* from the sidebar rails — an app
that talks to an agent ten times an hour must not bury your own sessions — and
reachable from the same header, so "what is my app saying to my agent?" is one
click, not a mystery.

<a id="8-testing"></a>
## Testing

Backend: token derivation and scope (a token for app A opens nothing of app
B's), the full route surface incl. every cap and every 4xx, a conversation
resuming across turns, the SSE frame order (`conversation` → `delta`* →
`message` → `done`), deletion cascading, and a real-CLI test that asks a live
agent a question through the app API and reads its answer.
Frontend: the Iterate popover (opens, sends, closes, reports errors, links to
the build session) and the conversations list.
E2E: the page has no bottom bar, the popover sends a change request, and the
app's own conversations never appear in the sidebar.

`tests/test_app_reply_delivery.py` joins the real Owner, Publisher, SQLite,
JSON/SSE routes and original result lookup. It injects ordinary/cancellation
failures only in the required body callback, retains normal model events and
checks explicit errors, saved outcomes, valid empty replies, no replay and
capacity ownership after caller cancellation or stream disconnection. Missing
required body delivery must produce a delivery error. Shared completion, notification-failure and held-publication regressions
also retain ordered business delivery, caller cancellation and diagnostics
redacted with the original execution's credential context.

The same tests bound ASGI writes and verify in-flight accounting with the first
frame held while another conversation's JSON `ask` completes. The client tests
cover recovery using either identity channel and reject conflicting identities
before querying status.

`tests/test_app_agent_owner_failure.py` invokes the public route's actual ASGI
Response through header failure, pre-body cancellation, body failure and normal
completion, plus idle disconnect on ASGI 2.3 and 2.4. It checks that a closed
observation cannot send later publications while the original
execution remains counted and available through its original result lookup.

<a id="9-what-this-defers"></a>
## Current limits

* **App question UI and attachments.** The stream carries questions. A deadline
  resolves one only when that product deadline is enabled; cancellation remains
  separate, and no unconditional automatic-answer guarantee applies. App reply
  controls and attachment/material handles are conditional scope in
  [application evolution](../plans/application-evolution.md).
* **Per-app agent allow-lists.** An app can address non-archived agents of its own account by
  name. These are trusted same-account programs under the host user, within a
  multi-account product. A minimum App→Agent scope/allowlist and its policy UI
  belong to [the interface plan](../plans/octo-cli.md); Wiki client grants remain
  a separate permission boundary in [the Wiki plan](../plans/wiki-future-plan.md).

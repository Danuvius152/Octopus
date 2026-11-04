# 运行中应用与 Agent 的对话

> **Status:** implemented — 按应用令牌授权的 Agent 对话、实时返回与原执行完成。

## 2. The shape: one API, mounted where the app already lives

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

Successful replies and the initial SSE conversation event include `request_id`.
An `ask` response timeout returns HTTP 504 with structured `detail` containing
`code=response_wait_timeout`, `conversation_id`, `request_id`, `status_url` and
`operation_terminal=false`, including on a first call without a conversation ID.
SSE returns the same recovery fields in an error event. The server observes the
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
HTTP timeouts, caller cancellation and SSE disconnection detach only that
response consumer. The application keeps the original execution in its active
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
[application lifecycle](applications.md#5-rest-surface) for failure, retry and restart.
See the [architecture contract](architecture-contract.md#输入完成与资源退休)
for the shared ownership and retirement rules. Deployment is verified separately.

Historical request status uses the shared
[session result association](architecture-contract.md#原请求结果读取): ordered fragments
from legacy records are joined with two newlines without trimming the whole
saved reply, including failed outcomes. New App Server status and live replies
share the selected root items and formatting: ignore empty fragments, join with
two newlines and strip the whole reply. Conversation refresh projects those same
selected native rows; commentary and child text remain available in the full
product conversation. Invalid/missing refs return an explicit error, including
HTTP 409 on historical lookup, without borrowing other text. A valid empty
selection returns an empty reply. Live ask/SSE retains attempt resets and requires
delivered notifications. Legacy records cannot prove every lost body row.

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
The SSE parser handles chunked UTF-8 and LF/CR/CRLF frames, cancels and releases
its reader before the timeout lookup, and preserves the original identity on
EOF, network failure or abort after `conversation`. Such exceptions carry
`error.receipt` with `operation_terminal=false`; an immutable abort reason is
retained as `cause`. Failure before receiving identity cannot invent one.

Backend consumers can pass `baseUrl: process.env.OCTOPUS_AGENT_API` and
`headers: { Authorization: "Bearer " + process.env.OCTOPUS_APP_TOKEN }` to
`createAgentClient`; the supplied base works with or without a trailing slash.
The default base is relative to the served module. Recovery URLs must identify
the same application/conversation/request; status responses must confirm the
original conversation/request. The authenticated module is served with no-store
from the packaged source also exercised by the JS tests. It needs no package
publication and does not migrate installed Apps.

## 3. A conversation is a session

An app conversation is a real Octopus session (`origin='app'`, new nullable
`sessions.app_id` naming its owner). Not a parallel mechanism, because every
property we'd have to reinvent is already there: the transcript persists,
the harness resumes it so turn 5 remembers turn 1, tools and MCP servers work,
the broadcast bus already carries deltas and results, and you can open the
thread in the normal chat view and read exactly what your app has been saying.

`app_id` rather than an id encoded in `origin`: it's how a route proves a
conversation belongs to the app that asked for it, how the sidebar filters
them out, and how deleting an application takes its conversations with it.

## 4. Two credentials, because two callers

The page inside the iframe already authenticates with the `octopus_app_token`
cookie, so its `fetch("agent/ask", …)` needs nothing new.

A backend script gets a **scoped token** instead:
`HMAC-SHA256(master_key, "app:<app_id>")`, handed to it as `OCTOPUS_APP_TOKEN`
with `OCTOPUS_AGENT_API` as the base URL. It opens exactly one application's
`/apps/<id>/…` surface and is useless anywhere else in Octopus. This keeps the
promise `script_env` already makes — the server's environment, and the master
token in it, never reach app code (application-backends.md §4) — while still
letting a backend hold a conversation. It needs no storage: derived from the
master key, so rotating that rotates every app's token with it.
Signature-shape rejection and the legal account-token fallback follow the shared
[scoped authentication contract](multi-tenancy.md#keys-and-secrets). A malformed
App signature is an authentication failure, not a server comparison exception.

## 5. What the agent is told

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

## 6. Limits

An app is a program; a program can loop. Each cap returns a specific error
rather than letting Octopus absorb the cost:

| cap | value | why |
|---|---|---|
| concurrent turns per app | 4 | bounds admitted executions and their original driver lifetimes; a turn is not always a fresh process |
| conversations per app | 50 | a thread-per-request bug is otherwise invisible |
| message | 32k chars | |
| context | 200k chars | roughly 50k tokens; beyond this, summarize first |
| `ask` wait | 300s | a real turn can be slow; a hung one shouldn't pin a socket |

## 7. UI: chrome that gets out of the way

The application view carried a permanent composer bar under the page. It cost
~60px of every app forever to serve something you do rarely, and the ask was
explicit: put it away. The bar and the "Build session" button merge into one
top-right **Iterate** control whose popover holds the change request, the
build-session link and the last error. The page gets its space back.

Conversations started by the app are *hidden* from the sidebar rails — an app
that talks to an agent ten times an hour must not bury your own sessions — and
reachable from the same header, so "what is my app saying to my agent?" is one
click, not a mystery.

## 8. Testing

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
capacity ownership after caller cancellation or stream disconnection. The old
path reported success after losing body text; these cases now require a delivery
error. Shared completion, notification-failure and held-publication regressions
also retain ordered business delivery, caller cancellation and diagnostics
redacted with the original execution's credential context.

## 9. What this defers

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

## Historical evidence

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/app-agent-access.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.

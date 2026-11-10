# Connector：安装、启用与令牌归属

> **Status:** implemented — 账户所属 OAuth 安装、Agent 启用范围与类型化 MCP 工具。

## Current contract and design decisions

A connector kind describes a provider and its tools. An installation is one
account's connected external identity. `agent_connectors` enables installations
for a durable agent; each turn loads that agent's enabled installations and
renders backend-neutral MCP entries through the selected Harness. Enablement
survives conversation replacement without copying secrets into a session.

Gmail and GitHub are built-in kinds. Browser-defined custom OAuth kinds use the
generic connector and MCP module. OAuth client configuration is DB-first with
environment fallback. OAuth uses the [public base rules](#public-oauth-base)
below for its redirect URI. Typed MCP tools avoid proxying arbitrary
CLI traffic. The server stores credentials; model prompts carry capabilities and
tool namespaces, not provider tokens.

`ConnectorManager` owns token refresh and per-installation locking. A process-level
connector MCP instance may serve many installations, so cached tokens never bypass
current installation/account checks. Per-installation coordination and database
transactions protect different responsibilities: external refresh/binding versus
atomic installation-and-secret writes. See the
[account and MCP contract](multi-tenancy.md).

## Public OAuth base

The router uses a nonempty `OCTOPUS_PUBLIC_BASE_URL` before any request-derived
address. Without it, the existing request rule remains: forwarded proto before
request scheme, and forwarded host before Host, otherwise the request base URL.
This preserves remote and tunneled access without substituting localhost or
changing which forwarding headers the deployment trusts.

Settings alone normalizes the explicit URL: surrounding whitespace is removed,
empty or whitespace-only values mean unconfigured, and every other value must
parse as an absolute HTTP(S) URL without credentials, query or fragment. The
parser's canonical string is used, with trailing slashes removed after parsing.
Thus `/` or `///` is an invalid configuration, not an absent override. Legal
ports, IPv6 hosts and base-path prefixes remain supported; accepting a prefix
does not configure proxy routing. Client ID and secret contents retain their
independent existing rules and are not trimmed by URL normalization.

Setup GET/PUT and OAuth start share the same resolver and callback suffix.
Start passes the final redirect URI to the authorize encoder and captures it in
the original pending login. Callback sends that captured value to token exchange;
a later configuration change or different callback host/headers cannot replace
it. Invalid explicit configuration fails Settings validation rather than silently
choosing a request host. No additional resolver, localhost fallback or persistent
login state is maintained. Operator instructions belong to
[Connector setup](../connectors-setup.md).

The browser's [configuration draft lifetime](frontend-list-reconciliation.md#connector-配置表单实例)
owns setup reads and save tails independently of OAuth login observation.

## OAuth and lifecycle

Pending OAuth login is bounded and in memory. The fifteen-minute TTL bounds
callback admission; after a callback is claimed, its network operations have
their own deadlines. Cancellation and local binding coordinate on the same login
owner. Local binding atomically associates the external identity, installation
and secret. An external grant alone does not prove local installation succeeded.

Reconnect carries the selected installation ID from its rendered card through
OAuth start, pending login and prepared authorization. That target is transient
intent; the login's result installation ID is set only after successful local
binding. Start and callback check the current account, exact owner/kind, target
existence and captured definition. Binding checks them again after provider I/O.
An unavailable target is refused without falling back to a new installation.

Explicit Reconnect and ordinary Connect that resolves an existing external
identity share the same checked update. Under the original installation lock,
then definition lock and DB transaction, it updates that ID's metadata and
secret atomically, clears replaced reconnect errors, and preserves created time,
default enablement and Agent links. The expected provider identity must match
the stored representation exactly. Custom NULL identities use the explicit
target with a NULL-safe comparison; they are never merged by kind or label.
Connect without a target retains external-identity deduplication and can create
independent same-kind Custom installations.

The original login binding lock orders cancellation against the whole local
binding, including its wait behind an existing refresh. Cancellation before
binding does not apply the replacement authorization; once binding wins,
cancellation observes its actual result. Installation deletion shares the
installation lock, while definition deletion is rechecked under its own lock
and inside the transaction. A refused or rolled-back binding leaves no
replacement installation or partial secret update. A delayed resource rejection
still uses the [original grant comparison](#token-failures-and-delayed-rejection),
so it cannot mark a replacement authorization invalid.

Archive, delete, ownership and refresh outcomes are checked at their current
boundaries. The old second-pass audit's proposed provider revocation, host-login
policy and other mechanisms must not be inferred from this contract; remaining
proposals are [separately scoped](../plans/multi-tenancy-gaps.md).

A custom definition has an opaque process-local identity owned by the manager.
Pending login retains the original connector captured before the first client
configuration await. Authorization and callback validate that identity and read
its matching client configuration in one short definition lock before contacting
the provider. A deleted definition is refused before sending a replacement's
client secret to the old endpoint. Network I/O holds no definition lock; a
definition deleted while I/O is in flight is refused again at local binding.

Definition deletion atomically removes its OAuth client and all accounts'
installations, secrets and agent enablement links. Creation, deletion, binding
and independent client set/clear operations share the short local ordering;
deletion retires the in-memory identity only after confirmed DB deletion, even
when its waiter is cancelled. Delete/recreate under the same slug gets a different
identity, so old callbacks and client writers cannot attach to the replacement.
A binding that commits first is removed by deletion, while its historical
successful login receipt remains successful. Built-ins retain their registry
identity and need no custom-definition row.

The process-local representation is sufficient because this installation has one
manager, pending logins are not persisted and account transfer is offline. It
adds no storage field or migration; it does not coordinate independent writers
in multiple server processes.

OAuth state, expiry and single callback claim do not currently bind the callback
to the initiating browser's cookie. Disconnect deletes the local installation,
without promising to revoke the provider grant. Browser binding and revocation policy remain in
[the account plan](../plans/multi-tenancy-gaps.md), rather than being claimed as
current guarantees. Complete long-result access, sensitive-write authorization,
health checks and per-tool policy have their own
[capability plan](../plans/connector-capabilities.md).

## GitHub identity

GitHub identity is the positive numeric `id` returned by the actual `/user`
response, stored as canonical ASCII decimal text. Missing, boolean, noninteger
or nonpositive IDs are refused before local binding. `login` supplies only the
display label; it is never part of the installation's identity key.

Within an Octopus account, ordinary Connect after a GitHub rename resolves the
same installation and explicit Reconnect accepts the same numeric ID. Both
update the label and grant while preserving the installation ID, created time,
default enablement and Agent links. Explicit Reconnect with a different provider
ID is refused without replacing the original authorization; ordinary Connect
may create that distinct identity. Provider identity does not merge installations
belonging to different Octopus accounts.

Current main-format constraints and publication verification require this one
representation. All interpretable historical `login:id` keys must cross the
[finite offline handoff](current-storage-format.md#github-identity-and-finite-publication-handoff)
before opening the new writer, including rows never actively reconnected.
Conflicting or unknown source identities block publication for an explicit
decision. Startup, ordinary reads, Connect and account import neither guess an
old-key suffix nor rewrite legacy rows online.

## Browser OAuth operations

`ConnectorOAuthOperation` owns one login, independently of the dialog that
started it. ConnectorsPage creates and retains the operation before sending
start. It captures the original token, account generation and service identity.
Closing during start, unmounting, replacing the operation, or changing that
identity permanently removes its view rights. Reopening, account ABA or service
identity ABA cannot restore them. A retired operation cannot open a tab, resume
the waiting UI or close a newer operation's dialog.

The original operation still consumes a late start response. With a login ID,
it cancels using its original token and consumes the actual server receipt.
Waiting-stage close shows cancellation in progress until that receipt; a success
that won the binding lock is still success. A normal cancellation receipt already
settles local cancellation, so there is no additional confirmation phase.
Provider authorization remains separate from local cancellation.

Only start, status and cancel use the narrow OAuth HTTP adapter, which can
consume original-account receipts after the browser changes identity. All other
Connector requests retain `accountFetch` admission. Server authentication and
login ownership checks remain authoritative. Each OAuth request has a 30-second
deadline including response-body consumption. Retirement interrupts the current
status read and its timer, while start remains observed until its response or
deadline because that response may contain the only login ID. Active status
reads run at 1.5-second intervals within a five-minute polling window. An
in-flight status request retains its independent 30-second deadline. If the
login remains unsettled when that request finishes, expiry is checked before
another poll and requests local cancellation.
A cancelled receipt retains the timeout explanation, while success, error or an
unknown outcome is reported as received.

Start and cancel are never automatically replayed. A lost start response without
an ID has no recovery operation in the current API and is not proof that no login
was created. Start/cancel transport failure, 401/404 and local timeout are unknown
outcomes, not cancellation success. Failed status reads continue polling within
the original deadline and do not settle the login. An attached dialog reports
start/cancel uncertainty and asks
the user to inspect connections before starting again; a retired view receives
no new status UI. These browser owners are finite and in memory, with no cleanup
queue or persistence across a page reload.

Success, terminal error and unknown outcomes with a known login ID ask the
installation entry to observe current facts under the original account scope.
Error can mean local saving was unconfirmed, so it does not prove installation
absence. Leaving the view does not discard an applicable observation; changing
accounts prevents it from committing into the new account. List synchronization
failure remains separate from the login result.

## Browser installation observations

The management page, sidebar and completed OAuth flows share the finite
`connectorInstallations` business entry. Confirmed disconnects and custom
definition deletion remove the affected current-account installations and retire
overlapping reads before a follow-up GET. Zustand remains the single displayed
list, including the sidebar count and Agent settings' installation choices.
Read failure retains known rows and reports a synchronization error; it does not
reverse an already confirmed connection or deletion. Catalog refresh stays with
the page. Full admission, account, timeout and view-lifetime rules belong to the
[frontend installation contract](frontend-list-reconciliation.md#connector-安装列表).

## Refresh ownership and shutdown

After each caller's current account/installation check, the manager registers a
strongly held token task before awaiting it. Same-installation callers share that
task through shielded waits; cancellation detaches a waiter while the original
task continues provider I/O, save and exception observation. Other installations
can progress independently. Each waiter independently checks installation
ownership before joining and account availability again after the result;
joining shared work grants no authority.

The original installation lock spans refresh and save, serializing reauthorization
and installation deletion. Inside it, a short definition lock captures the
installation, encrypted grant, endpoint and matching client configuration
together, then releases before network I/O. Definition deletion can proceed
during that I/O. Save checks current account and installation authority, so a late
result neither recreates a deleted installation nor replaces a new authorization.
The lock order is identity deduplication, installation, definition, then DB where
those responsibilities are needed; deletion never waits back on an installation.

Task completion observes failures with credential-redacted diagnostics and retires
the original task from the manager's set. Shutdown closes admission and waits up
to 30 seconds without cancelling an owned rotation. A timeout leaves outstanding
tasks enumerable and fails close; an unexpected save failure observed during
shutdown also fails close. The DB pre-close hook joins this responsibility before
storage closes, and installation drain includes these operations. This preserves
responses received by the current process; a provider response never received or
a hard process kill still has unknown remote effects and triggers no automatic
rotation retry.

## Token failures and delayed rejection

The refresh producer preserves HTTP status and machine error code, including
non-JSON error pages. Transport failure, 429 and 5xx are temporary; 5xx remains
temporary even with an `invalid_grant` body. Only a 400/401 `invalid_grant`
establishes a permanent grant rejection. Configuration, permission, unavailable
installation/account and unknown failures remain distinct through manager,
internal route and MCP consumer. A temporary failure does not mark authorization
invalid; a later call may retry refresh. Successful refresh preserves an omitted refresh token
and clears prior error state.

One token resolution captures access token, expiry, API base and an opaque
`grant_stamp` under the installation lock. The stamp hashes the owned installation,
authentication type, encrypted material and endpoint; it is comparison evidence,
not a token or execution grant. Gmail, GitHub and custom consumers keep that
snapshot local to each call. A resource 401 submits its stamp in a JSON rejection
request. The original DB transaction compares current account, installation and
material: `applied` marks the current grant, `obsolete` preserves a replacement,
and `gone` reports its absence/unavailability. Unconfirmed marking has its own
message; none of those latter cases falsely asks the current login to reconnect.
Only the first applied state change emits the reconnect-needed metric; every
resource rejection remains a call-failure observation.

Authorization rejection does not replay a resource operation. For custom APIs,
the grant receipt settles authorization state only: an arbitrary upstream 401
cannot prove that the business write did not take effect.

## Resource HTTP operations and unknown effects

GitHub, Gmail and custom tools declare retry safety at the operation boundary;
the shared HTTP executor owns attempts, delays and the resource I/O budget.
Typed reads and custom GET/HEAD/OPTIONS can retry 429/5xx responses, with at most
three attempts including the first. Transport errors retain their direct-failure
behavior. Current writes, including Gmail label changes and custom PUT/DELETE,
have no enabled guarantee of safe replay and therefore make only one attempt.
This does not assert that those providers' operations are necessarily
non-idempotent; no sufficient remote deduplication contract has been established
for the current tools. Local UUIDs and HTTP method names do not establish one.

An uncertain write returns `outcome_unknown`, a bounded description of the
original operation, its reason and a concrete check target. The caller must
check the original result before considering another write. This covers 429,
5xx, transport failures and unproven error responses. Built-in validation,
permission and missing-resource rejections use their documented provider
semantics; an arbitrary custom 4xx or redirect is still uncertain. Custom 401
also keeps the original authorization receipt, and no provider redirect is
followed. A missing Gmail draft alone does not prove that mail was delivered.
There is no automatic replay queue or new persistent operation state.

Each provider resource HTTP call has one 30-second budget, including network
I/O, response-body reads, every attempt and all backoff. Async tools use the
host loop and cancel resource I/O at the deadline; closing a per-call event loop
could instead wait for outstanding DNS threads. The executor checks a monotonic
deadline before another attempt and after waiting. `Retry-After` accepts seconds
or HTTP-date and is never shortened to fit. If the required wait exhausts the
remaining budget, the tool returns the reason without waiting or resending.
Safe reads otherwise use bounded exponential backoff. GitHub 403/429 rate limits
also respect the primary reset time; secondary limits without a usable hint
require at least a minute and thus exceed this budget. Ordinary GitHub permission
403 remains distinct. HTTP-date/reset conversion uses wall time, separately from
elapsed-time accounting.

The budget belongs to a resource HTTP call, not an entire multi-request tool:
Gmail search can make separate list and metadata calls. Host token and rejection
callbacks keep their existing independent timeout and execute off the host loop;
OAuth refresh ownership remains as described above. Cancellation of a waiter or
resource I/O does not prove that an external write was cancelled.

The retry policy follows [HTTP replay semantics](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2)
and [Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3), with
[GitHub's rate-limit rules](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#exceeding-the-rate-limit)
and [Gmail's error semantics](https://developers.google.com/workspace/gmail/api/guides/handle-errors).
General provider retry advice is not evidence of safe write replay. The current
[GitHub issue](https://docs.github.com/en/rest/issues/issues#create-an-issue),
[comment](https://docs.github.com/en/rest/issues/comments#create-an-issue-comment),
[Gmail draft creation](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/create),
[draft lifecycle](https://developers.google.com/workspace/gmail/api/guides/drafts)
and [label modification](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/modify)
contracts do not establish the reliable replay capability required here.

## Gmail reply drafts

`create_draft.in_reply_to_message_id` is a Gmail message ID returned by
`search`/`get`. The tool reads that message's metadata before creating a draft;
callers do not supply RFC identifiers or construct the reference chain. The
returned Gmail message ID must match the lookup, `threadId` must be present,
and the original RFC Message-ID and Subject must be available. Missing or invalid required
metadata, failed reads and conflicting subjects stop before the create request.
Ordinary drafts retain the existing recipient, subject and body arguments.

A reply submits `message.threadId` together with MIME `In-Reply-To` containing
the parent's RFC Message-ID. `References` retains the parent's reference history
and appends that ID. If there is no history, a parent In-Reply-To containing one
valid identifier is retained before appending the parent ID. A multi-parent
In-Reply-To is not substituted for reference history. Header names are case
insensitive; legal continuation lines are unfolded, and bare CR/LF is refused.
Received legacy ID/display-name syntax remains usable, while actual parse errors
and damaged reference history fail before creation.

An omitted reply subject inherits the original. An explicit subject must match
it exactly; the tool does not assume Gmail normalizes a changed subject or a
new `Re:` prefix into the same thread. Explicit `to` always wins. If omitted,
the tool selects the original Reply-To, otherwise From, and requires at least
one mailbox. An empty address group fails before creation; an unusable Reply-To
does not fall back to From. Original To/Cc/Bcc are never copied into the reply. Standalone drafts
require `to`; an omitted subject is empty. Creating a draft does not send mail,
and `send_draft` retains its existing confirmation instructions.

These rules implement [Gmail's thread requirements](https://developers.google.com/workspace/gmail/api/guides/threads)
and the received-header/reply rules in [RFC 5322](https://www.rfc-editor.org/rfc/rfc5322.html#section-3.6.4).
References validation uses the stdlib msg-id tokenizer inside the Gmail codec:
[Python 3.12](https://raw.githubusercontent.com/python/cpython/3.12/Lib/email/headerregistry.py)
does not parse References as an ID list, so relying only on header diagnostics
would silently vary across supported Python versions. The narrow dependency
reuses the existing grammar and accepts comments/quoted IDs rather than splitting
on spaces or angle brackets. It adds no provider retry or compatibility state.

## Navigation and verification

[Connector setup](../connectors-setup.md) owns operator instructions. The
[architecture overview](../architecture.md#accounts-agent-configuration-and-providers)
explains component relationships. Exact route/schema inventories come from
FastAPI/OpenAPI and the code:
[manager](../../server/connector_manager.py), [providers](../../server/connectors/),
[routes](../../server/routers/connectors.py),
[MCP modules](../../server/mcp_servers/connectors/).

Regression entry points are `tests/test_connectors_oauth.py`,
`tests/test_router_connectors.py`, `tests/test_connectors_db.py`,
`tests/test_connectors_registry.py` and `tests/test_backend_connectors.py`.
They do not prove current external-provider access.
`tests/test_connector_grant_failures.py` additionally joins the real internal
routes, Context, three consumers and SQLite across provider HTTP failures,
authorization replacement, deletion/disable and concurrent calls sharing Context.
`tests/test_connector_manager.py` covers owned refresh across cancellation before
response, at the save lock and during COMMIT, sharing, shutdown and real save
failures. The previous waiter-owned path lost a received rotation when its caller
left; regressions now reopen SQLite and require the saved original grant.
`tests/test_connector_custom_lifecycle.py` and the router tests cover deletion,
rollback, binding order, same-slug replacement and stale client writers. Explicit
NULL-identity reconnect also goes through enabled-Agent scope and the actual
Custom MCP resource call, including a delayed rejection of the replaced token.
Rendered same-kind installation cards verify which target the start request
carries; ordinary Connect omits it.
`tests/test_connector_definition_http.py` uses two loopback providers with different
endpoints and secrets, through the real HTTP exchange/refresh implementation:
stale callbacks send nothing, in-flight old requests use only old credentials,
and their late results cannot bind or save a replacement. These are local protocol
and persistence checks, not live OAuth grants.

The [original design](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/connectors.md)
contains the superseded per-session model, Notion proposal and implementation
phases. Those are historical evidence, not outstanding work or current contracts.

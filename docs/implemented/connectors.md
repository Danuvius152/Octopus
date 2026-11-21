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

## Agent binding writes

An Agent binding read returns the committed installation IDs and any pending
binding operations for that Agent. A toggle reserves one proposed value for
the account, Agent and installation. Applying the reservation requires both
its operation ID and the original intent ID. The manager checks ownership and
serializes binding reads, writes and settlement. The committed binding and
the applied receipt are recorded before releasing that boundary even when the
HTTP waiter is cancelled.

Settlement reports what happened to that operation and separately reads the
current binding. It can retire a pending reservation before its write reaches
the boundary. Applied intents return their original disposition on retry.
Expired, retired or unavailable reservations cannot write. Whole-list
replacement and the single-binding API without an operation ID retire affected
pending reservations after their own write. An ordinary read gives the
committed value at that instant.

The installation process retains each reservation for 120 seconds. The first
recording of an applied or retired disposition starts a new 120-second period.
Reading an existing disposition leaves that deadline in place. The limit is
4,096 retained operations per account. SQLite owns
the saved Agent links. Restart releases the process-local reservations and
retains those links. The form's read and retry rules are described in
[Agent form instances](frontend-interaction-state.md#agent-表单实例).

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

The browser's [configuration draft lifetime](frontend-interaction-state.md#connector-配置表单实例)
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

Archive, delete, ownership and refresh outcomes are checked at their actual
boundaries. Remaining account-policy gaps are
[separately scoped](../plans/multi-tenancy-gaps.md).

A custom definition has an opaque process-local identity owned by the manager.
Pending login retains the original connector captured before the first client
configuration await. Authorization and callback validate that identity and read
its matching client configuration in one short definition lock before contacting
the provider. A deleted definition is refused before sending a replacement's
client secret to the old endpoint. Network I/O holds no definition lock; a
definition deleted while I/O is in flight is refused again at local binding.

Creation, deletion, binding and independent client set/clear operations share
the short local definition lock. Creation encrypts the client secret before the
write and commits the definition and OAuth client in one transaction. The create
response reports the definition and its OAuth availability from that commit.
A rejected write rolls back both records so the operator can retry the same kind.
Cancelling the lock wait prevents admission. An admitted creation settles before
waiter cancellation returns.
Storage or commit uncertainty follows the [storage failure policy](current-storage-format.md#事务与失信边界).

Definition deletion atomically removes its OAuth client and all accounts'
installations, secrets and agent enablement links. It retires the in-memory
identity after confirmed DB deletion, even when its waiter is cancelled.
Delete/recreate under the same slug gets a different identity, so old callbacks
and client writers cannot attach to the replacement.
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

The [current storage format](current-storage-format.md) requires canonical
numeric provider identities. Conflicting or unknown source identities block
publication for an explicit decision. Startup, ordinary reads, Connect and
account import neither guess an old-key suffix nor rewrite legacy rows online.

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
`connectorInstallations` business entry. Confirmed disconnects remove their
current-account targets and retire overlapping reads before a follow-up GET.
Zustand remains the single displayed installation list, including the sidebar
count and Agent settings' installation choices. Read failure retains known rows
and reports a synchronization error; it does not reverse a confirmed operation.

The separate `connectorCatalog` entry owns definition reads, creation,
configuration and deletion. It publishes the single catalog array and passes
confirmed deletion effects to the installation entry. The catalog distinguishes
an old deletion superseded by an observed same-slug creation from a new deletion
of that replacement. The superseded receipt only requests current catalog and
installation confirmation, preserving the new definition and installations;
independent deletion targets still apply their own results.

Saving and subsequent synchronization have separate results. The page retains
successful writes when confirmation fails and offers a read-only refresh; form
lifetime does not decide shared-list publication. Full admission, account,
timeout and view-lifetime rules belong to the
[frontend directory](frontend-list-reconciliation.md#connector-目录观察) and
[installation contracts](frontend-list-reconciliation.md#connector-安装列表).

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

Each resource HTTP call has its own budget. Gmail search can make separate list
and metadata calls, and [Gmail message reads](#gmail-message-body) also have an
overall deadline. Host token and rejection callbacks execute off the host loop
with their independent timeout. OAuth refresh ownership remains as described
above. Cancellation of a waiter or resource I/O does not prove that an external
write was cancelled.

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

## Gmail message body

`get` uses the `format=full` MIME structure to select the message body. Mixed
parts contribute eligible text in order. For alternatives, it prefers plain
text, then HTML, then other text. It skips known-empty parts. If the part that
gave a branch its priority decodes to empty, all enclosing alternatives are
compared again before more data is read. An inline check that can rule out a
branch takes precedence when it fits the available budget. Selection skips
malformed MIME branches when a valid nonempty alternative exists. Related
parts contribute only the root identified by `start`, or the first child when
`start` is absent.
A declared `start` must match exactly one direct child, and that root's own
disposition and filename are ignored. Other parts marked as attachments or
carrying filenames, binary parts and enclosed messages are skipped. HTML is
returned as text. Each selected part is decoded from base64url using its
declared charset, with UTF-8 as the default. Returned bytes are checked against
any declared size on their resource before the decoded text is accepted.

The budget allows up to 256 KiB of body data before charset conversion and at
most four external reads across all attempted branches. The plan reserves
budget for confirmed required parts and the minimum cost of outstanding
choices. It picks a check that fits the remaining byte and request budget. A
branch's remaining parts enter the preflight once the choice is confirmed.
Actual decoded byte lengths consume the budget even if a branch produces no
text. The initial
`format=full` JSON response supplies the MIME tree. Each required external GET
obtains a fresh host token and streams a bounded JSON response. HTTP status
and retry headers are handled before reading its data. The existing three
attempts per resource allow up to 15 provider HTTP attempts across the message
GET and its external reads. A
50-second tool deadline covers the message GET, the subsequent reads and their
authorization callbacks, while each resource retains its own 30-second I/O
budget.

The tool returns the body only after all selected parts are available and
decodable. Empty bodies succeed with an empty string. External read, decode and
budget failures return errors. The message resource accounts for its MIME
projection, including inline data selected after an external read. Each
external read accounts for its own time and cancellation. Successful output
uses the shared result truncation. Controlled HTTP regressions in
`tests/test_connector_gmail_mcp.py` cover selection, budgets, deadlines and
authorization. The byte fields and related root rule follow
[Gmail's MessagePartBody](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments)
and [RFC 2387](https://www.rfc-editor.org/rfc/rfc2387.html#section-3.2).

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

## GitHub file paths

`get_file` validates the original repository as two nonempty, non-dot segments
and the original path as repository-relative segments without empty or dot
components. An entirely empty path still lists the repository root. Each segment
is then percent-encoded separately; separators remain structural, while spaces,
Unicode, `#`, `?` and literal percent signs name the requested file. Input is
not URL-decoded first: literal `%2F` and `%2e%2e` remain filename text. `ref` is
a separate query parameter and retains its branch/tag/commit meaning. Other
GitHub tools and the authorization/retry policy above retain their own behavior.

## Logical resource monitoring

Each built-in provider wrapper captures installation, session and monotonic
start time before token preparation. It emits one final `connector_call` sample
with `unit=logical_resource_v1` after token preparation, all internal HTTP
attempts/body reads and the resource projection needed by the tool. Gmail's
list/detail and reply-metadata/create calls are separate resources, even within
one MCP tool. Token success and intermediate authentication/retry failures do
not emit additional samples. Local input rejection before a resource attempt
does not manufacture a resource sample; configured API preparation failure does.

Success requires decoding the fields actually consumed, including list
references, base64/MIME data and conditional reply fields. Explicit reply `to`
does not require an inferred original recipient. An explicit subject conflict
is local input rejection after a successful metadata GET, with no POST sample.
Pure formatting and truncation stay outside this boundary; custom's legitimate
text fallback, including text starting with `Error`, remains a success.

Expected failures have finite codes: token, auth, configuration, http, budget,
retry_exhausted, transport, json_decode, resource_decode, cancelled and internal.
HTTP rejection, insufficient wait budget and exhausted safe attempts are distinct.
Expected parse failures use a narrow decode exception; unrelated programming
errors propagate and record internal. Cancellation records cancelled and
propagates. A successful write response that cannot be decoded still preserves
the operation/check and unknown-effect instructions above; it never triggers a
write replay. Cancellation likewise does not prove external effects were undone.

`server/monitor/query.py:connectors` owns aggregation for report, overview, CLI
and Monitor. `n`, `ok_n`, success rate, duration and `last_seen` use only the exact
new unit. Unitless historical failure samples remain separately visible as
`legacy_failure_samples`; no historical successes or logical request totals are
inferred. A legacy-only connector retains its row with zero new requests and
unknown rate, duration and resource timestamp. New-unit timestamps cannot be
advanced by later legacy events. Overview counts and mixed error rankings remain
event samples, with matching API/CLI/page labels, rather than resource totals.

Metrics retain the existing bounded, lossy best-effort queue and event storage;
recording failure cannot change a tool result or replay a resource request.
Samples contain identities, elapsed time, outcome and error category, without
tokens, callback URLs, request bodies or resource content. They describe retained
observations rather than a complete availability audit. No DDL, new persistent
root or compensation worker is introduced; storage and account-portability
ownership remain unchanged.

`test_connector_metrics.py` joins real provider wrappers, token HTTP,
MetricsStore/query, authenticated API and CLI with controlled provider transport.
It covers recovered success, retries/budgets, malformed write responses,
resource decoding, cancellation and storage faults. `MonitorPage.test.tsx`
checks new/legacy presentation, and browser Monitor coverage consumes the same
headings and report. These checks do not establish current external-provider access.

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
failures. Refresh ownership survives waiter cancellation; regressions reopen
SQLite and require the saved original grant.
`tests/test_connector_custom_lifecycle.py` and the router tests cover creation
and deletion, rollback, cancellation, commit uncertainty, binding order,
same-slug replacement and stale client writers. Explicit
NULL-identity reconnect also goes through enabled-Agent scope and the actual
Custom MCP resource call, including a delayed rejection of the replaced token.
Rendered same-kind installation cards verify which target the start request
carries; ordinary Connect omits it.
`tests/test_connector_definition_http.py` uses two loopback providers with different
endpoints and secrets, through the real HTTP exchange/refresh implementation:
stale callbacks send nothing, in-flight old requests use only old credentials,
and their late results cannot bind or save a replacement. These are local protocol
and persistence checks, not live OAuth grants.

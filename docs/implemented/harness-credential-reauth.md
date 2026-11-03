# Harness 凭据刷新与重新授权

> **Status:** implemented — 原 owner 负责主动刷新和拒绝处置，重新授权保留凭据身份。

## 2. Scope

In: detect an attributed auth-credential rejection, conditionally mark the
adopted authorization, surface it (chat error + sidebar badge), and let the user
re-authorize **in place** (same credential id, so all agent/session
bindings survive) which clears the flag.

### Explicit Claude credentials

For direct Claude/Anthropic-compatible CLI calls, an explicit credential fixes
the chosen API key or OAuth token in both the child environment and a per-process
settings overlay. Competing key/token variables, the API-key descriptor source,
`apiKeyHelper` and `ANTHROPIC_CUSTOM_HEADERS` are disabled for that invocation;
arbitrary custom headers can carry competing authentication. The endpoint,
model mapping, transcript home and other host settings remain in use. A call
without an explicit credential retains the complete host authentication setup.
Managed policy and outer cloud-provider selection retain their native authority.

Renderers return an in-memory private-settings specification without filesystem
effects. After authorization, the engine creates a private directory and a mode
0600 settings file; only its path enters argv. The streaming process owns it
across completed turns and releases it after confirmed process-group retirement.
One-shot retirement owns the same cleanup, including cancellation, late startup
handles and I/O failures. Partial writes and definite spawn failures roll back
their files. Host configuration and stored credential identity are never changed.

Regression coverage checks both renderers and credential types, default host
behavior, subprocess file lifetime and failure cleanup. Live invalid-key checks
require authentication rejection; a timeout or generic transport failure is not
passing evidence. See [Claude authentication precedence](https://code.claude.com/docs/en/authentication#authentication-precedence)
and [settings environment precedence](https://code.claude.com/docs/en/env-vars#precedence).

### Proactive refresh ownership

Secret-backed OAuth bundles share one in-flight resolution per account and
credential in the SessionManager. Different credentials progress independently.
Cancelling a waiter leaves the provider operation and its database write owned
by the manager; database shutdown closes resolution admission and joins these
operations before retiring the connection. The provider has one bounded HTTP
request (20 seconds total, including a slow streaming response); subsequent
calls, rather than an added retry loop, retry temporary failures. The five-minute
refresh leeway triggers renewal at entry, while resolution after a provider
result accepts any currently valid token, including a shorter-lived renewal.

The database commits a refresh only while the captured account, backend,
authentication type, ciphertext and active execution authority still match.
Reauthorization and key updates replace the ciphertext, so late success or
failure cannot overwrite the new secret; deletion, revocation, account disable
and ownership changes also reject the old result. A rejected write resolves the
current authorized state, or reports that it is unavailable. It never returns
the old provider result as a new authorization.

The provider reports structured `OAuthRefreshError` classifications. A 400/401
OAuth `invalid_grant` requires reconnect; a missing refresh token also cannot
refresh. Network errors, timeouts, 5xx, other HTTP failures and unknown exceptions
fail the current call with their exception context while retaining retryable
credential state. Error text does not decide persistent invalidation. Directory
credentials and the reactive CLI rejection path retain their separate contracts.

Regression evidence (2026-10-27): the resolver, provider and reauthorization
modules passed 179 hermetic cases with temporary SQLite and controlled HTTP,
including shared refresh, independent progress, late results, cancelled waiters,
short-lived renewals and total deadlines under slow streaming responses.
This does not establish a live Claude result.

## 3. Detection lives behind the harness contract

Per the harness-layer rule (no `if backend ==` outside `server/harness/`),
each backend declares how *its* CLI reports an auth-credential rejection:

- `RuntimeProfile.auth_error_patterns: tuple[str, ...]` — lowercased
  substrings. A turn-failure whose combined error text contains any of
  them is an auth-expiry.
- `Harness.is_auth_error(text, *, code=None) -> bool` — accepts a trusted
  normalized auth code first, then uses the profile's case-insensitive text
  patterns when enabled. Pure; no I/O.

Claude and Codex exec patterns are intentionally specific phrases ("invalid authentication
credentials", "invalid x-api-key", "401 unauthorized", "invalid_grant",
…) rather than a bare `401`, and detection is **gated on the turn having
failed** (see §4), so a tool that merely *returns* a 401 to the model
(which then continues to a normal result) never trips it.

The ordinary App Server profile disables text-pattern auth detection. Its driver
accepts only typed provider-auth failure from the exact bound root terminal.
RPC, child/tool 401 and arbitrary stderr retain diagnostics without invalidating
the credential. Native retry/error notifications are diagnostic even if another
cause later fails the root; they do not accumulate as terminal auth evidence.
See [native error source and recovery](codex-backend.md#error-source-and-recovery).

## 4. Run-loop wiring (`server/sessions/turns.py`)

The original Owner classifies the failed attempt using its actual selected
profile. It passes the terminal error code and, where that profile enables text
matching, attributed terminal/preparation diagnostics. App Server's
typed bound-root code is its auth evidence; native retry/child notifications
and arbitrary stderr cannot supply it. Resource retirement remains owned by
the original execution.

- A turn is *failed* when its terminal `result` is an error, or it ended with
  no `result`. Native diagnostic notifications do not select a failed result.
- On attributed rejection, stop recovery and compare the attempt's actual
  `CredentialUse` with the current owned row inside the original DB transaction.
  The resolver derives this non-secret evidence from the exact encrypted
  material, account, backend and auth type it decrypted. A held process keeps its
  adopted evidence; later attempts capture their own. Labels and status are not
  authorization material, and provenance is excluded from process reuse signatures.
- `applied` marks that current authorization `needs_reconnect` and emits
  `auth_expired`. Replacement or reauthorization yields `obsolete`; a missing,
  foreign or unavailable account/credential yields `gone`. Those outcomes emit
  `auth_failed` without changing the current login or asking it to reauthorize.
  Unproven adoption and host-default authentication use that same conservative
  failure. Trusted provider rejection remains actionable even if a later frame
  damages collection; collection damage never supplies authentication evidence.

A Codex home path cannot identify what the independent CLI loaded, cached,
reloaded or refreshed. Its filesystem publication and DB reauth update are
separate effects. The current adapter therefore does not manufacture adoption
evidence from that path or a post-failure read: a typed root unauthorized stops
with `auth_failed`, preserving the current login. This is a deliberate limit,
not a claim that the provider accepted the credential.

### Process diagnostics

Unidentified held-process stderr can arrive during idle or a later request.
Clearing a buffer, offsets and read time do not establish attempt ownership, so
that text cannot classify authentication, resume or transient failure. An exit
without a terminal reports a backend/process failure and points to server logs.
Current attributed structured errors and dedicated preparation failures retain
their existing classification.

Run reads stderr in bounded chunks with incremental UTF-8 decoding. Its masked
tail is at most 16,384 characters; the existing INFO logger receives the complete
stream in bounded pieces. Streaming masking retains secret-prefix, quoted-value
and URL-userinfo state across chunk boundaries, including arbitrary separating
whitespace. Supplementary activity parsing stays bounded and grants no business
authority. The checked installation uses the existing journal sink at INFO with
systemd rate limiting disabled; retention follows that journal's policy, not a
new diagnostic store. These facts do not promise indefinite log retention.

Detection reads the raw `HarnessEvent` stream directly (terminal event
`content`/`raw` captured in the loop), not the WS/persistence mappers — so
classification does not depend on frontend rendering or duplicate error bubbles.

### 4a. Don't wait out the CLI's own 401 retries

The Claude CLI retries a failed API call by itself and announces each
attempt as `{"type":"system","subtype":"api_retry", "error_status":…}`. For
429/5xx that's exactly right — stay out of its way and let the terminal
failure fall through to the transient-retry path.

A **401 is not retryable**: the credential is rejected, and the tenth attempt
will be rejected too. But the CLI backs off exponentially across 10 attempts,
so the turn sits there for roughly ten minutes — long enough that the idle
watchdog trips first and the user gets "the turn timed out" instead of "your
key was rejected".

So `ClaudeEventParser` treats an `api_retry` carrying `error_status: 401` (or
`error: authentication_failed`) as fatal: it emits an `error` event worded to
match `_CLAUDE_AUTH_ERROR_PATTERNS` and ends the stream. §4's wiring then runs
on the spot — credential flagged, re-authorize prompt surfaced — about a
second after the first rejection instead of ten minutes later. Covered by
`test_parser_api_retry_401_is_a_fatal_auth_error` (and its transient-stays-quiet
sibling) plus the real-CLI
`test_real_credential_with_bad_key_yields_auth_error`, which previously timed
out waiting for the retry storm to end.

## 5. Re-authorize in place + clear-on-success

The durable affordance mirrors connectors: the sidebar credential shows a
"needs reconnect" badge + "Re-authorize" button. Re-auth re-runs the same
login flow but targets the **existing** credential id, so bindings stay
valid and the flag clears.

- **Claude** (`POST /credentials/oauth/complete`): optional
  `credential_id`. On first completion, the target must belong to the login's
  account and use the Claude backend before the code is exchanged. Storage checks
  the target owner and current account availability under the same write lock as
  the in-place update. It atomically writes `auth_type=oauth`, returned material,
  expiry and cleared reconnect/status fields without changing the credential ID.
  Both old API-key and OAuth records can receive a minted key or token bundle;
  resolver and renderer interpret the committed OAuth representation. A
  target deleted or adopted before commit returns 404 without replacing its
  secret. First-time creation checks current account availability in its commit
  transaction too. Repeated matching completion reads the
  original operation result before revalidating mutable target existence:
  deletion after success does not erase that historical result or recreate a row.
- **Codex** (`POST /credentials/codex/start`): optional
  `reauth_credential_id`. The permanent `codex_home_for(reauth_credential_id)`
  path and credential ID stay unchanged. The login CLI writes into a fresh
  staging home under the same home root; only after successful exit and auth
  validation does Octopus atomically replace the existing `auth.json` with a
  mode-0600 file. Failure, cancellation and launch errors remove only this
  staging home, preserving existing authorization, configuration and native
  sessions. The status route serializes persistence for each login and checks
  the target account again on update. Repeated success reads return the owned
  credential; deletion produces an explicit error without recreating the row.
  Only a new sign-in inserts a credential.
- **API key** (`PATCH /credentials/{id}` with a new secret): clears the
  flag too, so re-pasting a fresh key recovers an `api_key` credential.

A brand-new login already creates `status=active`, so no change there.

### Claude OAuth operation ownership

The login driver accepts one immutable completion request containing account,
login ID, authorization code, label and optional reauth target. OAuthLoginManager
registers and strongly retains its only task before target validation can await.
That task owns exchange, API-key mint (or the scope-missing OAuth bundle), and
the narrow `OAuthCredentials` writer. One database transaction saves secret,
expiry and credential state. Success follows that commit; the router performs
no subsequent persistence. Matching repeated calls wait for the same task/result;
conflicting parameters return 409 and another account sees no such login.

| Observation or action | Result and retirement responsibility |
| --- | --- |
| HTTP completion waiter disconnects | Only that wait ends; the original task continues and blocks drain. |
| Explicit cancel before committing | Revoke subsequent stages first, cancel provider work once, then wait for actual retirement; a late exchange cannot mint or commit. |
| Explicit cancel after committing begins | Keep the only committer alive and wait for its actual result. A cancelled cancel-waiter does not withdraw it. |
| `POST /credentials/oauth/cancel` succeeds | HTTP 200 with `status=cancelled` or `already_completed`; errors and unconfirmed retirement are explicit failures. |
| Retirement exceeds five seconds | Return 503 and retain the original task/blocker; repeated observation can confirm its later result. |

Cancellation does not undo provider effects already sent. The check and transition
into committing have no await between them. Task exceptions are observed; TTL
cleanup removes only retired records. Shutdown seals admission and joins the
original tasks before releasing storage. Login operations are process-local and
are not resumed across restart; completed credentials remain in the normal list.
Storage uncertainty follows the existing database health boundary, without replay.
`tests/test_oauth_operation.py` covers stage barriers, lost waiters, deduplication,
GC/shutdown and real route-to-SQLite commits; reauth ownership tests cover changed
targets and historical result reads. These are offline provider tests.
`tests/test_credential_rejection.py` drives actual controlled process adoption
and delayed single/concurrent rejection across replacement, OAuth commit, label
change, deletion and disable. `test_credential_reauth_ownership.py` covers all
old/new representation combinations through commit, resolver and renderer;
`test_claude_diagnostic_ownership.py` and `test_harness_diagnostics.py` cover
late process output and bounded, masked full logging.

## 6. Frontend

- `HarnessPage.tsx`: per-credential `needs_reconnect` → amber/destructive
  "needs reauth" badge + a "Re-authorize" button that opens the existing
  dialog in re-auth mode, threading `credential_id` (Claude) /
  `reauth_credential_id` (Codex). On success, refetch → flag clears.
  Claude's OAuth authorization action also supports an existing API-key record;
  the dialog explains that conversion retains its ID and bindings.
- Claude submission and recoverable errors retain the original completion request
  for explicit **Wait again**. **Close** ends observation; **Cancel sign-in**
  consumes the typed cancellation receipt. Late success, error and cancellation
  cannot update a retired account/view. Historical completion refetches the
  current credential list instead of upserting its old result, so deleting a
  credential is not undone by a late response. Codex retains its device polling
  and cancellation flow. `HarnessPage.test.tsx` covers these actual consumers.
- `useWebSocket.ts`: an `error` event with `code === "auth_expired"` renders
  the chat error text (as today) AND refetches `/api/credentials` so the
  sidebar badge appears immediately without a reload.

## 7. Real-CLI verification boundary

Real-runtime test selection, signed-in credential checks, isolation and this
installation's provider scope are owned by [testing.md](../testing.md).
The original bug showed why binary presence alone is not a signed-in precondition;
that diagnostic lesson does not authorize probing another provider's live login.
Hermetic protocol/refresh tests remain distinct from separately selected native
sign-in or model calls. This document does not keep another gate policy.

## 8. What this defers

- No automatic *retry* of the failed turn after re-auth — the user re-runs
  their prompt. (Connectors don't auto-retry either.)
- Reactive detection complements the proactive refresh contract above; it does
  not add refresh retries or change directory-based login.

## Historical evidence

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/harness-credential-reauth.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.

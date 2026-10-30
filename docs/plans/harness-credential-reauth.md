# Harness credential re-authorization (reactive 401 detection)

> **Status:** shipped — Reactive 401 detection: flag a credential that died mid-turn as `needs_reconnect` and offer re-authorization.

## 1. Problem

A harness sign-in credential (Claude Code OAuth / API key, Codex
device-auth) can become invalid *while a session is running* — the token
is revoked, an API key is rotated, or an OAuth token expires in a way the
**proactive** refresh path (`_refresh_oauth_if_needed`) never caught
(e.g. a pasted API key has no `token_expires_at`, so nothing pre-checks
it). When that happens the CLI fails the turn with an auth error, e.g.:

```
Failed to authenticate. API Error: 401 Invalid authentication credentials
```

Today that surfaces as a generic red error bubble (or, for some shapes,
is dropped entirely) and the bound credential is **never** flagged. The
user gets the same failure every turn with no affordance to fix it.

Connectors already solve the analogous problem: a refresh failure sets
`needs_reconnect=True` + `last_refresh_error_code` on the installation,
the sidebar shows a "reconnect" button, and re-auth clears the flag
in place. This plan brings the **reactive** (mid-turn 401) equivalent to
harness credentials, reusing the credential schema fields that already
exist for the proactive path (`needs_reconnect`, `last_refresh_error_code`,
`status`).

## 2. Scope

In: detect a mid-turn auth-credential rejection, mark the bound
credential, surface it (chat error + sidebar badge), and let the user
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
- `Harness.is_auth_error(text) -> bool` — case-insensitive substring match
  over the profile patterns. Pure; no I/O.

Patterns are intentionally specific phrases ("invalid authentication
credentials", "invalid x-api-key", "401 unauthorized", "invalid_grant",
…) rather than a bare `401`, and detection is **gated on the turn having
failed** (see §4), so a tool that merely *returns* a 401 to the model
(which then continues to a normal result) never trips it.

## 4. Run-loop wiring (`session_manager._run_backend`)

The combined error text for a turn is the terminal error event's
`content` (Codex `turn.failed`/`error` carry the message) plus the run's
captured `stderr_text` (Claude prints API errors there). After the stream
ends and `backend.stop()` runs:

- A turn is *failed* when it produced an `is_error` `result`/`error`
  event, or ended with no `result` at all.
- If failed AND `harness.is_auth_error(blob)`:
  - mark the bound credential (`session.credential_id` ?? agent's)
    `needs_reconnect` with `RefreshErrorCode.invalid_credentials` (reuses
    `_mark_needs_reconnect`); no-op if no credential is bound (host-default
    auth);
  - persist + broadcast a tagged error event
    `{type:"error", code:"auth_expired", credential_id, backend, message}`;
  - return immediately — the Claude premature-exit "continue" respawn must
    NOT run (re-auth won't fix itself, and retrying just burns the budget).

Detection reads the raw `HarnessEvent` stream directly (terminal event
`content`/`raw` captured in the loop), not the WS/persistence mappers — so
it works even for `type="error"` events that those mappers drop today, with
no change to result/error rendering semantics and no duplicate bubbles.

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
  `credential_id`. The target must belong to the login's account and use the
  Claude backend before the code is exchanged. After exchange, storage checks
  the target owner again under the same write lock as the in-place update,
  clearing reconnect/status fields without changing the credential ID. A
  deleted, adopted or foreign target returns 404 without replacing its secret;
  install-owner None is an explicit owner here, not an unfiltered DB query.
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

## 6. Frontend

- `CredentialList.tsx`: per-credential `needs_reconnect` → amber/destructive
  "needs reauth" badge + a "Re-authorize" button that opens the existing
  dialog in re-auth mode, threading `credential_id` (Claude) /
  `reauth_credential_id` (Codex). On success, refetch → flag clears.
- `useWebSocket.ts`: an `error` event with `code === "auth_expired"` renders
  the chat error text (as today) AND refetches `/api/credentials` so the
  sidebar badge appears immediately without a reload.

## 7. Test gating (real-CLI suites)

Surfacing this bug in the app also exposed it in the test suite: the
`tests/*_real.py` gates skipped only on a missing binary, so an *expired*
host login (the `claude` CLI present but logged out) ran them and turned
them red with the very 401 this feature detects — noise that masquerades
as a product regression. The gates now call shared helpers in `tests/cli_gate.py`
(`claude_cli_works()` / `codex_cli_works()`, imported as
`from tests.cli_gate import …` — `import conftest` isn't reliable under
pytest collection) that confirm the CLI is present **and** signed in (claude
via a tiny cached real call; codex via `~/.codex/auth.json`), so a logged-out
environment SKIPS rather than fails.
`test_codex_login_real.py` still gates on the binary alone — it tests the
sign-in flow itself and must run while logged out.

## 8. What this defers

- No automatic *retry* of the failed turn after re-auth — the user re-runs
  their prompt. (Connectors don't auto-retry either.)
- Reactive detection complements the proactive refresh contract above; it does
  not add refresh retries or change directory-based login.

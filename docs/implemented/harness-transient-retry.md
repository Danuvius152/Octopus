# Harness 瞬态错误恢复

> **Status:** implemented — 仅凭准备或续聊证据有界恢复，缺少输出不授权重放输入。

Transient provider failure, credential rejection and user quota have different recovery actions.
Idle timeout classification precedes this dispatch; see [turn safety](turn-safety.md).

## 2. Three dispositions for a failed turn

A failed turn's owned error facts are classified behind the harness contract.
Ordinary Codex uses only its bound root provider error (message, additional
details and typed code), or its preparation refusal. Claude uses attributed
terminal/raw and preparation errors; held-process stderr remains local diagnosis.
Native tool, child and unbound stderr text
cannot classify the main request or its credential.

1. **Auth-credential rejection** (401 / revoked / expired) →
   `Harness.is_auth_error` → STOP and conditionally reject the actual adopted
   material under the [credential contract](harness-credential-reauth.md#4-run-loop-wiring-serversessionsturnspy).
   Obsolete or unproven adoption leaves the current login unchanged.
2. **Transient backend error** (5xx / overloaded / dropped connection /
   timeout) → `Harness.is_transient_error` → bounded retry (this doc).
3. **Everything else** — including quota/credit ("rate limit", 429,
   "insufficient quota", billing) — surfaces as-is. Deliberately NOT
   retried: the user said "not my credit limit", and hammering a quota
   error wastes time and can worsen rate-limiting.

Typed personal-quota and authentication failures are excluded from transient
classification. Native explicit personal-quota text also takes precedence over
coincident capacity wording. A preparation RPC permission refusal is scoped
`permission_denied`: it cannot invalidate the main login or become a transient
retry. A bare `429`, "rate limit" or "quota" is not a reliability match.

## 3. Detection behind the harness contract

Mirrors the auth work (no `if backend ==` outside `server/harness/`):

- `RuntimeProfile.transient_error_patterns: tuple[str, ...]` — lowercased,
  auth-/quota-free, server-reliability phrases per backend.
- `Harness.is_transient_error(text, *, code=None) -> bool` — typed exclusions,
  provider codes and case-insensitive reliability matching; pure.

Patterns are specific server-reliability phrases ("overloaded", "internal
server error", "service unavailable", "bad gateway", "529", "connection
reset", "timed out", "stream error", …), chosen narrow after Vera's note
that broad tokens (a bare "unauthorized") cause false positives.

**Server-side throttle vs. the user's usage limit.** Anthropic emits a
server-side throttle as "Server is temporarily limiting requests (**not your
usage limit**) · Rate limited" — a transient blip that SHOULD retry, despite
containing "Rate limited". We must still NOT retry the user's *own* quota /
usage limit. So we match the throttle on its specific phrasing
("temporarily limiting requests", "not your usage limit") rather than a bare
"rate limit" / "429" (which also appears in the user's-limit message, and
stays non-retryable). This was a real miss: the bare-"rate limit" exclusion
swallowed the server throttle and stopped the turn instead of retrying.

Ordinary Codex retains the full legacy exec reliability phrase set in
`_CODEX_TRANSIENT_ERROR_PATTERNS`, including explicit capacity and server-throttle
wording. Bound native `serverOverloaded`, internal server errors and known
connection/stream variants with absent status, 5xx or 408 supply equivalent
structured evidence. Generic/no dedicated code can still qualify through the
provider's explicit text, including `additionalDetails`; personal quota and typed
authentication retain precedence. Exact source rules belong to
[the native error contract](codex-backend.md#error-source-and-recovery).

## 4. Recovery evidence in the original Owner

The original Owner classifies watchdog timeout first, authentication next,
then preparation/stale-history/transient failure and premature exit. A failed
result or missing result permits classification, not replay. Error notifications
remain diagnostics until the final outcome is known. Transient recovery permits
at most two additional attempts, after 1 second and 2 seconds
(`_TRANSIENT_RETRY_BASE_DELAY * 2**(n-1)`). The original logical turn owns this
budget across hosts; idle recovery retains its separate configurable budget.
The logical-turn deadline and manual interruption bound preparation, retirement
and backoff alike. Known native auth/quota/nonrecoverable facts veto recovery
even when admission closure also crossed an idle deadline; the earlier timeout
cause is retained rather than replaced by late provider observations.

| Original evidence | Allowed action |
| --- | --- |
| Recoverable preparation failure before business input submission | Prepare that attempt's pending input again within its authorization and budget. |
| Admission/terminal committed, acknowledgment lost | Read the original receipt; do not rerun the source or model. |
| Adapter supports continuation and this logical turn captured a conversation | Send continuation instructions against that conversation, checking existing files/tool effects before repeating work. |
| Only missing tool/text, an ambiguous diagnostic, or lost native history | Stop automatic replay; retain the original outcome and diagnostics. |
| Protocol/local boundary proves input was not executed | Resubmit within the original identity and budget. Current adapters provide the pre-submission boundary, not a provider execution ACK. |

`HarnessRun.input_may_have_run` belongs to the run's local input boundary. It
becomes true before submitting an argv-input spawn or writing a stream-JSON
frame; a partial write, failed drain or cancelled spawn acknowledgment cannot
turn it back into proof of non-execution. Reuse resets it before the next
turn's input. A preparation refusal before this boundary can retry the same
attempt prompt; it does not recover an earlier original prompt over already
attempted continuation work.

`RuntimeProfile.can_continue` is independent of steering and process reuse.
Claude stream-JSON and ordinary Codex App Server support conversation continuation;
the independent
Research exec leaf does not. The logical
turn retains conversation evidence observed during its own attempts, including
a failed attempt with no display output. A resume ID inherited from a previous
request alone does not authorize this recovery. Continuation is a new input;
it cannot promise exactly-once tool or external effects.

The Owner classifies against the actual attempt's selected profile. The native
provider first retries within its bound root; valid active-root/associated-child
`willRetry` observations count as activity without another input. Native failed
roots and idle expiry can then qualify for outer continuation only with
[the native identity and retirement evidence](codex-backend.md#error-source-and-recovery).
Unknown acceptance, protocol corruption and successful roots do not qualify.
Native resolved model/effort/tier is captured before thread preparation and carried
to later attempts, including after a preparation refusal. The original product
configuration and credential remain captured, while authorization is rechecked
before each execution. Changing defaults cannot change this request's selection.

Stale-history recovery can clear the stale ID and prepare a retained-history
wrapper once only if the input was not submitted and this turn has no captured
continuation work. A stale error after possible submission stops with
`recovery_unproven`; missing history does not prove the request had no effects.
The old no-output replay rule and turn-start resume-ID restoration are removed.
The current attempt owns its prepared input, including any large-prompt file
pointer. Recovery selects the next attempt's input from that value; there is
no separate original-prompt copy to synchronize after rebuilding history.

Recovery markers remain persisted and published. Exhaustion or insufficient
continuation evidence ends the request instead of replaying it. An explicit
failed outcome stays failed; restart claims and unknown steer retain their
separate [input reliability](input-reliability.md) meanings.

SessionResult, App ask/SSE, application builds and named delegations keep waiting
through failed attempts and consume only the unique logical-request completion.
Final-answer references select the final attempt's qualified root text; earlier
failed-attempt output stays historical. Resource observation spans every original
attempt, so successful recovery requires retirement of all of them.

`tests/test_retry_evidence.py` exercises idle/transient recovery on both
backends with real external file effects but no tool/text observations, stale
history, unsupported continuation and pre-submission preparation refusal.
The scripted-process and per-attempt metric regressions additionally check
actual prompts, attempts, budgets and termination.
`tests/test_codex_native_recovery.py` drives the original Owner, SQLite input
evidence and controlled stdio through native reconnect activity, all legacy
categories, capacity shapes, preparation snapshot drift, Stop/revocation/deadline
and retirement boundaries. `tests/test_codex_app_server_results.py` checks actual
App/SSE, build and named-delegation consumers through recovery.
`tests/test_turn_recovery_state.py` also checks large input through preparation
failures on both sides of stale-history recovery: the original spill file stays
intact and subsequent attempts retain the rebuilt history and its pointer.

## 5. Limits

- No retry of personal quota/credit failures; a bare rate limit is insufficient
  transient evidence (§2).
- Retry counts/backoff are module constants, not yet user-configurable.

## Historical evidence

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/harness-transient-retry.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.

# Harness transient-error retry

> **Status:** implemented — Evidence-limited preparation and continuation; missing output never authorizes prompt replay.

Transient provider failure, credential rejection and user quota have different recovery actions.
Idle timeout classification precedes this dispatch; see [turn safety](turn-safety.md).

## 2. Three dispositions for a failed turn

A failed turn's error text (terminal event content/raw + the CLI's stderr)
is classified, in order, behind the harness contract:

1. **Auth-credential rejection** (401 / revoked / expired) →
   `Harness.is_auth_error` → flag the credential needs_reconnect and STOP
   (harness-credential-reauth.md). Never retried — re-auth won't fix itself.
2. **Transient backend error** (5xx / overloaded / dropped connection /
   timeout) → `Harness.is_transient_error` → bounded retry (this doc).
3. **Everything else** — including quota/credit ("rate limit", 429,
   "insufficient quota", billing) — surfaces as-is. Deliberately NOT
   retried: the user said "not my credit limit", and hammering a quota
   error wastes time and can worsen rate-limiting.

The classifiers are mutually exclusive by construction — `429` / "rate
limit" / "quota" appear in neither pattern set; auth phrases appear only in
the auth set.

## 3. Detection behind the harness contract

Mirrors the auth work (no `if backend ==` outside `server/harness/`):

- `RuntimeProfile.transient_error_patterns: tuple[str, ...]` — lowercased,
  auth-/quota-free, server-reliability phrases per backend.
- `Harness.is_transient_error(text) -> bool` — case-insensitive substring
  match; pure.

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

## 4. Recovery evidence in the original Owner

The original Owner classifies watchdog timeout first, authentication next,
then preparation/stale-history/transient failure and premature exit. A failed
result or missing result permits classification, not replay. Error notifications
remain diagnostics until the final outcome is known. Transient recovery retains
the two-attempt exponential budget (`_TRANSIENT_RETRY_BASE_DELAY * 2**(n-1)`),
and the logical-turn deadline and manual interruption bound all recovery.

| Original evidence | Allowed action |
| --- | --- |
| Preparation failed before business input submission | Prepare that attempt's input again within its authorization and budget. |
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
Claude stream-JSON supports native conversation continuation; the independent
Research exec leaf does not. The logical
turn retains conversation evidence observed during its own attempts, including
a failed attempt with no display output. A resume ID inherited from a previous
request alone does not authorize this recovery. Continuation is a new input;
it cannot promise exactly-once tool or external effects.

The Owner classifies against the actual attempt's selected profile. App Server
has `can_continue=False` and no outer transient text/code matches. Its provider
may retry inside the bound root; `willRetry` is a saved diagnostic, without a
new product attempt or reset budget. Repeated retry/child error diagnostics do
not refresh idle; a newly observed child terminal still counts as progress.
Accepted/unknown input, idle failure and
exhausted provider retry do not authorize another send. A proven pre-submission
refusal remains subject to the original preparation policy. Quota and unrelated
tool/child auth errors retain their distinct source; see
[the native error contract](codex-backend.md#error-source-and-recovery).

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
separate [input reliability](../input-reliability.md) meanings.

`tests/test_retry_evidence.py` exercises idle/transient recovery on both
backends with real external file effects but no tool/text observations, stale
history, unsupported continuation and pre-submission preparation refusal.
The scripted-process and per-attempt metric regressions additionally check
actual prompts, attempts, budgets and termination.
`tests/test_turn_recovery_state.py` also checks large input through preparation
failures on both sides of stale-history recovery: the original spill file stays
intact and subsequent attempts retain the rebuilt history and its pointer.

## 5. Limits

- No retry of quota/rate-limit/credit failures (§2).
- Retry counts/backoff are module constants, not yet user-configurable.

## Historical evidence

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/harness-transient-retry.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.

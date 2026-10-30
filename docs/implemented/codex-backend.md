# Codex: interactive execution and tool-free text calls

> **Status:** implemented — Native exec turns, scoped instructions and a separate subscription text worker.

## Interactive execution

The current Codex profile runs native `codex exec --json` through the common
Harness. Native thread identity is retained for resume. Stdin closes after the
initial input; additional user input queues for a subsequent turn. The proposed
app-server transport and complete native-tail semantics remain in
[the runtime plan](../plans/engineering-runtime-roadmap.md).

The profile renders selected MCP entries and a composed controller through
developer instructions. Resumed threads receive controller updates and separate
request-specific trusted instructions; no saved agent configuration is rewritten
to accomplish this. Model, effort and speed follow [their own contract](model-reasoning.md).
Nonempty arbitrary tool allow/deny policies unsupported by Codex are rejected;
headless bypass is not proof that such a policy was enforced.

Native stdout events and bounded verified rollout observations are normalized
into common events. Native helper cards describe observation, not proof that
the parent consumed every child result. Usage timing has the narrower
[Monitor contract](codex-performance-monitor.md).

The selected native login remains the authentication authority. A bound
credential selects its runtime home; an unbound invocation follows the existing
host-default path. In-app device login is implemented. This differs from the
original plan's unresolved login alternatives. Public transcript export/import
is separate from [rewind](session-rewind.md) and [native-copy fork](session-fork.md).

<a id="single-call-text-contract"></a>
## Single-call text contract

`run_oneshot` performs one non-interactive text task without executable model
tools. The Codex profile now launches `codex_text.py` with isolated Python
(`-I`) under the common Harness, instead of starting `codex exec`. Schedule
parsing, `/showme` reference resolution and research decomposition/synthesis
keep their public interface; ordinary Codex turns still use the native CLI.

The worker sends a subscription Responses request with `tools=[]`,
`tool_choice="none"` and no dispatcher for tool execution. It does not load
project instructions, skills, MCP, apps or plugins into the model request.
Only a successful `response.completed` can publish text; incomplete, failed,
tool-operation and truncated streams fail without publishing partial text.
There is no fallback to the old executable-agent path or API-key billing.

Native Codex remains the authentication authority. The installed 0.164.0
app-server's `getAuthStatus(includeToken=true, refreshToken=false)` returns
the selected ChatGPT bearer in memory, using native file/auto/keyring storage
selection and login policy. The worker neither reads `auth.json` itself nor
writes a token cache. This preserves the unbound host-default login as well as
an explicitly bound application login. Account routing comes from the native
token's account claim. On HTTP401, it rereads native current auth and requests
native refresh only if the rejected login is still current, then retries the
model request once. Other failures do not start a retry loop.

Explicit model/effort values win; absent values use native `config/read` for
the working directory, then `model/list` defaults if needed. Octopus normal
speed remains `default`; `fast` maps to Responses `priority`. HTTP proxy
environment variables remain inherited. Native control starts no model thread.
Both it and the HTTP request belong to the common one-shot process group:
spawn authorization precedes execution; cancellation/timeout must confirm group
retirement, including native descendants, before reporting a clean stop.

The reason for this separate worker is executable capability control: disabling
public CLI features did not establish a complete tool-free native-agent request.
The text worker has no executable-tool dispatcher and publishes only a successful
text terminal. The original probe record retains the failed and corrected trials.

The native auth operation and subscription wire were checked against Codex
0.164.0; they are not a promise about arbitrary future CLI releases. Protocol
incompatibility fails visibly and cannot restore the executable-agent path.
No OS keyring was provisioned for the original live validation; synthetic
keystore cases cover bound/unbound behavior without claiming a live keyring test.

## Source and verification

Code: [profile](../../server/harness/codex.py),
[native control](../../server/harness/codex_control.py),
[text worker](../../server/harness/codex_text.py).
Regression entry points: `tests/test_harness_codex.py`,
`tests/test_codex_controller.py`, `tests/test_codex_text.py`,
`tests/test_codex_text_process.py` and `tests/test_backend_codex_real.py`.
The focused worker scenarios include admission denial, HTTP/stream failures,
missing native binary, hostile startup configuration and confirmed descendant
retirement. Current installation checks are selected through [testing.md](../testing.md).

## Historical evidence

The [original design and evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/codex-backend.md) retain their original version and conditions. Current verification follows [testing.md](../testing.md); this document does not establish production deployment.

# Octopus-native deep research

> **Status:** shipped — Octopus-orchestrated deep research: scoped fan-out of web-search sub-turns, adversarially verified, then synthesized.

> **Implementation status: SHIPPED.** The full pipeline described in this plan
> is implemented and running: `server/research/` (manager, orchestrator, leaf
> executor, schemas), `server/routers/research.py`, `server/mcp_servers/research.py`,
> and the `ResearchCard` frontend component. The `research_jobs` table is in the
> schema. The §1a prerequisites (process-group reaping, tool-policy renderer) were
> resolved as part of the turn-safety and harness work. This doc now serves as
> design reference; the §9 test suite is in `tests/test_research*.py` and
> `web/e2e/research.spec.ts`.

## 1. Why native (not the harness skill)

The Claude Code `/deep-research` skill is a background, multi-agent
**Workflow** that fans out dozens of subagents and relies on the interactive
Claude harness's workflow runtime + completion-notification. Run inside
Octopus's one-shot headless `claude --print` turn it (a) never streams a
terminal result so the turn hangs, and (b) is Claude-only — Codex has no
equivalent. Octopus is a **harness-agnostic** agent platform, so research
orchestration belongs at the Octopus layer, the same way connectors,
schedules, bg-tasks and delegations do. We own the fan-out, the limits, the
progress, the cancellation, and the persistence — and it works identically on
claude-code and codex.

## 1a. Prerequisites — resolved before ship

Two harness-layer concerns were addressed before research shipped:

1. **Per-turn lifecycle / process-group reaping ("Layer 1").** `HarnessRun`
   spawns without `start_new_session=True` (`run.py:225`) and `stop()` only
   kills the direct child (`run.py:270`); `run_oneshot` likewise just
   `proc.kill()`s. Every research leaf is another real harness subprocess
   (Claude's web sub-turn itself may spawn children), so we need: own process
   group + `killpg`, a per-turn idle/overall timeout, and a heartbeat — applied
   to BOTH the streaming run and `run_oneshot`. bg_tasks already does process
   groups (`bg_tasks.py:252,593`); reuse that.
2. **A backend tool-policy renderer.** Claude maps `tool_allow` →
   `--allowedTools` (`claude_code.py:206`), but **codex `build_turn_argv`
   ignores `ctx.tool_allow`/`tool_deny` entirely** (`codex.py:129`). So the
   "restricted to web + read-only" leaf contract is NOT backend-agnostic today.
   Codex needs a profile-level tool-policy + web-enable renderer (emit
   `-c tools.web_search=true` and enforce the allow/deny set) before research
   leaves run on codex; until then codex research is gated off.

## 2. The layering (this is the whole idea)

We do NOT build web search/fetch — that's a product in itself (ranking,
freshness, anti-bot, JS rendering, extraction) and a maintenance sink. Web
access comes from the **harness's own native web tools** (Claude
`WebSearch`/`WebFetch`; whatever a future backend exposes). Octopus owns only
the part that was actually missing: **orchestration** — driving the fan-out as
many small, bounded sub-turns instead of one giant uncontrolled turn, with
progress, limits, cancellation, and persistence.

| Layer | Owner | Mechanism |
|-------|-------|-----------|
| Web search + fetch | **the harness** | its native `WebSearch`/`WebFetch`, used inside a scoped sub-turn — §4 |
| Per-leaf work (search-an-angle / extract-claims / verify-a-claim) | a scoped harness **sub-turn** | a normal `HarnessRun` turn with `tool_allow` = web (+read-only) and a focused prompt — §5 |
| Pure reasoning leaves (scope decompose, final synthesize) | harness, agnostic | `harness.run_oneshot` (tool-free) — §5 |
| Orchestration (phases, fan-out, bounded concurrency, timeout, cancel, persist) | **Octopus (asyncio)** | `ResearchManager` + a pipeline — §6 |
| Invocation + progress + result delivery | Octopus | a built-in MCP tool + session injection, like bg/delegations — §7 |

Architecturally backend-neutral (no `if backend ==`; the orchestration only
ever calls `run_oneshot` and the generic `HarnessRun` turn API). Web
*capability* is **capability-gated** like fork's `can_fork`, but BOTH current
backends qualify (verified — §4): claude-code via `WebSearch`/`WebFetch`,
codex via its native `web_search` tool. A future backend with no web tools
simply doesn't offer research. We never build search ourselves.

## 3. Phases (mirrors the proven shape, orchestrated in Python)

1. **Scope** — one `run_oneshot` (tool-free) decomposes the question into 3–6
   angles (JSON, validated like schedule_ai's parse). No web needed.
2. **Search + gather** — one scoped **web sub-turn per angle** (parallel,
   semaphore-bounded): a `HarnessRun` turn allowed only the harness's web tools
   (+read-only), prompted to search the angle, read the best sources, and
   return a compact list of `{claim, url}` findings as JSON. The harness does
   the searching/fetching with its own maintained tools; we just collect text.
3. **Dedup + rank** — pure Python over the returned findings (no model, no web).
4. **Verify** — for the top-ranked claims, K independent web sub-turns each
   (adversarial "try to refute", web-allowed); majority-refute kills the claim.
   Only a strict majority of explicit positive verification counts as verified.
   Valid non-refutation without supporting evidence stays unverified; no valid
   verdict is a verification failure, retained with that label rather than
   counted as verified.
5. **Synthesize** — one `run_oneshot` (tool-free) merges survivors into a cited
   report from the gathered `{claim, url}` set and their verification labels,
   explicitly qualifying unsupported findings.

A single `asyncio.Semaphore` (config, default ~4–6) bounds ALL concurrent
sub-turns/oneshots so we never spawn the ~75-subprocess storm the CLI Workflow
did. Each phase reports progress (§7).

## 4. Web access — from the harness, never built here

Web search/fetch is the harness's job. A "leaf" that needs the web is a normal
Octopus `HarnessRun` turn (the same engine a chat turn uses), run in a
throwaway scratch context — not the user's session. Octopus reads the turn's
final text (JSON findings) and discards the sub-turn.

- **A dedicated leaf executor, NOT delegation child sessions** (Vera). Reusing
  delegations would drag in session persistence, child archival, broadcast
  capture, parent-chain/cycle rules, follow-up semantics and user-visible
  collaboration — all wrong for a stateless leaf. Instead a small leaf executor
  resolves the agent's credential/model once, runs a throwaway `HarnessRun`,
  captures the final assistant text, and returns structured `{text, cost,
  error}`.
- **The leaf does NOT inherit the session's `RunConfig`** (Vera). `_run_config`
  normally composes the agent's MCP set, connectors, memory dir, persona and
  tool policy (`session_manager.py:2082`), and `HarnessRun._make_context`
  always layers in the tools prompt + selected MCP servers (`run.py:171`). A
  web leaf must get an EXPLICIT minimal config: `mcp_servers=[]`,
  `connectors=[]`, no memory dir, a stripped system prompt, no resume id, a
  scratch cwd under the job dir, and `tool_allow` = the backend's web tools +
  read-only — so the leaf can't see bg/ask/ask_agent/connectors or durable
  memory instructions and cause side effects.

- **No provider, no API key, no extractor lib, no scraping** on our side —
  those are exactly the "way harder than it looks" problems we're declining to
  own. We inherit whatever quality/freshness the harness's web tools give.
- **Capability gate — a `WebCapability` object, not just a tuple** (Vera). A
  tuple of tool names under-specifies it. The profile declares a small
  `WebCapability`: the web tool name(s); how to ENABLE web (codex needs
  `-c tools.web_search=true`; claude needs nothing); whether tool restrictions
  are actually enforceable on this backend (see §1a — codex isn't yet); the
  native tool-event name(s) to watch in the stream; and whether fetch is
  separate (claude: WebSearch + WebFetch) or combined (codex: one `web_search`
  that searches+reads). Empty/None = no research on that backend, gated like
  `can_fork`. Verified against live CLIs: claude-code `WebSearch`/`WebFetch`;
  codex `web_search` (off by default — `codex 0.132 exec --json
  -c tools.web_search=true` emits a real `item.type:"web_search"` with cited
  results). The leaf prompt is written tool-agnostically ("use your web
  tools…") so one orchestration drives both shapes.
- **Bounding is mandatory here** — see §1a prerequisite 1.

## 5. Reasoning leaves via `run_oneshot`

The non-web leaves (scope decompose, final synthesize) are
`OneShotContext(prompt=..., model=agent.model, credential=<agent's resolved
credential>, working_dir=<scratch>)` → `run_oneshot`, parsed against a JSON
schema (reusing schedule_ai's extract-validate-retry helper). Tool-free and
backend-neutral, with the agent's own credential/model so cost attributes
correctly, bounded by `run_oneshot`'s timeout + the job timeout (§6).

## 6. ResearchManager (async job, like bg/delegations/scheduler)

A module singleton bound in `main.py`'s lifespan (mirrors `bg_task_manager`
/ `delegation_manager`). It:

- Starts a research job for `(session_id, question)`, returns a `research_id`
  immediately, runs the pipeline as a tracked `asyncio.Task`.
- A failed or empty final synthesis ends the job as `failed`, preserving the
  reason and gathered findings in the account-owned job directory as
  `synthesis-failure.json`. It emits no successful completion or report
  injection; a placeholder is not a completed report.
- Owns a **hard overall timeout** and an **idle/heartbeat** — the gap that let
  the CLI Workflow hang forever simply cannot happen here because we control
  the loop and never block on an unbounded external stream.
- Is **cancellable**: cancel reaps all in-flight `run_oneshot` subprocesses
  (we hold their `HarnessRun`s) and fetch tasks — no orphans (contrast the CLI
  Workflow, whose nested `claude` processes orphaned on interrupt).
- Persists to a `research_jobs` table, with **completion and delivery tracked
  separately** (Vera): `status`/`phase`/`completed_at` AND `injection_status`/
  `injected_at`/`delivery_error`/`report_path`. Because the report is delivered
  by queuing a `start_message` injection (§7), a job can be `completed` while
  its report still sits behind an active user turn — the card/DB must show
  completion separately from admission. `injection_status=delivered` is a
  legacy admission fact, not evidence the model processed the report.
  `delivery_receipt` binds the follow-up request and version, distinguishing
  queued, processing, processed, failed, recording_unconfirmed and unknown.
  A browser confirms the exact displayed version through the account-only
  `POST /api/sessions/{sid}/research/{rid}/seen` route; this is separate from
  model processing and does not assert human reading. Restart reconciles
  committed outcomes or records unknown, without replaying the request.
  Terminal injection is **idempotent** (like delegations' `_terminal_injected`).
  Completion, cancellation, failure and interruption share one guarded
  `running`-to-terminal transaction. Completion requires the report file and
  its directory entry to be synced before committing the completed row.
  A winning cancellation interrupts live work; cancellation after completion
  returns the completed job and preserves its delivery. The card consumes this
  response directly through its existing state merge. A boot sweep marks running jobs
  `interrupted`/`failed`; v1 does NOT resume mid-pipeline.

Research captures execution configuration when admitted, including while it
waits for a job slot. A new lawful return may restore its original archived
session through SessionManager; the original account, agent, work and receipt
still govern admission. Archive interrupts the current input and revokes all
queued inputs, while later returns may wake the caller. Previously selected
Stop remains paused; cancelled inputs are never replayed. Background tasks and
delegations use this same return boundary, including delegated questions.
Restoration carries dispatch ordering within the current service generation,
so delayed lifecycle notifications cannot replace a newer list projection.

Report delivery preserves its original `research:{job_id}` identity. A typed
`AdmissionIntegrityError` permanently ends automatic delivery while retaining
the completed report and reason; restart or repairing the target never resends
it. Only explicit capacity refusals (`SessionMailboxFull`, `QuotaExceeded`) and
`SessionAdmissionBusy` permit the existing delayed retry. Arbitrary `ValueError`
and programming errors are not retry classifications. They retain the original
exception, task and diagnostic on the completed job; the card shows delivery
stopped, without changing the research outcome or asserting non-acceptance.
Storage failures preserve the original fail-stop boundary.

A lost admission ACK first resolves the exact original source receipt. A found
receipt supplies the existing input and subsequent processing status, even if
the destination Owner has since failed. An unreadable or absent receipt after
an unconfirmed attempt leaves `injection_status=unknown` and the report retained;
neither a later rejection nor a restart authorizes replay. Startup may reconcile
that original receipt, without rerunning research, reopening a failed Owner or
submitting another input. Source cancellation and shutdown remain separate from
input acceptance and resource retirement. The regression seams are the real
SessionManager admission, SQLite receipts and a controlled report generator in
`test_research_session_return.py`, with source-failure coverage in
`test_research_manager.py` and `test_source_return_owner_failure.py`.

Pipeline owners and report-delivery observers share one task registry and one
registration entry. `wait_for_idle()` joins both phases without closing admission;
a completed report alone does not mean its follow-up receipt has settled. Done
callbacks release only the registration they still own. Observation failures
remain visible to draining and shutdown instead of disappearing after logging.
Shutdown closes admission, captures the original registry once and cancels each
live owner at most once. A timeout preserves that snapshot for a later join,
including cancellation cleanup still writing an unknown delivery result. Observers
handed off by a shielded original pipeline during shutdown join that
same snapshot and settle unknown without replaying the accepted request. Request
to job/session routing and exact-version browser confirmation remain separate
facts; neither is a second task registry or a prerequisite for business delivery.

A web leaf needs a successful provider terminal. Parseable text followed by
EOF without that terminal remains diagnostic material and supplies neither
search findings nor verification votes. Cleanup failures retain the original
run and its result evidence separately from the retirement error.

## 7. Invocation + surfacing

- **Agent-initiated**: a new built-in MCP server `mcp__research__deep_research
  (question)` added to the default per-agent MCP set (alongside ask/bg/
  ask_agent; defaults apply only when creating an agent). It's a thin HTTP shim to a
  `/api/sessions/{sid}/research` route, same env-injection pattern as bg/
  ask_agent. Returns immediately with `research_id`; the model's turn ends
  cleanly (no hang). This keeps invocation model-agnostic — the agent just
  calls a tool.
- **User-initiated**: a `/research <question>` slash command (like
  `/schedule`) → same route.
- **Progress**: phase transitions + counts ("Searching 5 angles…", "Gathered
  9 findings…", "Verified 18 claims…") broadcast as session events and rendered
  as a research progress card (a new card like the delegation/bg chips).
- **Result**: the final cited report is written to a `research/` file under
  the working dir AND injected into the session as a synthesized turn
  (`[deep-research:<id>] …report…`) via the same `start_message` path bg
  delivery uses — so the agent can read/act on it and the user sees it.
- **Cancel**: a cancel control on the card → route → `ResearchManager.cancel`.

## 8. Limits & safety

A per-job semaphore (~4–6) is necessary but **not sufficient** (Vera): add a
**global** concurrency budget across ALL research jobs (else N sessions each
start 6 leaves and exhaust the box) and a max-concurrent-jobs cap. Plus
phase-level caps: max findings per angle, max claims entering K-vote verify,
max retries per leaf, max report-input bytes. Other config: max angles,
verifier votes, per-sub-turn + per-oneshot + per-job timeouts, optional per-job
token/cost ceiling. Never hold the session's turn lock during a job; only touch
`start_message` for terminal injection. Web I/O limits (page size, rate) are
the harness's concern, not ours.

## 9. Testing

- Orchestrator: fake the leaf executor (both the web sub-turn and
  `run_oneshot` are injected callables — schedule_ai already shows the
  `runner=` seam) so the whole pipeline runs deterministically with no network
  or CLI; assert phase order, bounded concurrency, timeout abort, and
  cancellation reaping. Dedup/rank are pure-unit.
- Capability gate: a backend with no `web_tool_names` makes deep research
  return the clear unavailable message.
- Manager/routes/MCP tool: like the delegations/bg suites.
- Prerequisite coverage (Vera): codex `build_turn_argv` web-enablement +
  tool-restriction rendering; leaf-config isolation (no inherited
  MCP/connectors/memory); process-group reaping for `HarnessRun` *and*
  `run_oneshot`; global (cross-job) concurrency cap; queued terminal injection
  (job `completed` while injection pending → delivered) + idempotency; boot
  sweep marking interrupted jobs.
- Gated real-CLI test (one tiny real research run on claude-code) behind the
  `tests/cli_gate.py` "signed-in" gate.
- Frontend: research card states (running/phase/done/failed/cancel).

## 10. What this defers / explicitly out of scope

- **We never build or integrate web search/fetch** — no provider API, no
  scraping, no extraction lib. Web is sourced from the harness's native tools
  (§4); quality is whatever the harness gives.
- **Prerequisite, not deferred:** the generic per-turn watchdog / heartbeat /
  process-group reaping ("Layer 1"). Because each web leaf is a real harness
  turn, this feature must land on top of that safety net so a wedged leaf is
  bounded — it's what structurally prevents the original hang.
- Result caching across jobs, and non-text sources, are follow-ups once the
  core lands. (Codex web parity is NOT deferred — verified working via
  `tools.web_search`; both backends are supported from the start.)

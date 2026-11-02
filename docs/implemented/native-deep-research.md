# Native research: pipeline, report and return ownership

> **Status:** implemented — Bounded native leaves with distinct research completion, follow-up processing and retirement.

## 1. Responsibility and rationale

Octopus owns research orchestration, bounded concurrency, progress, cancellation,
report persistence and caller delivery. Native harness web tools own search and
fetch. Reasoning steps use `run_oneshot`; web steps use isolated native runs.
This keeps independent research jobs from depending on an interactive provider
workflow or the lifetime of the caller's initial model turn.

## 3. Pipeline

Scope decomposition produces angles, parallel web leaves gather findings, Python
deduplicates/ranks, independent leaves try to refute claims, then a tool-free
reasoning call synthesizes the report. Only a strict majority of explicit positive
verification counts as verified. Non-refutation without support remains unverified;
no valid verdict is a verification failure, retained as such.

## 4. Web leaves

Web capability is supplied by the selected profile. The leaf executor uses a
scratch cwd, no native resume ID, a minimal persona, empty MCP and connector lists,
no memory directory and `web_research=True`. It does not inherit the caller's
interactive RunConfig. Profile-level rendering supplies the scoped web policy;
unsupported configurations fail explicitly. This is a concrete runtime contract,
not a promise that a future CLI preserves today's tool set.

The Codex web leaf uses its independent `codex exec` path with read-only sandbox
and no injected Octopus MCP or connectors. This is separate from ordinary
interactive transport selection and is retained when that transport changes.
Read-only and empty injected tools do not establish a strict web-only whitelist:
the verified native tool surface also exposes non-web functions and collaboration
tools. The leaf's prompt requests web research, but is not an enforcement boundary.
Reasoning leaves use the distinct tool-free text worker below.

Leaves are not delegation sessions: stateless extraction does not need named-agent
conversations, parent questions, retained memory or collaboration UI. Each leaf is
bounded and its original process remains owned until retirement is confirmed.

## 5. Reasoning leaves

`OneShotContext` and `Harness.run_oneshot` perform decomposition and synthesis
without executable tools. The [Codex text worker](codex-backend.md#single-call-text-contract)
owns its narrower implementation. Provider failures retain error evidence;
cancellation and unconfirmed cleanup propagate to the job owner.

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
- Is **cancellable**: cancellation joins the original leaf and fetch operations.
  One-shot subprocesses remain owned by their registry Harness through confirmed
  retirement, including late spawn and private-settings cleanup. An unconfirmed
  cleanup remains visible to installation drain; a returned waiter is not proof
  that its process has exited.
- Persists to a `research_jobs` table, with **completion and delivery tracked
  separately** (Vera): `status`/`phase`/`completed_at` AND `injection_status`/
  `injected_at`/`delivery_error`/`report_path`. Because the report is delivered
  through the shared `admit_return` operation (§7), a job can be `completed` while
  its report still sits behind an active user turn — the card/DB must show
  completion separately from admission. `injection_status=delivered` is a
  legacy admission fact, not evidence the model processed the report.
  `delivery_receipt` binds the follow-up request and version, distinguishing
  queued, processing, processed, failed, recording_unconfirmed and unknown.
  A browser confirms the exact displayed version through the account-only
  `POST /api/sessions/{sid}/research/{rid}/seen` route; this is separate from
  model processing and does not assert human reading. Restart reconciles
  committed outcomes or records unknown, without replaying the request.
  Terminal delivery retains its original source identity and admission receipt.
  Completion, cancellation, failure and interruption share one guarded
  `running`-to-terminal transaction. Completion requires the report file and
  its directory entry to be synced before committing the completed row.
  A winning cancellation interrupts live work; cancellation after completion
  returns the completed job and preserves its delivery. The card consumes this
  response directly through its existing state merge. A boot sweep marks running jobs
  `interrupted`/`failed`; v1 does NOT resume mid-pipeline.

Before admitting a job, Research captures the session overrides and Agent defaults
through SessionManager's short configuration boundary, resolving the actual model
and credential ID into the job's private request value. Capacity waits, credential
resolution and model execution occur outside that lock. A queued job therefore
keeps its admitted choice when defaults change; a later job sees the new defaults.
Every leaf spawn rechecks current account, credential and execution authority;
revocation ends the original job rather than switching it to another credential.
After configuration waiting, admission rechecks shutdown synchronously before
registering the task. Configuration capture rejects a missing or closed target
and checks that the same target remains after Agent loading; Research maps that typed
refusal to its normal admission error without creating a job or pipeline.

A new lawful return may restore its original archived
session through SessionManager; the original account, agent, work and receipt
still govern admission. Archive interrupts the current input and revokes all
queued inputs, while later returns may wake the caller. Previously selected
Stop remains paused; cancelled inputs are never replayed. Background tasks and
delegations use this same return boundary, including delegated questions.
Restoration carries dispatch ordering within the current service generation,
so delayed lifecycle notifications cannot replace a newer list projection.

Report delivery and startup reconciliation use the session-owned
[`admit_return` operation](architecture-contract.md#公共返回接管), preserving the
original `research:{job_id}` identity. Normal preparation reads the source job
and builds its report body only when a new input is still permitted; startup
preparation always skips new input. An existing receipt therefore needs neither
the report material nor a functioning destination Owner. Research owns its
report, delayed retry and processing observer; it does not assemble receipt
lookup, wake-up or session admission checks itself.

A typed
`AdmissionIntegrityError` permanently ends automatic delivery while retaining
the completed report and reason; restart or repairing the target never resends
it. Only the shared operation's explicit capacity or busy refusal permits
Research's existing delayed retry. Arbitrary `ValueError`
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
`test_research_session_return.py` and `test_research_return_admission.py`, with source-failure coverage in
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

## 7. Invocation, files and verification

The research tool and `/research` command admit the same account/session-owned
job. Progress and the report card are projections of that job. Reports and failure
materials live under the account-owned research job directory; the original
caller receives a separate follow-up with stable delivery identity.

Source: [manager](../../server/research/manager.py),
[orchestrator](../../server/research/orchestrator.py),
[leaf executor](../../server/research/leaf.py).
Regression entry points: `tests/test_research_orchestrator.py`,
`tests/test_research_leaf.py`, `tests/test_research_manager.py` and
`tests/test_research_session_return.py`. Manager regressions queue jobs across
Agent-default/session-override changes, credential deletion and shutdown while
configuration capture waits. `tests/test_research_api.py` deletes or archives
the real session during that wait and verifies HTTP refusal with no job created.

Research does not resume an interrupted pipeline on startup, add a new search
provider, or claim that browser display proves human reading. Current authorization
and resource retirement follow [the execution contract](architecture-contract.md).

## Historical evidence

The [original design and evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/native-deep-research.md) retain their original version and conditions. Current verification follows [testing.md](../testing.md); this document does not establish production deployment.

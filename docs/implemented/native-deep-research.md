# 原生 Research：流水线、报告与返回归属

> **Status:** implemented — 有界原生 leaf，分别记录报告完成、后续处理与资源退休。

阶段检查点、跨 job 来源复用、非文本材料与工作预算的后续范围归
[Research 工作流计划](../plans/ai-research-workflow.md)。当前可恢复返回观察，
不代表流水线能从任意中间阶段续跑；未知外部效果不自动重放。

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

Leaf JSON selection follows the consumer's expected shape: scope angles and
search findings are arrays; verification verdicts are objects. A complete naked
JSON value owns its whole tree. Otherwise, a unique schema-valid complete fence
takes priority over surrounding prose; without one, selection considers complete
outer containers while respecting strings, escapes and nesting. Wrong-shaped,
malformed or unusable outer values never promote their children into results.
Multiple independent usable candidates at the selected priority are ambiguous,
even when identical. Unusable candidates do not compete with usable ones.
Decoder refusals due to syntax, integer size or nesting make a candidate unusable.
Selection itself does not shorten evidence or change omission flags; bounds are
applied only to the uniquely selected verdict. Unusable or ambiguous angles fall
back to the original question. Only an actual empty search array means successful
absence of material; unusable searches and verdicts remain failures. A leaf error
takes precedence over any partial JSON, and parsing tolerance supplies neither
positive verification nor missing sources.

The pipeline constructs one bounded evidence summary, consumed by synthesis,
the deterministic evidence section at the start of the saved report, and the
ResearchCard. Each angle records findings, a successful empty result, or a
failure with its reason. Unparseable search output is a failure, not a successful
empty search; parseable partial output from a failed leaf supplies no findings
or verification votes. If every search fails but synthesis produces a gap
report, execution may complete: the report and card prominently show insufficient
evidence, and the card opens the failed angles and reasons by default.

Each checked claim retains support, refutation, unverified and failed votes,
their reasons, conditions and the source URLs actually supplied by the checks.
Missing check sources remain missing; the original claim URL is never borrowed
as a counterevidence source. Strict majority uses the original scheduled vote
count, including failures. Refuted claims leave the recommendation findings but
remain in the summary and synthesis input with their counterevidence and
disposition. Votes and angles are not independent sources. Positive verification
is a leaf's explicit support verdict, not proof that its URL was read or that
the whole question has sufficient evidence.

The original job/claim/vote limits still govern work. The summary retains at
most five angles, twelve claims, eight vote details per claim and three URLs
per vote; aggregate counts retain the original denominator when details are
omitted. Construction caps angle text at 256 UTF-8 bytes, claim text at 512,
reasons/conditions/errors at 256, and URLs at 512. Overlong URLs are omitted,
never changed into new addresses. Shortening or omission is disclosed. The
saved envelope is limited to 256 KiB. These limits bound retained material;
they do not increase search or voting budgets or establish content quality.

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
- A failed or empty final synthesis ends the job as `failed` with its original
  reason. The manager attempts to retain findings and the same evidence summary
  in the account-owned job directory as `synthesis-failure.json`. Auxiliary
  serialization, directory creation, write or sync failures do not prevent that
  known outcome from settling: the committed `error` keeps the synthesis reason
  and appends a bounded, credential-masked material failure reason, visible in
  snapshots, failure events and the card. The diagnostic traceback remains in
  logs. The original guarded terminal transaction commits before notification;
  database and actual retirement failures retain their existing fatal or owned
  failure boundaries. It emits no successful completion or report injection;
  a placeholder is not a completed report.
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
  `injected_at`/`error`/`report_path` and `delivery_receipt.error`. Because the report is delivered
  through the shared `admit_return` operation (§7), a job can be `completed` while
  its report still sits behind an active user turn — the card/DB must show
  completion separately from admission. `injection_status=delivered` is a
  legacy admission fact, not evidence the model processed the report.
  `delivery_receipt` binds the follow-up request and version, distinguishing
  queued, processing, processed, failed, recording_unconfirmed and unknown.
  A browser confirms the exact displayed version through the account-only
  `POST /api/sessions/{sid}/research/{rid}/seen` route; this is separate from
  model processing and does not assert human reading. Receipt and admission
  status commit together; an unchanged terminal receipt still repairs an older
  split status without changing its version or browser confirmation.
  Terminal delivery retains its original source identity and admission receipt.
  Completion, cancellation, failure and interruption share one guarded
  `running`-to-terminal transaction. Completion requires the report and evidence
  files and their directory entries to be synced before committing the completed
  row. A save refusal publishes failure, not a completed report; files left by a
  losing completion attempt are not proof of completion.
  A cancellation control captures the original pipeline and takes ownership
  before its first database wait. It survives cancellation of its HTTP waiter;
  repeated calls join the same control while it remains active. A cancelled row,
  including an already committed cancellation, signals that captured pipeline
  at most once rather than looking up a later replacement. The pipeline still
  owns actual leaf/fetch retirement. Cancellation after completion returns the
  completed job and preserves its report and delivery. The card consumes this
  response directly through its existing state merge. A boot sweep marks running jobs
  `interrupted`; v1 does NOT resume mid-pipeline.

Before admitting a job, Research captures the session overrides and Agent defaults
through SessionManager's short configuration boundary, resolving the actual model
and credential ID into the job's private request value. Capacity waits, credential
resolution and model execution occur outside that lock. A queued job therefore
keeps its admitted choice when defaults change; a later job sees the new defaults.
This capture does not yet carry selected reasoning effort or service tier into
execution: the manager passes only `model` to the orchestrator, and both web and
reasoning leaves construct their run contexts without those two controls. They
therefore retain runtime defaults rather than the selected effort/tier
(see [manager invocation](../../server/research/manager.py#L362-L378),
[pipeline arguments](../../server/research/orchestrator.py#L89-L120) and
[leaf configuration](../../server/research/leaf.py#L58-L152)).
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

Normal report delivery uses the session-owned
[`admit_return` operation](input-reliability.md#公共返回接管), preserving the
original `research:{job_id}` identity. Normal preparation reads the source job
and builds its report body only when a new input is still permitted. An existing receipt needs neither
the report material nor a functioning destination Owner. Research owns its
report, delayed retry and processing observer; it does not assemble receipt
lookup, wake-up or session admission checks itself.

Before startup admission opens, one read-only sweep covers completed reports
with pending/unknown admission and unsettled receipts, including the historical
receipt-before-status window. It queries the original source/session/delivery
identity and reconstructs the persisted input completion: a committed success
is processed, a committed failure or withdrawn input is failed, and unresolved
processing remains unknown. It never creates an Owner, restores a session,
submits an input or reruns research. Existing terminal receipt versions and
browser confirmations remain unchanged.

An absent source row proves non-admission only for an originally pending report
with a supported saved caller identity, matching account/session ownership,
healthy authoritative reads and explicit prior-writer retirement proof from
managed startup. That report stays completed, with failed delivery and a
diagnostic explaining that it was not replayed. Standalone startup, missing
identity or read uncertainty yields unknown; an already unknown admission is
not upgraded by absence alone. Storage distrust remains fail-stop rather than
being rewritten as a delivery result.

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
registration entry. Cancellation controls have a separate retained registry so
shutdown cannot interrupt their transaction or notification by signalling them
as pipelines. `wait_for_idle()` joins all these phases without closing admission;
a completed report alone does not mean its follow-up receipt has settled. Done
callbacks release only the registration they still own. Observation failures
remain visible to draining and shutdown instead of disappearing after logging.
Shutdown closes admission, captures both registries once and cancels each
live pipeline/delivery owner at most once, leaving cancellation controls to finish.
A shared deadline bounds their join. A timeout preserves that snapshot for a later join,
including cancellation cleanup still writing an unknown delivery result. Observers
handed off by a shielded original pipeline during shutdown join that
same snapshot and settle unknown without replaying the accepted request. Request
to job/session routing and exact-version browser confirmation remain separate
facts; neither is another execution owner or a prerequisite for business delivery.
An ordinary missing-job refusal preserves its caller's error without poisoning
shutdown; an unexpected control failure or cancellation remains visible as
unconfirmed cleanup. `test_research_manager.py` exercises real UPDATE/COMMIT
barriers, repeated cancellation, shutdown/rejoin and original subprocess exit.

A known preparation gap remains before any leaf starts: an `OSError` creating
the per-job scratch directory follows the generic failure handler, which can
commit `failed` and then re-raise. The registry retains that exceptional task and
its context, so drain/shutdown cannot complete even though no leaf was launched.
This is not proof of an unretired subprocess; it is the current classification
limitation (see [scratch preparation and failure handling](../../server/research/manager.py#L350-L422)
and [exceptional-task retention](../../server/research/manager.py#L135-L155)).
It does not authorize clearing genuinely unknown retirement or storage failures.

A web leaf needs a successful provider terminal. Parseable text followed by
EOF without that terminal remains diagnostic material and supplies neither
search findings nor verification votes. Cleanup failures retain the original
run and its result evidence separately from the retirement error.

## 7. Invocation, files and verification

The research tool and `/research` command admit the same account/session-owned
job. Progress and the report card are projections of that job. Within the account
research root, the report is `{job_id}.md`, the versioned summary is
`{job_id}/evidence.json`, and synthesis failure attempts to retain the summary in
`{job_id}/synthesis-failure.json`; the original
caller receives a separate follow-up with stable delivery identity.

The manager projects the same summary into GET/list snapshots and completed WS
events. Reading binds its envelope to the persisted account/session/job and a
fixed path within the account research root, accepts only regular files within
the size limit, and requires every v1 field, including nested reasons, conditions
and sources. Old jobs without a summary, missing or damaged files and unsupported
versions yield explicit unavailable evidence, clearing the card's cached summary.
Missing material after completion does not revise the committed job outcome.
This adds no database field, pipeline replay or second lifecycle owner.

Session deletion cascades job rows but follows the existing policy of retaining
account research files. The [account archive](../backup-and-migrate.md#account-export-and-import)
recursively carries these materials while excluding session/job history; restored
files do not restore cards or execution authority. Complete backups preserve both
the existing rows and their files. There is no sidecar cleanup shortcut.
The completed card retains a Download report link through
`GET /api/sessions/{sid}/research/{rid}/report`, independently of admission or
processing. The route checks the original account/session and an existing file
under that account's research root, including archived sessions. Missing material
is a 404; the path does not confer another account's access.
Creation receipts, snapshots, browser confirmations and control feedback follow
the [frontend observation contract](frontend-list-reconciliation.md#辅助任务的读取与事件合并).

Source: [manager](../../server/research/manager.py),
[orchestrator](../../server/research/orchestrator.py),
[leaf executor](../../server/research/leaf.py).
Regression entry points: `tests/test_research_orchestrator.py`,
`tests/test_research_leaf.py`, `tests/test_research_manager.py` and
`tests/test_research_session_return.py`. Manager regressions queue jobs across
Agent-default/session-override changes, credential deletion and shutdown while
configuration capture waits. `tests/test_research_api.py` deletes or archives
the real session during that wait and verifies HTTP refusal with no job created.
The same manager/API seams cover restart-before-admission, original persisted
withdrawal/unknown/terminal completion, split receipt/status repair, repeated
recovery, report downloads and foreign-account refusal. No recovery test uses
a new model input as evidence of a recovered receipt.

Research does not resume an interrupted pipeline on startup, add a new search
provider, or claim that browser display proves human reading. Current authority
follows [account scope](multi-tenancy.md); settlement follows
[original completion](input-reliability.md#原请求完成与失败交接), and resource exit
follows [Harness retirement](harness-layer.md#execution-and-resources).

## Historical evidence

The [original design and evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/native-deep-research.md) retain their original version and conditions. Current verification follows [testing.md](../testing.md); this document does not establish production deployment.

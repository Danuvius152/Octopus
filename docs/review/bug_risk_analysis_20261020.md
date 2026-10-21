# Bug Risk Analysis: before-takeover to HEAD

**Analysis Date**: 2026-10-20\
**Review Basis**: [octopus_code_review_20261020.md](octopus_code_review_20261020.md)\
**Scope**: 20 confirmed issues from code review (6 P1, 14 P2)

## Executive Summary

Based on comprehensive code review findings, **20 confirmed issues** were identified across authentication, request lifecycle, Wiki operations, and UI resilience. No P0 (critical data corruption/security breach) issues were confirmed in this review scope, though **6 P1 issues** require priority remediation before the next feature release.

The most severe risks center on **incomplete permission revocation** (CR-01), **credential leakage in logs** (CR-02), **lost request state after restart** (CR-03), and **Wiki identity confusion after file moves** (CR-18). These affect security boundaries, operational reliability, and data integrity guarantees.

---

## Critical Bugs (P0)

**None confirmed in this review scope.**

This does NOT constitute security certification or proof that no other issues exist. Full security audit, production load testing, and real provider validation remain outside this review's boundaries.

---

## High Priority Issues (P1)

### CR-01 · Account Disable Bypass via Derived Credentials

**Category**: Architecture Smell (incomplete authorization boundary)\
**Priority**: P1

**Problem**: When an account is disabled via `users.set_disabled`, only the primary auth tokens are revoked. MCP-scoped tokens and App tokens remain valid and can still access APIs.

**Trigger Conditions**:
- Administrator disables a user account
- User already holds MCP lease tokens or App tokens
- Derived credentials were issued before the disable action

**Impact**:
- **Scope**: Authorization bypass — disabled accounts retain partial API access
- **Blast Radius**: Medium — affects MCP and App entry points, but NOT proven to enable cross-account data access (IDOR)
- **Detection**: Would appear in audit logs as API calls from disabled accounts
- **Data Risk**: Unauthorized API usage; revocation policy incomplete

**Root Cause**: Permission checks fragmented across entry points. `set_disabled` updates user table and revokes primary tokens, but MCP scope validation only checks work/run lease era, and App token validation returns `_APP_ITSELF` after signature verification without checking account status.

**Evidence**: [S-DISABLE], [S-MCP-AUTH], [S-APP-AUTH] — real DB reproduction confirmed MCP/App requests still return 200 after disable.

**Remediation Priority**: Must fix before next release. All entry points must resolve real owner identity and check current enabled status. Disable should trigger derived lease revocation and supervised work retirement.

---

### CR-02 · Credentials and Private Prompts Logged at INFO Level

**Category**: Security — credential/PII leakage\
**Priority**: P1

**Problem**: `HarnessRun.start` logs complete `argv` at INFO level. For Claude, this includes MCP bearer headers and system prompts. For Codex, this includes developer/user prompts passed via argv. Full credentials and private context are written to default logs.

**Trigger Conditions**:
- Any harness start (every turn)
- Default logging configuration (INFO level)
- Log files backed up, rotated to insecure storage, or accessed by unauthorized operators

**Impact**:
- **Scope**: Privacy violation — secrets and private prompts exposed beyond runtime
- **Blast Radius**: High if logs are shipped externally or retained long-term
- **Detection**: Manual log inspection or automated secret scanning
- **Data Risk**: Active credentials leaked; user private content exposed

**Root Cause**: Direct logging of subprocess arguments without profile-aware sanitization. `safe_diagnostic` exists but doesn't cover this logging path.

**Evidence**: [S-SPAWN-LOG], [S-CLAUDE-ARGV], [S-CODEX-ARGV] — reproduction with synthetic credentials confirmed bearer and prompt text in logs. Note: Codex HTTP MCP uses env vars for bearer, so this specific leak is Claude-specific, but both backends have prompt exposure.

**Remediation Priority**: Immediate. Default logs must only record backend, model, request ID, exit status, namespace, and lengths. Diagnostic mode must use profile-aware redaction. Do NOT rely solely on regex `Bearer` replacement — covers prompts, stdio env, and future parameters comprehensively.

---

### CR-03 · Accepted Requests Lost After Service Restart

**Category**: Critical Bug (data correctness — lost work)\
**Priority**: P1

**Problem**: Client submissions save payload hash, request ID, and first receipt. Once accepted, querying returns `accepted=true`. But queued messages (not yet in transcript) are held in memory only. Graceful shutdown cancels and clears the queue. After restart, the old receipt says `accepted=true`, but there's no persisted execution state, no recoverable content, and no terminal status.

**Trigger Conditions**:
- Request 1 is executing
- Request 2 is accepted and queued
- Service performs graceful shutdown before Request 2 executes
- User queries Request 2 status or retries with same client ID

**Impact**:
- **Scope**: Request reliability — users lose queued work without notice
- **Blast Radius**: Any queued input during restart window
- **Detection**: User confusion — "I submitted this, you said accepted, where is it?"
- **Data Risk**: Silent work loss; no recovery path for unexecuted admitted requests

**Root Cause**: Immutable admission receipt conflated with mutable execution state. Queue lives in memory; shutdown doesn't persist "withdrawn-before-start" terminal status. User sees historical `accepted=true` but current reality is "never ran, content gone."

**Evidence**: [S-CLIENT-SUBMIT], [S-CLIENT-DB], [S-DRAIN] — reproduction: second request accepted, shutdown, restart, query shows `accepted=true` with no terminal, no queue, no saved payload.

**Remediation Priority**: Must fix. Separate admission (immutable) from execution (mutable). Save recoverable input + attachment refs before acceptance. Graceful shutdown records `withdrawn-before-start`. Hard crash classifies by known execution phase; absence of terminal cannot be auto-replay justification.

---

### CR-04 · Scheduled Sessions Archived Before Async Work Completes

**Category**: Critical Bug (data loss — lost async results)\
**Priority**: P1

**Problem**: Driver release detects local idle and auto-archives schedule-origin sessions. Archive condition doesn't consider external Research/bg/delegation work. Research callback requires parent session still in live map. If parent archived while research running, injection fails with "contract missing/closed/mismatched."

**Trigger Conditions**:
- Fresh scheduled session initiates async research
- Initial turn ends (session becomes locally idle)
- Research still running when auto-archive triggers
- Research completes and attempts to inject follow-up

**Impact**:
- **Scope**: Scheduled workflow breakage — "research then summarize/continue/write to Wiki" never completes
- **Blast Radius**: Any schedule-triggered work that spawns async continuations
- **Detection**: Research card shows completed, but no follow-up injected; report file saved but not processed
- **Data Risk**: Partial execution — research data collected but never delivered to intended workflow

**Root Cause**: Auto-archive checks session-local idle state only. External work managers (Research/delegation/bg) track their own state; parent session doesn't know about outstanding callbacks.

**Evidence**: [S-RELEASE], [S-ARCHIVE], [S-RESEARCH-RETURN] — reproduction: scheduled session fires research, ends turn, gets archived, research completes but injection fails.

**Remediation Priority**: Define session quiescence = all supervised work + pending callbacks complete. Trigger re-evaluation on last work completion and callback delivery. Do NOT just "never archive if research exists" without adding completion trigger — that creates opposite problem (permanent no-archive).

---

### CR-05 · Auto-Archive Leaks Held Backend Processes

**Category**: Critical Bug (resource leak)\
**Priority**: P1

**Problem**: Auto-archive directly pops session from live map. Reaper and shutdown only enumerate live sessions. Reusable backends are normally released by turn-end path. When auto-archive fires on idle scheduled session with held backend, the session disappears but backend process never stopped.

**Trigger Conditions**:
- Scheduled session completes work
- Backend in reusable mode (Claude STREAM_JSON)
- Auto-archive triggers
- Reaper/shutdown cycles run

**Impact**:
- **Scope**: Resource leak — held backend processes accumulate
- **Blast Radius**: Repeated occurrences bypass held cap, accumulate memory/file descriptors
- **Detection**: Process count grows, held backends never reaped
- **Data Risk**: DoS via resource exhaustion; no data corruption

**Root Cause**: Archive path doesn't capture backend owners before removing session. Cleanup paths only see live sessions.

**Evidence**: [S-ARCHIVE], [S-HOLD], [S-PROCESSES], [S-CLAUDE-MODE] — reproduction: idle scheduled session with instrumented reusable backend, auto-archive, reaper, shutdown: session archived, stop never called, process still alive. Current Codex uses CLOSE_AFTER_SPAWN (not affected); Claude STREAM_JSON path is.

**Remediation Priority**: Archive must first capture and retire all backend owners, confirm exit, then remove Session. Don't cancel CR-04's async work to reuse explicit archive function.

---

### CR-18 · File Move Causes History Identity Confusion and Cross-Document Corruption

**Category**: Critical Bug (data correctness — cross-document corruption)\
**Priority**: P1

**Problem**: When a Wiki task item is enqueued then the file moves, `jobs.reread` reads current path A but records bytes to `item.path` (old location), without passing actual doc identity. History inference uses old path to find "current document" — which may now be a different file B. Publication recovery also uses old path. This can:
1. Bind A's content as B's historical version (cross-document pollution)
2. Allow normal restore to overwrite B with A's bytes
3. Cause settlement errors when paths don't match expected identities

**Trigger Conditions**:
- Document A enqueued for maintenance
- A moves from `Library/a.md` to `Library/moved-a.md`
- Document B moves into old path `Library/a.md`
- Worker calls `reread` for queued item
- Later: someone restores "B's history" or recovery checks old path

**Impact**:
- **Scope**: Data integrity violation — historical versions bound to wrong documents
- **Blast Radius**: Any document that occupies a path previously held by another document
- **Detection**: Mysterious content appearing in wrong files after restore; settlement errors on valid operations
- **Data Risk**: **Cross-document data corruption via restore**; silently lost source tracking

**Root Cause**: Task item stores path; reread uses current doc but logs to old path; history inference assumes path → current doc. No stable doc_id flow through reread/recovery.

**Evidence**: [S-JOB-REREAD], [S-HISTORY-IDENTITY], [S-PUBLICATION-RECOVERY] — reproduction in temporary vault: A moved, B takes old path, reread produces conflict with B's doc_id but A's bytes. Restore B's "before" version → B overwritten with A. Settlement: A moved before write → correct identity but checks old path (404); A moved after write → checks old path, reports "Document changed."

**Remediation Priority**: **MUST FIX IMMEDIATELY. Block cross-document history pollution.** Use stable input/output doc_id throughout. Reread must bind actual read identity, path, revision, and bytes. Settlement validates recorded output_doc/output_revision, not old input path. Prohibit already-corrupted candidates from direct restore; preserve evidence.

---

## Architecture Smells (P1)

The P1 issues above (CR-01, CR-04, CR-05, CR-18) are primarily architectural in nature:

- **CR-01**: Authorization boundary incomplete — derived credential checks don't flow through unified owner resolution
- **CR-04/CR-05**: Lifecycle coupling — session local idle conflated with work quiescence; archive doesn't own resource cleanup
- **CR-18**: Identity instability — path-based tracking instead of stable doc_id through async work chains

These require **boundary fixes**, not just local patches.

---

## Performance Issues (P1)

### CR-20 · Quadratic Section Scanning for Raw Blocks

**Category**: Performance Issue (algorithmic cost)\
**Priority**: P2 (listed here as it scales badly)

**Problem**: `raw_blocks.load` generates all blocks, and for each block restarts iteration from `parsed.sections` beginning using `next(...)` to find containing section. Pagination happens after full load. `revise` calls loader even for `no_change` branches. With B blocks and S sections, each page costs O(B×S) lookups; P pages → O(P×B×S). For B≈2S, P≈B/20, total candidates examined scales ~cubic with document size.

**Trigger Conditions**:
- Long documents with many sections (400+ sections, 800+ blocks)
- Paginated read (100+ pages)
- `no_change` confirmation paths

**Impact**:
- **Scope**: Efficiency — excessive CPU in history lock
- **Blast Radius**: 1,000 sections → 101M section candidate examinations for full pagination
- **Detection**: Slow Wiki operations on long documents; lock contention
- **Data Risk**: None (correctness preserved) — purely performance waste

**Root Cause**: Linear search from section list beginning for every block; pagination repeats parse; no_change repeats block assembly.

**Evidence**: [S-RAW-BLOCKS], [S-REVISE-NO-CHANGE] — reproduction: 1,000 sections/2,001 blocks, 101 pages → 101,303,101 candidate examinations, 202 file opens, all content correct but algorithmically wasteful.

**Remediation Priority**: Use ordered interval cursor or boundary index for block→section matching. `no_change` should only verify identity, bytes/revision, encoding, grants, read receipts — not rebuild all block coordinates. Don't cache mutable tokens; still verify grants and bytes each call.

---

## Maintainability Concerns (P2)

### CR-06 · Completed Delegation Children Not Auto-Archived

**Category**: Maintainability Concern (resource accumulation)\
**Priority**: P2

**Problem**: Terminal callback injects result then immediately tries to archive, but current request/task not yet released. Release later only auto-checks schedule-origin sessions. Completed delegation children stay in live map indefinitely.

**Trigger Conditions**:
- Delegation completes
- Terminal callback runs (injects result, marks terminal_injected=true)
- Driver release finishes
- No subsequent triggers for child session cleanup

**Impact**:
- **Scope**: Session accumulation — completed children don't clean up
- **Blast Radius**: Memory/state bloat over time; not immediate failure
- **Detection**: Live session count grows; completed delegations never archived
- **Data Risk**: None — sessions still have correct state, just not cleaned up

**Root Cause**: Terminal fires before release; release only re-checks schedule origin. No archive intent bound to work completion.

**Evidence**: [S-DELEGATION-END], [S-DELEGATION-CALLBACK], [S-RELEASE], [S-DELEGATION-TEST] — reproduction: delegation completes, terminal_injected=true, driver finished, child still live, archived=false. Existing tests bypassed real ordering.

**Remediation Priority**: Terminal proposes archive intent bound to work/round; release executes when no grandchildren/callbacks/new follow-ups. Don't blanket-add all delegations to idle-archive list.

---

### CR-07 · One-Shot Schedule Silently Consumed by Human Backlog

**Category**: Maintainability Concern (workflow breakage)\
**Priority**: P2

**Problem**: `_backlogged` treats any pending queue as "already has schedule fire." One-shot exits early, then `finally` block unconditionally deletes definition. If session has queued human message when one-shot fires, schedule disappears without executing.

**Trigger Conditions**:
- Session actively executing + human message queued
- One-shot schedule triggers
- Scheduler sees backlog, returns early
- Finally block deletes one-shot definition

**Impact**:
- **Scope**: Scheduled work lost — one-shot never runs, no user notification
- **Blast Radius**: Any one-shot firing during human input backlog
- **Detection**: User expects scheduled action, never happens, no error shown
- **Data Risk**: Workflow incompleteness; user trust erosion

**Root Cause**: Backlog check conflates "busy now" with "consumed fire." Deletion happens regardless of execution. No persistent fire identity/deduplication.

**Evidence**: [S-SCHEDULE-BUSY], [S-SCHEDULE-DELETE] — reproduction: session executing + human queued, one-shot fires, finally runs, schedule row deleted, queue still only contains original human message.

**Remediation Priority**: Use schedule ID + fire identity for deduplication. Separate definition from execution record. Busy → queue or persist defer; skip needs visible reason + run record, not definition deletion.

---

### CR-08 · History Restore Trusts Filename Without Content Verification

**Category**: Maintainability Concern (integrity gap)\
**Priority**: P2

**Problem**: Normal `restore` checks blob existence and reads content, but doesn't verify SHA256 matches filename. History list treats existence as recoverable. Snapshot restore has hash validation; normal restore path omitted it. Corrupted backup object with correct filename hash → restore succeeds, overwrites current healthy content with corrupted bytes.

**Trigger Conditions**:
- Corrupted historical blob object exists (content doesn't match filename hash)
- User initiates restore from that version
- Or automated recovery selects that blob

**Impact**:
- **Scope**: Data integrity — corrupted history used as trusted recovery point
- **Blast Radius**: Any document with corrupted backup objects
- **Detection**: Restored content malformed/truncated; no error during restore
- **Data Risk**: Healthy current version replaced with corrupted historical version

**Root Cause**: Snapshot path validates hash; normal restore path doesn't. Trust filename = hash without content verification.

**Evidence**: [S-RESTORE], [S-SNAPSHOT] — reproduction: save v1→v2, corrupt v1 blob keeping filename, history lists as recoverable, restore succeeds, overwrites healthy v2 with corrupted bytes.

**Remediation Priority**: Unified verified blob reader; validate SHA256 before write. Untrusted object → preserve current file + evidence, return stable error. History list can distinguish exists/verified without full re-read every time.

---

### CR-09 · Unrelated Bad Asset Blocks All Maintenance

**Category**: Maintainability Concern (fault isolation failure)\
**Priority**: P2

**Problem**: `index_status` aggregates whole-vault document errors. `maintenance_readiness`, even for exact doc, requires global ready status. One excluded asset with YAML error → all Library notes return `ready=false`, `total=null`, `items=[]`.

**Trigger Conditions**:
- Healthy documents in maintained directories
- One excluded asset (e.g., `tmp/extraction-output.md`) has YAML parse error
- Request maintenance candidates or check specific doc readiness

**Impact**:
- **Scope**: Operational blockage — healthy docs can't be maintained due to unrelated bad asset
- **Blast Radius**: One bad excluded file blocks vault-wide maintenance UI
- **Detection**: Maintenance shows "not ready" despite target docs being healthy
- **Data Risk**: Work stoppage; no data corruption

**Root Cause**: Global identity/journal safety barrier conflated with per-document parse errors. Health check doesn't scope to target and its direct dependencies.

**Evidence**: [S-INDEX-STATE], [S-MAINT-PROJECTION] — reproduction: healthy `Library/good.md` + YAML-broken `tmp/asset.md` (excluded) → Library list and exact good doc both return ready=false. Remove asset → immediate recovery.

**Remediation Priority**: Separate vault-wide identity/journal safety from individual parse errors. Health based on target + direct evidence dependencies. Return healthy candidates + bounded exclusion summary. Don't blanket turn all partial→ready; unknown identity still blocks write.

---

### CR-10 · Read of Unchanged File Invalidates Maintenance Cursor

**Category**: Maintainability Concern (spurious invalidation)\
**Priority**: P2

**Problem**: Index fast path UPDATEs even when signature unchanged. Documents UPDATE trigger unconditionally bumps generation/dirty. Maintenance pagination cursor validates generation. Reading unrelated unchanged file → generation bump → cursor 409.

**Trigger Conditions**:
- Paginated maintenance listing (limit=1, get cursor)
- Read any other document without changing it
- Attempt to continue pagination with cursor

**Impact**:
- **Scope**: UX friction — "list candidates → read → continue listing" interrupted by own read
- **Blast Radius**: Any workflow that interleaves reading and pagination
- **Detection**: 409 cursor errors during normal read-then-paginate flow
- **Data Risk**: Wasted maintenance work; false dirty signals

**Root Cause**: Fast path doesn't skip no-op UPDATE; trigger doesn't distinguish check-time refresh from semantic change. `checked_at` refresh also has same static invalidation path.

**Evidence**: [S-INDEX-FAST], [S-TRIGGERS], [S-MAINT-CURSOR] — reproduction: Library 3 docs paginated, cursor works, read unrelated unchanged doc by 1 char, cursor now 409. index_epoch stable, maintenance generation bumped.

**Remediation Priority**: Fast path avoid no-op UPDATE; trigger monitor semantic changes (delete/move/source change/receipt status), not check metadata. Real changes still must invalidate correctly.

---

### CR-11 · Single Read Returns Mixed Transaction Versions

**Category**: Maintainability Concern (consistency violation)\
**Priority**: P2

**Problem**: `read` completes `_index` in outer transaction, then constructs return value. `public`/`dependencies` and `index_state` helpers open separate connections. Outer transaction doesn't commit until full return expression built. One read can return new content + new revision + native sources=B (latest) but attached source_refs=A + epoch=3 (stale from separate connection).

**Trigger Conditions**:
- File modified outside vault (hardlink, unobserved write)
- `read` discovers change (triggers synchronize)
- Response built before synchronize consumed by separate-connection helpers

**Impact**:
- **Scope**: Data consistency — one response mixes two transaction snapshots
- **Blast Radius**: Any read that discovers unobserved changes
- **Detection**: First read shows mismatched metadata, second read consistent (after synchronize applied)
- **Data Risk**: Consumer sees inconsistent state; could make decisions on mixed data

**Root Cause**: Response projection helpers open separate DB connections instead of reusing outer transaction. Content, coordinates, source relations, index epoch must belong to one version.

**Evidence**: [S-READ], [S-PUBLIC], [S-STORE-STATE] — reproduction: modify via hardlink (watcher misses), read: new content/revision/native B, old source_refs=A/epoch=3. Second read: all consistent.

**Remediation Priority**: Response projection reuses same transaction connection, or commit first then assemble in controlled consistent snapshot. Content + coordinates + sources + epoch = one definite version. Don't require model to read twice to compensate.

---

### CR-12 · App Wiki Adapter Parameter Routing and Interpretation Inconsistent

**Category**: Maintainability Concern (API contract divergence)\
**Priority**: P2

**Problem**: App adapter's `elif schema` branch handles POST body, blocking subsequent `elif operation.startswith("jobs/")`. `job_id` not passed to handler. Also, `include_assets` manually does `.lower()=="true"`, bypassing main entry boolean validation. Illegal values silently treated as false instead of 422.

**Trigger Conditions**:
- `POST /apps/{id}/wiki/jobs/{job_id}/cancel` or `/retry`
- Query `include_assets=1`, `yes`, or invalid value

**Impact**:
- **Scope**: API inconsistency — App entry diverges from main API semantics
- **Blast Radius**: App-invoked Wiki operations fail or behave differently than main API
- **Detection**: 500 errors on job control; silent boolean misinterpretation (assets missing or extra)
- **Data Risk**: Operation failures; behavioral confusion between entry points

**Root Cause**: Dynamic adapter logic short-circuits job routes; manual boolean parsing differs from main validation.

**Evidence**: [S-APP-WIKI] — reproduction: cancel/retry return 500 "missing job_id." Main API `include_assets=1` returns 2 docs, App returns 1. Main `include_assets=invalid` → 422, App → silent false/200. Asset-only search: main returns 1, App returns 0.

**Remediation Priority**: Parse path/query/body independently; share type validation/defaults. Legal boolean forms interpreted consistently; illegal → 422. Add direct bug regression + share typed business service (not HTTP handler kwargs shuffling). Preserve App-specific binding/permission restrictions.

---

### CR-13 · App Proxy Leaks Query Token and Collapses Multi-Value Params

**Category**: Maintainability Concern (parameter handling error)\
**Priority**: P2

**Problem**: Proxy intentionally removes authorization/cookie headers, but `params=dict(request.query_params)` forwards query token verbatim and merges duplicate keys (last wins).

**Trigger Conditions**:
- App backend accessed via `?token=...&tag=one&tag=two`
- Or any business param with multiple values

**Impact**:
- **Scope**: Parameter leakage + semantic loss
- **Blast Radius**: Query-based auth tokens forwarded to untrusted backend; repeated params collapsed
- **Detection**: Backend receives Octopus token; only last value of repeated param preserved
- **Data Risk**: Auth token exposure (though App already has trusted local execution capability); business logic errors from lost multi-value params

**Root Cause**: Blanket dict conversion of query params; no filtering of Octopus-consumed auth params.

**Evidence**: [S-APP-PROXY], [S-APP-TOKEN] — reproduction: synthetic request with token + tag=one&tag=two, upstream captures token plaintext, tag only contains "two."

**Remediation Priority**: Filter Octopus-consumed auth params; preserve multi-value list with order, empty values, Unicode. Note: query token path has issue; default cookie iframe doesn't necessarily carry token. App has trusted local capability already (not new sandbox escape).

---

### CR-14 · Last Wiki Notification Refresh Failure Has No Auto-Recovery

**Category**: Maintainability Concern (UI resilience gap)\
**Priority**: P2

**Problem**: WS handler's refresh exception has empty catch. Loading clears without preserving target version or retry. Card only reloads on mount/connect/identity changes. Last `changed/v2` event arrives, REST temporarily 503, WS stays connected — no more events. Service recovers, but UI stuck showing v1.

**Trigger Conditions**:
- Running Wiki job at v1
- Final `changed/v2` event arrives
- REST temporarily fails (503/timeout)
- WS connection remains open
- No subsequent events

**Impact**:
- **Scope**: UI staleness — user sees outdated state, no indication of sync failure
- **Blast Radius**: Any final status update during transient REST failure
- **Detection**: Job completes but UI shows old status; manual reconnect/refresh fixes it
- **Data Risk**: User confusion; perceived incomplete work (actual work completed)

**Root Cause**: No target version preservation; no bounded retry + jitter. Card effect doesn't handle "failed to sync latest known version."

**Evidence**: [S-WS], [S-WIKI-JOBS-CLIENT], [S-CARDS] — reproduction: UI at v1, last event v2, REST fails, 120s pass, still shows v1. Reconnect/reopen fixes. 13 existing tests pass; new recovery regression fails.

**Remediation Priority**: Save "target version not yet synced," bounded retry + jitter, show "state not synced" + retry entry when budget exhausted. Snapshot overwrites target → clear dirty. Identity/generation change → cancel old callback, don't start new model turn.

---

### CR-15 · Prompt Advertises Unavailable Tools

**Category**: Maintainability Concern (prompt inconsistency)\
**Priority**: P2

**Problem**: MCP selection and prompt assembly separated. `compose` unconditionally inserts profile's full tool descriptions. Web research leaf explicitly sets `mcp_servers=[]` + only web tool, but still gets bg/ask/delegation/schedule guidance in system prompt.

**Trigger Conditions**:
- Any run context with tool subset (e.g., research leaf)
- MCP entries filtered but prompt not correspondingly filtered

**Impact**:
- **Scope**: Prompt confusion — instructions reference unavailable tools
- **Blast Radius**: Added token cost; potential model hallucination of tool calls
- **Detection**: Inspect assembled prompt vs actual MCP entries
- **Data Risk**: Wasted tokens; incorrect model behavior (not proven in this review that model always hallucinates, but risk exists)

**Root Cause**: Capability set determines MCP entries independently from prompt generation. Pruned capability set doesn't prune corresponding guidance.

**Evidence**: [S-ASSEMBLY], [S-CONTEXT], [S-LEAF] — reproduction: web-only RunConfig, 0 MCP entries, system prompt contains all 4 tool class guidance, 4,762 UTF-8 bytes.

**Remediation Priority**: Resolved capability set generates both MCP entries AND prompt. Pruned leaf only includes corresponding guidance. Unknown/unavailable capabilities not persona-fabricated.

---

### CR-16 · Oversized Persona Deferred to Exec Failure (E2BIG)

**Category**: Maintainability Concern (validation gap)\
**Priority**: P2

**Problem**: AgentCreate/Update has unbounded `system_prompt`. Codex encodes developer instructions into one TOML argv element. Large prompt spill only covers user messages, not persona. Oversized persona accepted by API, then fails at exec with E2BIG.

**Trigger Conditions**:
- Agent persona > ~130KB UTF-8
- Start session with that agent (Codex)
- Persona encoded into argv

**Impact**:
- **Scope**: Late failure — configuration accepted, runtime fails
- **Blast Radius**: Any agent with very large persona (uncommon but possible)
- **Detection**: Exec errno=7/E2BIG at harness start
- **Data Risk**: User cannot use configured agent; confusing error (not validation error at config time)

**Root Cause**: API lacks byte budget validation; argv construction doesn't pre-check OS limits.

**Evidence**: [S-AGENT-MODEL], [S-CODEX-ARGV], [S-LARGE-PROMPT] — reproduction: 45K characters (135K UTF-8), final argv element 140KB. `/bin/true` with same args → E2BIG. Real Codex not executed.

**Remediation Priority**: Validate both config and serialized argv against byte budgets, preserve platform margin, fail at config/start-prep with interpretable error. Long-term: use provider-supported instruction transport. Don't silently downgrade persona to user attachment; don't claim stdin controller test proves argv unlimited.

---

### CR-17 · Organize Without Patch Shadows Native Sources

**Category**: Maintainability Concern (metadata consistency)\
**Priority**: P2

**Problem**: `organize` without `frontmatter_patch` inherits omitted sources from `managed_notes.metadata`. Adding relations writes `refs + linked` to managed sources, shadowing native YAML sources (which remain in file). Effective `sources`/`source_refs` only show new relations; native sources lost from view. Source changes no longer trigger stale flag.

**Trigger Conditions**:
- Note has native YAML `sources: [A]`
- Call `organize` adding relation B, omit sources field
- Managed metadata overwrites sources with [B]
- Native YAML still contains A (bytes unchanged)

**Impact**:
- **Scope**: Metadata consistency — effective sources diverge from native YAML
- **Blast Radius**: Any organize call adding relations without explicit source handling
- **Detection**: Source A changes but `stale=false`; effective sources missing native entries
- **Data Risk**: Lost dependency tracking; missed staleness detection

**Root Cause**: Two write paths interpret "omit sources" differently. Organize: omit → inherit managed, then shadow with relations. Normalize: omit → inherit current effective, merge relations. Managed metadata hides native YAML.

**Evidence**: [S-ORGANIZE-SOURCE-INHERIT], [S-ORGANIZE-SOURCE-SHADOW], [S-NORMALIZE-SOURCE-MERGE] — reproduction: native A, organize adds B without sources, effective=[B] only. A changes, refresh, read: stale=false. Same input with frontmatter_patch → preserve A+B, A change → stale=true.

**Remediation Priority**: Unify omit/explicit-empty/explicit-replace semantics. Omit → inherit current effective sources + identity/version proof, append new relations. Don't hide native YAML with managed metadata. Don't auto-confirm new source versions.

---

### CR-19 · Dual Heading Parsers Cause Anchor False Positives/Negatives

**Category**: Maintainability Concern (format inconsistency)\
**Priority**: P2

**Problem**: Main format parsing uses CommonMark tokens. `links.headings` manually parses lines for ATX headers and fences. `anchor_exists` and `source_span` use second structure. Setext headings (`Title\n====`) recognized by CommonMark but not manual parser → valid anchor rejected. Code fence with backticks in content line followed by `# Fake` → manual parser exits fence early, accepts non-existent anchor.

**Trigger Conditions**:
- Setext heading in target doc: `publish` link to `#title` rejected
- Fence with content line starting with 3 backticks + non-whitespace: fake heading accepted as anchor

**Impact**:
- **Scope**: Link validation inconsistency — rejects valid, accepts invalid
- **Blast Radius**: Any Setext headings or complex fenced code blocks
- **Detection**: 422 "anchor does not exist" for valid heading; silent acceptance of non-existent anchor
- **Data Risk**: Broken valid workflows; false confidence in invalid links

**Root Cause**: Two independent heading/fence parsers. Validation uses manual line-scan structure instead of authoritative CommonMark parse.

**Evidence**: [S-FORMAT-HEADINGS], [S-LINK-HEADINGS], [S-LINK-VALIDATION] — reproduction: `Topic\n=====` in target, link `#topic` → 422. Fake heading in complex fence → publish succeeds with non-existent anchor.

**Remediation Priority**: Derive headings, section ranges, anchor validation from same CommonMark structure. Preserve char offsets, duplicate headings, slug rules. Source span location also uses same structure. Don't skip link validation to "fix" false rejection.

---

## Test Coverage Gaps

From §6 of the review:

**Tested**:
- 364 existing backend tests passed (hermetic tier)
- 13 existing frontend tests passed
- 9 Wiki additional repro tests: 6 failed (confirming bugs), 3 passed (confirming correct paths)
- 800-step continuous usage with deterministic curator (4 batches + 5 boundary scenarios)
- 4 SIGKILL writer boundaries
- 3 disk error injections (ENOSPC, EACCES, EIO)
- 12 main API / App parameter comparisons

**Not Tested** (review scope limitations):
- Full backend hermetic suite (only targeted selections)
- Complete frontend suite
- TypeScript compilation
- Lint/generated-contract/docs-index
- Browser E2E tests
- Real Codex/Claude CLI execution
- Production load,断电 (power-loss) durability
- Real model content quality decisions

**Key Coverage Gaps** (from §6.7):
- Arbitrary operation orderings (added 2×80-step random sequences)
- Full service hard interrupt (only Wiki writer subprocess SIGKILL tested)
- Multi-writer + overlapping cancel/retry + late callbacks
- Complex source graph (fan-in/fan-out, cycles, location recovery)
- UI network failure recovery modes
- Scale limits (production p95, peak memory, real provider usage)
- Natural model decision quality (only deterministic curator tested)

---

## Summary by Category

| Category | Count | Issues |
|----------|-------|--------|
| **P1 (Priority Fix)** | **6** | CR-01 (auth bypass), CR-02 (log leak), CR-03 (lost requests), CR-04 (premature archive), CR-05 (process leak), CR-18 (identity confusion) |
| **P2 (Near-term Fix)** | **14** | CR-06 (delegation accumulation), CR-07 (one-shot loss), CR-08 (restore trust), CR-09 (bad asset blocks all), CR-10 (spurious invalidation), CR-11 (mixed transaction), CR-12 (App adapter divergence), CR-13 (proxy params), CR-14 (UI recovery gap), CR-15 (prompt mismatch), CR-16 (late E2BIG), CR-17 (source shadow), CR-19 (dual parsers), CR-20 (quadratic cost) |

**Architectural Themes**:
- Incomplete authorization boundaries (CR-01, CR-18 identity flow)
- Lifecycle coupling and resource ownership unclear (CR-03, CR-04, CR-05, CR-06)
- Transaction/consistency boundaries leaky (CR-11, CR-17)
- Validation gaps at wrong layer (CR-02 logs, CR-16 late validation)
- Fault isolation incomplete (CR-09, CR-14)

**Next Steps** (from §7):

1. **WP1**: Account revocation + log safety + proxy filtering (CR-01, 02, 13)
2. **WP2**: Session/resource unified retirement (CR-04, 05, 06)
3. **WP3**: Durable request + schedule fire identity (CR-03, 07)
4. **WP4**: Wiki consistency + sources + move + anchors + section cost (CR-08, 09, 10, 11, 17, 18, 19, 20) — **CR-18 cross-document corruption should be sealed first**
5. **WP5**: App adapter + UI recovery (CR-12, 14)
6. **WP6**: Prompt assembly + testable efficiency (CR-15, 16)

---

## References

Full detailed analysis, reproduction evidence, and source links: [octopus_code_review_20261020.md](octopus_code_review_20261020.md)

Implementation remediation contracts: [octopus_implementation_spec_20261020.md](octopus_implementation_spec_20261020.md)

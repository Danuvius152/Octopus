# Documentation rules

Use [README.md](README.md) to find the existing owner before
creating a document. Follow [the root rules](../AGENTS.md) for task scope,
verification and repository actions.

## Ownership

- Current guides describe current behavior. Architecture explains component
  relationships, state ownership, failures and extension points.
- `README.md` owns task navigation; [catalog.md](catalog.md) owns the complete
  generated contract/plan indexes and version-evidence lists;
  [roadmap.md](roadmap.md) owns upcoming choices, priorities, dependencies and
  start conditions. Detailed designs stay in their topic plans.
- `implemented/` owns contracts the current code actually follows, by subject:
  behavior, ownership, limits and necessary reasons. Revise the owning contract
  when behavior changes; code/doc disagreement needs investigation, not a move
  based only on an old plan's completion label.
- `plans/` records current unmet needs (`proposed`, `in-progress` or `paused`)
  and pointers to authorized active specifications. Unselected implementation
  designs belong in task materials, not the current architecture. On completion,
  integrate the verified contract and necessary rationale into `implemented/`
  and remove the superseded execution material.
- [testing.md](testing.md) owns verification policy; [local-wsl.md](local-wsl.md)
  owns local operating procedures. Link to their details from other documents.
- `reference/` describes protocol fields and compatibility used by current
  drivers. Retain version and provenance for raw fixtures consumed by tests;
  keep behavior and resource ownership in the corresponding current contract.
- `review/`, repository `.scratch/` and the account workspace are temporary.
  Only grill documents and their derived specs/tickets use `.scratch/`;
  logs, scripts and all other temporary artifacts use the account workspace,
  including those from grill tasks. Follow
  [local task conventions](agents/issue-tracker.md). Preserve active paths until
  their consumers can be updated safely. Test state has separate roots.
  Transfer current rules, necessary reasons and regression entry points to
  their durable owner before cleanup. Preserve active tasks and recovery state.
  Remove superseded development narratives instead of keeping another archive,
  post-mortem or lessons directory. Git retains the development history.
- Persona owns role and collaboration behavior. Memory owns stable user
  preferences, role methods and a few project entry points, kept with the
  responsible role. Detailed project navigation belongs in `docs/README.md`. Shared
  memory holds agreements that actually apply across roles; shared Wiki query
  access does not make development, review or research memory common.
  Engineering contracts belong in the
  owning project documents; runtime permissions come from the current tools
  and operation context. See [the memory contract](implemented/memory.md).

## Writing and maintenance

- For implementation tasks, consolidate durable documentation at the close of
  the agreed delivery scope, after implementation is settled, required checks
  pass and required code-review fixes are resolved. Keep intermediate findings
  in existing temporary task materials when needed. Then update the owning
  guides and `implemented/` contracts with verified behavior and necessary
  rationale, and check documentation and code consistency before delivery.
  Work-package documentation requirements assign responsibility at that delivery
  boundary; they do not require a rewrite after each patch or review round.
- Give each explanation one owner. Keep the complete local contract together:
  behavior, timing, ownership, failure and consequences for its consumers.
- Start with the question the page answers: current behavior, proposed change,
  next choice or version observation. When implementation completes, update
  the body as well as its status; retain unfinished scope in its topic plan.
  Split by reader task and responsibility, preserving necessary local context
  and reasons rather than enforcing a line-count limit.
- Preserve conditions, exceptions, negative guarantees and useful rationale
  when shortening. Remove repetition and implementation-session narration.
- All project documentation and code comments describe current behavior,
  constraints, applicability and necessary reasons. Do not retain AI development
  or modification history: temporary task phases, ticket labels, review rounds,
  approval stories, change timelines or chains of corrective appendices.
  For example, a task's temporary `phase1` through `phase5` plan leaves no phase
  labels or execution narrative in maintained documentation or code comments;
  describe the resulting code and project state directly.
- When a confirmed decision B supersedes decision A, rewrite the owning
  explanation for B and remove A's obsolete rules and the A-to-B change story.
  Keep B's necessary current rationale; Git retains the modification history.
- Ordinary fixes update their existing owner when needed; they do not require
  a separate plan, decision record, post-mortem and memory entry each time.
- Check code, confirmed requirements and runtime evidence separately. If code
  differs from a confirmed requirement, state the discrepancy and its impact;
  an implementation deviation does not become a business rule through editing.
  Distinguish implementation, validation and deployment.
- Use source or existing generators for exhaustive API/configuration lists.
  Keep relationships and decision guidance in authored prose.
- Repair inbound links in the same change as a move, rename or deletion.
- Keep a tutorial's ordered steps distinct from a reference's lookup scope.
  Split a page when that makes a real lookup easier; preserve existing anchors.

## Verification

Check changed facts against their strongest available source, changed local
links and anchors, both generated indexes and `git diff --check`. Follow
[testing.md](testing.md#release-and-documentation-checks) for applicable checks.

Prompt text that reaches a model can change behavior. Identify the actual
runtime source and verify affected representative tasks when applying it.
Save persona changes through Agent settings; editing documentation examples
alone does not update saved agent settings. Keep unchanged settings intact.

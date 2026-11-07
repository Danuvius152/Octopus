# Documentation rules

Use [README.md](README.md) to find the existing owner before
creating a document. Follow [the root rules](../AGENTS.md) for task scope,
verification and repository actions.

## Ownership

- Current guides describe current behavior. Architecture explains component
  relationships, state ownership, failures and extension points.
- `README.md` owns document discovery and generated status indexes;
  [roadmap.md](roadmap.md) owns upcoming choices, priorities, dependencies and
  start conditions. Detailed designs stay in their topic plans.
- `implemented/` owns contracts the current code actually follows, by subject:
  behavior, ownership, limits and necessary reasons. Revise the owning contract
  when behavior changes; code/doc disagreement needs investigation, not a move
  based only on an old plan's completion label.
- `plans/` owns unfinished changes (`proposed`, `in-progress` or `paused`), their
  dependencies and completion conditions. On completion, integrate the current
  contract and rationale into `implemented/` and remove the superseded plan;
  a mixed document must be split before moving it.
- [testing.md](testing.md) owns verification policy; [local-wsl.md](local-wsl.md)
  owns local operating procedures. Link to their details from other documents.
- `reference/` holds version-bound protocol observations and experiments. State
  their version, method, observed result and limits; link current contracts.
  Historical evidence does not determine today's implementation or deployment.
- `review/`, repository `.scratch/` and the account workspace are temporary.
  Grill intermediate artifacts and their derived specs/tickets use `.scratch/`;
  other new tasks and isolated worktrees default to the account workspace under
  [local task conventions](agents/issue-tracker.md). Preserve active paths until
  their consumers can be updated safely. Test state has separate roots.
  Transfer useful decisions, reasons, applicability and regression evidence to
  their durable owner before cleanup. Preserve active tasks and recovery state.
  Use `post-mortems/` selectively for incidents whose causal evidence helps
  future diagnosis. Git preserves superseded timelines; dated evidence does not
  establish current behavior or deployment.
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
- Ordinary fixes update their existing owner when needed; they do not require
  a separate plan, decision record, post-mortem and memory entry each time.
- Check current behavior against code and evidence. Label proposals and
  unverified claims; distinguish implementation, validation and deployment.
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
[agent-prompts.md](agent-prompts.md) is a copyable snapshot; editing it alone
does not update saved agent settings. Keep unchanged settings intact.

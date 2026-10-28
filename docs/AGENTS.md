# Documentation rules

Use [README.md](README.md) to find the existing owner before
creating a document. Follow [the root rules](../AGENTS.md) for task scope,
verification and repository actions.

## Ownership

- Current guides describe current behavior. Architecture explains component
  relationships, state ownership, failures and extension points.
- `plans/` owns durable contracts, decisions, rationale and remaining scope by
  subject. Revise the current section when behavior changes; consolidate
  overlapping plans. Distinguish current contracts, proposed work and
  version-bound evidence. Record genuine dependencies in the owning plan.
- [testing.md](testing.md) owns verification policy; [local-wsl.md](local-wsl.md)
  owns local operating procedures. Link to their details from other documents.
- Reviews and post-mortems retain the revision, conditions and evidence needed
  to interpret them. Git preserves superseded implementation timelines; dated
  evidence does not establish current behavior or deployment.
- Persona owns role and collaboration behavior. Memory owns stable user
  preferences, locations and pointers, kept with the responsible role. Shared
  memory holds agreements that actually apply across roles; shared Wiki query
  access does not make development, review or research memory common.
  Engineering contracts belong in the
  owning project documents; runtime permissions come from the current tools
  and operation context. See [the memory contract](plans/memory.md).

## Writing and maintenance

- Give each explanation one owner. Keep the complete local contract together:
  behavior, timing, ownership, failure and consequences for its consumers.
- Preserve conditions, exceptions, negative guarantees and useful rationale
  when shortening. Remove repetition and implementation-session narration.
- Check current behavior against code and evidence. Label proposals and
  unverified claims; distinguish implementation, validation and deployment.
- Use source or existing generators for exhaustive API/configuration lists.
  Keep relationships and decision guidance in authored prose.
- Repair inbound links in the same change as a move, rename or deletion.
- Keep a tutorial's ordered steps distinct from a reference's lookup scope.
  Split a page when that makes a real lookup easier; preserve existing anchors.

## Verification

Check changed facts against their strongest available source, changed local
links and anchors, the generated plans index and `git diff --check`. Follow
[testing.md](testing.md#release-and-documentation-checks) for applicable checks.

Prompt text that reaches a model can change behavior. Identify the actual
runtime source and verify affected representative tasks when applying it.
[agent-prompts.md](agent-prompts.md) is a copyable snapshot; editing it alone
does not update saved agent settings. Keep unchanged settings intact.

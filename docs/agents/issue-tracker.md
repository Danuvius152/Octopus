# Local tasks, specs and tickets

This project uses local Markdown files as its issue tracker. Skills such as
`to-spec`, `to-tickets`, `triage` and `wayfinder` read and write these files.
There is no external issue tracker; a GitHub Git remote does not change that.

## Task directory

Grill discussion drafts and intermediate artifacts use `.scratch/<task>/` in
this repository. A spec, tickets or map derived from that discussion stay in
the same task directory. Existing grill tasks keep their current paths.

Other new temporary tasks default to `<account-workspace>/<task>/`. Resolve
the account workspace from the session/account context; the local layout is in
[local-wsl.md](../local-wsl.md#临时任务目录). A user-specified task directory
takes precedence. Use one directory per task, not copies in both roots.

## Placement

Keep temporary analysis, specs, tickets, investigation scripts, experiments,
handoffs and task artifacts together in the selected task directory. New
isolated source worktrees default to the account workspace; edit deliverable
code in the selected repository checkout or worktree. Reusable product code
and regression tests belong in that repository.

Test databases, synthetic accounts, provider output and other mutable runtime
state use the separate roots required by [testing.md](../testing.md). A source
worktree under the account workspace does not make its task directory a valid
isolated test-state root. Test launchers must still select independent state and
provider homes. `~/.cache/octopus-tests/` is a local testing location, not the
default for source worktrees or general task materials.

Preserve existing tasks while processes, review links or recovery depend on
their paths. Moving an active task requires updating those consumers, not just
moving files. `docs/review/` remains available for requested temporary review
reports; managed application/research output and private publication resources
keep their owners.

## Files and operations

- A spec, when needed: `<task-directory>/spec.md`.
- Separate implementation tickets, when needed: `issues/01-<slug>.md`, numbered
  from `01` in dependency order, one file per ticket.
- Record `Status:`, `Blocked by:`, intended behavior and acceptance criteria
  near the top. Reference the relevant source revision. `Blocked by:` names
  exact ticket paths, relative to the task directory or absolute when needed.
- “Publish to the issue tracker” means save these local files. “Fetch a ticket”
  means read its file. A bare number resolves only within an identified task;
  ask when the task or file reference is ambiguous.
- Record clarification and evidence in the same ticket. Append conversations
  under `## Comments` instead of creating a second copy of the ticket.
- Use the triage states in [triage-labels.md](triage-labels.md). For triage,
  record one category (`bug` or `enhancement`) and one triage state.
- Small, well-defined work can proceed directly from the conversation. Creating
  a spec, tickets or a map is not an acceptance requirement for every task.

## Wayfinding and execution

- A map, when needed, is `<task-directory>/map.md`, linking its child tickets
  under `issues/`. Use `Type: research`, `prototype`, `grilling` or `task`.
- A ticket is unblocked when all tickets named by `Blocked by:` are `resolved`.
  Read blocker status before starting; missing references are unresolved.
- The frontier contains open, unblocked and unclaimed tickets. Select the first
  eligible ticket by number unless the user chooses one.
- Claim by saving `Status: claimed` before work. Resolve by recording the
  answer and evidence under `## Answer`, setting `Status: resolved`, and adding
  a context pointer to the map when one exists.
- `claimed` and `resolved` are execution states, in addition to the five triage
  labels. `needs-info` records a missing prerequisite; `wontfix` closes a task
  without implementation. Status changes do not grant implementation,
  publication or production authority.

## Lifecycle

`.scratch/`, `docs/review/` and the account workspace are temporary. Before
cleanup, put lasting decisions, their reasons and applicability, and concise
regression evidence in the owning project document. Current contracts belong
in `docs/implemented/`; unfinished formal plans belong in `docs/plans/`;
verification policy belongs in `docs/testing.md`. A confirmed grill decision
does not establish implemented behavior. Follow [domain.md](domain.md) for
document consumption and placement.

Final durable links point to those owners. Do not remove material still needed
by running work, an active review or recovery. Machine-specific publication
tools and unfinished release evidence stay in their managed operations location,
as described in [local-wsl.md](../local-wsl.md).

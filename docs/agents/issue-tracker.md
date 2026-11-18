# Local tasks, specs and tickets

This project uses local Markdown files as its issue tracker. Skills such as
`to-spec`, `to-tickets`, `triage` and `wayfinder` read and write these files.
There is no external issue tracker; a GitHub Git remote does not change that.

## Task directory

Grill documents use `.scratch/<task>/` in this repository: discussion drafts,
design and terminology notes, and derived specs, tickets and maps. Keep
existing grill documents at their current paths.

All other temporary artifacts use `<account-workspace>/<task>/`, including
those produced while implementing or verifying a grill task: logs, debugging
and verification scripts, experiment output, downloaded sources, review
reports, patches and manifests. A Markdown extension alone does not make an
artifact a grill document. Ordinary temporary tasks also use this workspace.
Resolve the account workspace from the session/account context; the local
layout is in [local-wsl.md](../local-wsl.md#临时任务目录). A user-specified task
directory takes precedence.

Use the same task name in both roots when a grill task needs working artifacts.
Link its `.scratch/` documents to the workspace evidence instead of duplicating
files. For example, `.scratch/<task>/spec.md` stays in the
repository, while verification logs and scripts use
`<account-workspace>/<task>/`. Set output paths explicitly when
running from the repository or reading a `.scratch/` ticket.

## Placement

Keep each artifact in its assigned root above; a grill document's location
does not determine where its implementation or verification output is written.
New isolated source worktrees default to the account workspace; edit deliverable
code in the selected repository checkout or worktree. Reusable product code
and regression tests belong in that repository.

Test databases, synthetic accounts, provider output and other mutable runtime
state use the separate roots required by [testing.md](../testing.md). A source
worktree under the account workspace does not make its task directory a valid
isolated test-state root. Test launchers must still select independent state and
provider homes. `~/.cache/octopus-tests/` is a local testing location, not the
default for source worktrees or general task materials.

Preserve existing artifacts while processes, review links or recovery depend
on their paths. Move older non-document artifacts from `.scratch/` or
`docs/review/` to the workspace once those consumers can be updated safely;
write new artifacts to the assigned root immediately. Moving an active task
requires updating its scripts and references, not just moving files. Managed
application/research output and private publication resources keep their owners.

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
- Maintain current decisions, acceptance criteria and blocking facts in the
  same ticket. Link necessary evidence; do not copy conversation logs into
  durable project knowledge.
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

### Implement tickets

1. Keep the design context through `to-tickets` when preparing a spec and its
   tickets. For implementation, select an eligible ticket and confirm its
   baseline includes its resolved blockers. On resume, check the current
   candidate, recorded evidence and running checks before claiming work.
2. Use `implement` for a single ticket. For an authorized multi-ticket batch,
   one coordinating session starts one eligible ticket at a time, each in a
   fresh implementer context. Give the implementer the ticket, spec and current
   integration candidate. The coordinator owns integration and tracker updates.
3. Use `tdd` where possible at the agreed test seams. Run the ticket's
   [affected checks](../testing.md#fast-checks-during-development)
   and review its fixed diff against the acceptance criteria and repo standards.
   Return a recoverable candidate and links to checks and review results. The
   coordinator accepts and integrates the ticket, records the integrated
   revision or snapshot and evidence under `## Answer`, and sets `Status: resolved`.
4. After the current ticket resolves, the coordinator automatically starts the
   next eligible ticket in the authorized batch from that integrated candidate.
   If acceptance fails, address it on the current ticket. On interruption,
   leave pointers to the spec, candidate and current or next eligible ticket
   so a coordinator can resume from the recorded state.

For a multi-ticket delivery batch, hand off the accepted changes as a recoverable
candidate and commit at the agreed delivery boundary. Follow the
[verification policy](../testing.md#fast-checks-during-development) for the
combined regression and full gate schedule.

## Lifecycle

`.scratch/`, `docs/review/` and the account workspace are temporary. Before
cleanup, put current rules, necessary reasons and regression entry points in
the owning project document. Current contracts belong
in `docs/implemented/`; unmet needs and active-specification pointers belong in `docs/plans/`;
verification policy belongs in `docs/testing.md`. A confirmed grill decision
does not establish implemented behavior. Follow [domain.md](domain.md) for
document consumption and placement.

Final durable links point to those owners. Do not remove material still needed
by running work, an active review or recovery. Machine-specific publication
tools and unfinished release evidence stay in their managed operations location,
as described in [local-wsl.md](../local-wsl.md).

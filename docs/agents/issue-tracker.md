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

Keep the design context through `to-tickets`. One Octo main session implements
an authorized batch in dependency order, one ticket at a time. Octo owns the
tracker, integration and final delivery.

1. **Claim.** Register the authorized batch in the main chat with
   `mcp__ask_agent__begin_ticket_batch` before working on its first ticket.
   Read the spec and ticket, confirm its blockers are resolved in the
   current candidate, and state the ticket title. On resumption, check the
   existing claim, worktree, checks and delegations. Before claiming another
   ticket or starting final acceptance, use the previous ticket's handoff ID
   to check whether its continuation started and where to resume it.
2. **Implement and test.** Use `implement` and, where possible, `tdd` at the
   agreed seams. Octo must pass the ticket's [affected checks](../testing.md#fast-checks-during-development),
   including applicable type checks. Pin the baseline and a recoverable
   candidate that includes new files. Save the commands, results and evidence
   for that candidate.
3. **Review.** Open a [named delegation](../implemented/agent-collaboration.md#invocation-and-identity)
   with `name="Vera"` for each ticket, starting at design time when a decision
   needs early review. For final review, provide the ticket, baseline, fixed
   diff, check results and open questions. Vera assesses the candidate against
   the acceptance criteria and repository standards. Octo reads her verdict,
   fixes substantiated findings, reruns the affected checks and returns the
   revised candidate to the same `delegation_id` for verification. The next
   ticket gets a new Vera conversation.
4. **Accept.** Keep the ticket `claimed` until its criteria are met, its checks
   pass and Vera's independent review is complete for the accepted version.
   In one ticket update, set `Status: resolved` and record the integrated
   revision or snapshot, passing checks, Vera's delegation ID and conclusion
   under `## Answer`. If the user authorized further work, include the next
   eligible ticket within that scope or the agreed final acceptance and a
   stable handoff ID marked pending. Otherwise stop at this ticket.
5. **Start the next phase.** Use the recorded ID with the
   [ticket handoff tool](../implemented/input-reliability.md#ticket-handoffs).
   If the installation lacks automatic clearing, leave the handoff pending.
   When available, the runtime starts the named
   phase automatically in a fresh model context in the same Octo chat. Keep
   the transcript visible to the user. Carry forward the user's scope and
   constraints, the spec, the accepted candidate and pointers to the next work
   and its evidence. Record confirmation against the same handoff ID once the
   fresh context receives its first input. After an interruption or lost
   receipt, reconcile that ID before advancing. An unknown result stays pending.
   Honor Stop and new human instructions before continuing the batch. A
   separate human request supersedes the automatic batch. To resume an
   authorized batch after a pause, register under the new human request.
6. **Deliver the batch.** After the last ticket, Octo checks the integrated
   behavior across tickets and runs the [full gate](../testing.md#full-checks-before-code-commits)
   once on the final candidate. Complete the applicable [release checks](../testing.md#release-and-documentation-checks)
   before the authorized commit, push and deployment.

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

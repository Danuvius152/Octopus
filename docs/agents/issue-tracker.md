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

### Skill phases

Start each phase when the user requests it and reuse the design context.
`grill-with-docs` ends with design questions, answers and notes. `to-spec`
ends with the spec in the local issue tracker. `to-tickets` ends with the
approved tickets in that tracker. Register and claim a batch after the user
authorizes implementation.

### Implement tickets

One Octo main session implements an authorized batch in dependency order,
one ticket at a time. Octo owns the tracker, integration and final delivery.

1. **Claim.** Register the authorized batch in the main chat with
   `mcp__ask_agent__begin_ticket_batch` before working on its first ticket.
   Read the spec and ticket, confirm its blockers are resolved in the
   current candidate, and state the ticket title. On resumption, check the
   existing claim, worktree, checks and delegations. Before claiming another
   ticket or starting final acceptance, use the previous ticket's handoff ID
   to check whether its continuation started and where to resume it.
2. **Implement and pin.** Use `implement` and, where possible, `tdd` at the
   agreed seams. Pin the baseline, execution checkout and recoverable
   candidate, including new files and extra check inputs, as described in
   [Fixed candidates and parallel acceptance](../testing.md#fixed-candidates-and-parallel-acceptance).
3. **Review.** Open a [named delegation](../implemented/agent-collaboration.md#invocation-and-identity)
   with `name="Vera"` and `purpose="ticket_review"` for each ticket, starting
   at design time when a decision needs early review. For final review, provide
   the ticket, baseline, fixed candidate and diff, available evidence, planned
   checks and open questions. Run the
   [applicable focused checks](../testing.md#fixed-candidates-and-parallel-acceptance)
   while Vera assesses the candidate against the acceptance criteria and
   repository standards. Record both outcomes against the candidate. If a
   verified review finding requires a change, follow
   [Retiring a candidate's checks](../testing.md#retiring-a-candidates-checks)
   and return the fixed replacement to the same `delegation_id` for verification.
   Follow-up inherits the purpose and returns a new `work_id` for the review round.
4. **Accept.** Keep the ticket `claimed` until its criteria are met, its checks
   pass and Vera's independent review is complete for the accepted version.
   In one ticket update, set `Status: resolved` and record the integrated
   revision or snapshot, applicable passing checks, and Vera's delegation ID,
   `work_id` and explicit conclusion on that candidate under `## Answer`.
   Read the verdict before accepting the ticket. A completed delegation round
   can also contain a request for corrections. If the user authorized further work, include the next
   eligible ticket within that scope or the agreed final acceptance and a
   stable handoff ID marked pending. Otherwise stop at this ticket.
   A user directive to stop after acceptance keeps this ticket's review in
   scope. Apply it with `mcp__ask_agent__limit_ticket_batch` and the exact
   human message. Record acceptance, call `mcp__ask_agent__finish_ticket_batch`,
   and report the outcome.
5. **Start the next phase.** Use the recorded ID with the
   [ticket handoff tool](../implemented/input-reliability.md#ticket-handoffs).
   Before handing off the last ticket, complete its formal review and focused
   checks on the fixed integrated candidate. Pass
   `pointers.next_phase="final_acceptance"` and point `next_work` to the batch
   acceptance record.
   If the installation lacks automatic clearing, leave the handoff pending.
   When available, the runtime starts the named
   phase automatically in a fresh model context in the same Octo chat. Apply
   human updates to the current ticket by default. Leave updates with unclear
   scope in that ticket and continue. For clear cross-ticket requirements, use
   `pointers.human_scope` with the exact original `message` and the exact
   clauses in `carry`, including their conditions. Use `batch` for requirements
   that apply to the batch and `next` for requirements limited to the next
   ticket. Further authorization must come from the user.
   Query the handoff by its original ID and locate that ID in the accepted ticket
   to record `started`. Work from the next ticket, its relevant spec and the
   accepted candidate. Open previous acceptance evidence for a dependency,
   check reuse, recovery or final acceptance. Reconcile an interruption or lost
   receipt by the same ID before advancing. An unknown result stays pending.
   Honor Stop and new human instructions before continuing the batch. If a
   confirmed inline steer withdraws the pending handoff, process the update,
   revisit acceptance and record a new handoff ID. Keep the original receipt.
   Manual compact waits in the current batch. Independent human requests
   supersede the automatic batch. To resume after closing, Stop or an
   unconfirmed handoff, reconcile any outstanding receipt and register under
   new human authorization.
6. **Deliver and close the batch.** From confirmed final acceptance, Octo
   checks whether the focused results and Vera's verdict cover the fixed
   integrated candidate. Run any missing focused checks. If existing review
   does not cover that candidate, open an ordinary named delegation to Vera
   in parallel, since formal reviews belong to an active ticket. Use the
   ordinary delegation's ID for any replacement review during final acceptance.
   When both tracks pass for the candidate, run the [full gate](../testing.md#full-checks-before-code-commits)
   once. When [release checks](../testing.md#release-and-documentation-checks)
   apply, a single `--release` run covers both gates. Reuse passing evidence
   when it still applies. Complete the applicable checks before the authorized
   commit, push and deployment. Settle pending human
   input and verify the delivery still matches the authorization. Adopt a
   separate input that supplements the same batch with
   `mcp__ask_agent__adopt_ticket_batch_update`, using its exact text before
   acting on it. Record the final checks and the outcome of authorized
   delivery, call `mcp__ask_agent__finish_ticket_batch`, then report the
   outcome after confirming the close receipt.

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

# Local tasks, specs and tickets

New temporary tasks use `<account-workspace>/<task>/`. Resolve the current
account's workspace from the session/account context; the local layout is in
[local-wsl.md](../local-wsl.md#临时任务目录). It is outside the project checkout.
This project has no external issue tracker. When a skill asks for tracker setup
or uses `.scratch/<feature>/` as its generic local example, use this task directory.

## Placement

Keep temporary analysis, specs, tickets, investigation scripts, experiments,
handoffs and task artifacts together in that task directory. An isolated Git
worktree, when needed, can live under its `worktree/` subdirectory. Ordinary
implementation edits stay in the selected repository checkout or worktree;
reusable product code and regression tests belong in that repository.

Test databases, synthetic accounts, provider output and other mutable runtime
state use the separate roots required by [testing.md](../testing.md). A source
worktree under the account workspace does not make its task directory a valid
isolated test-state root. Test launchers must still select independent state and
provider homes. `~/.cache/octopus-tests/` is a local testing location, not the
default for source worktrees or general task materials.

Existing `.scratch/` or cache-based tasks may finish at their current paths while
processes, review links or recovery still depend on them. New tasks use workspace;
moving an active task requires updating those consumers, not just moving files.
`docs/review/` remains available for requested temporary review reports; managed
application/research output and private publication resources keep their owners.

## Files and lifecycle

- One feature or investigation per `<account-workspace>/<task>/` directory.
- A spec, when needed: `spec.md`.
- Separate implementation tickets, when needed: `issues/01-<slug>.md`, numbered
  in dependency order. Record `Status`, `Blocked by`, intended behavior and
  acceptance criteria near the top. Reference the relevant source revision.
- “Publish to the tracker” means save these local files. “Fetch a ticket” means
  read its file. Record clarification and evidence in the same ticket.
- A map is optional for a large decision graph. Use `map.md`, link its child
  tickets, and keep each resolved answer with the decision that required it.
- Small, well-defined work can proceed directly from the conversation. Creating
  a spec, tickets or a map is not an acceptance requirement for every task.

The local status vocabulary is `needs-triage`, `needs-info`, `ready-for-agent`,
`ready-for-human`, `claimed`, `resolved` and `wontfix`. Use only the states the
selected workflow needs. `Blocked by` names the exact local ticket files;
resolved dependencies permit work, while `needs-info` records the missing input.
Status changes do not grant implementation, publication or production authority.

`.scratch/`, `docs/review/` and the account workspace are temporary. Before
cleanup, put lasting decisions, their reasons and applicability, and concise
regression evidence in the owning project document. Current contracts belong in
`docs/implemented/`; unfinished changes belong in `docs/plans/`; verification
policy belongs in `docs/testing.md`. Final durable links point to those owners.
Do not remove material still needed by running work, an active review or recovery.
Machine-specific publication tools and unfinished release evidence stay in their
managed operations location, as described in [local-wsl.md](../local-wsl.md).

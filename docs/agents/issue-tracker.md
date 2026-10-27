# Local specs and tickets

This project uses local Markdown under `.scratch/`, which Git ignores. It has
no external issue tracker. When a skill asks for tracker setup, use this
convention; no account, remote issue or setup interview is needed.

## Files and lifecycle

- One feature or investigation per `.scratch/<feature>/` directory.
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

`.scratch/` is temporary, not the sole home of a durable decision or recovery
record. Before cleanup, put lasting decisions and concise regression evidence
in the existing owning document under `docs/`; final delivery links point there.
Do not remove material still needed by running work, an active review or recovery.
Machine-specific publication tools and unfinished release evidence stay in their
managed operations location, as described in [local-wsl.md](../local-wsl.md).

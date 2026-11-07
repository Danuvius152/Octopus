# Domain Docs

This repository uses one root `GLOSSARY.md` for settled domain language.
Current decisions and their rationale live with the corresponding contracts
in `docs/implemented/`; unfinished formal designs live in `docs/plans/`.
Use this existing layout rather than creating a parallel `docs/adr/` tree.

## Before exploring

- Read the relevant terms in the root [GLOSSARY.md](../../GLOSSARY.md).
- Use [the documentation index](../README.md) to find the owning documents.
- Read the current contracts in `docs/implemented/` for the area being changed,
  including their rationale and retained obligations.
- Read the relevant `docs/plans/` document when the task concerns planned work.
  A plan or a confirmed interview choice is not evidence of implementation.

Read only task-relevant documents. Missing documents do not require creating
empty placeholders. Repository document maintenance and consolidation timing
follow [docs/AGENTS.md](../AGENTS.md).

## During a design discussion

Grill intermediate artifacts, including unsettled terms and decision notes,
belong in `.scratch/<task>/`. Derived specs and tickets stay with that task.
Follow [issue-tracker.md](issue-tracker.md) for task paths and lifecycle.

Use established glossary terms when naming a domain concept. Resolve and
record terminology changes in the scope authorized for the task; task-local
glossary drafts do not silently replace the root glossary.

## Record and revisit decisions

Keep a decision's rationale, conditions and exceptions with its owning
contract or formal plan. On completion, consolidate the verified current
contract in `docs/implemented/` and retire the superseded execution material
under the repository's documentation rules. Do not label an unimplemented
decision as implemented merely because the user confirmed it.

If a proposal conflicts with an existing decision, name the owning document,
state the conflict and explain why revisiting it is warranted. Keep the
existing contract until the change is authorized and actually implemented.

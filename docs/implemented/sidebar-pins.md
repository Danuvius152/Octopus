# Sidebar pins: visibility, ordering and attention

> **Status:** implemented — Pins are shortcuts independent of archive; selection updates sessions in place.

## 1. Two questions, two flags

An agent or application answers two questions independently:

| | in the sidebar | in use (delegation, schedules, opening the app) | on the page |
|---|---|---|---|
| pinned | yes | yes | yes |
| not pinned | no¹ | yes | yes |
| archived | no | no | yes, with Restore |

¹ except while it needs you — §5.

The model is positive, not negative. The sidebar is a set of **shortcuts**
you chose, like a dock; the page is the whole collection. There is no
"hidden" state to reason about — something you haven't pinned simply isn't a
shortcut. That's why the flag is `pinned` and not `hidden`: the default
(nothing chosen) and the action (choosing) read the same way round as the UI.

## 2. Defaults

* **A new agent or application is pinned**, at the bottom. You almost always
  use something right after making it; making it land off-screen would turn
  "create" into "create, then go find it".
* **Restoring is pinning.** Bringing something back from the archive is
  choosing to use it; if it was pinned when it was archived it returns to its
  old place, otherwise it goes to the bottom.
* **The Default Agent is always pinned.** It's where a session goes when
  nothing else is chosen; the API refuses to unpin it (400) and the UI shows
  no control for it.

## 3. Order

`pin_order` is a position among the pinned, 1-based, meaningful only while
`pinned = 1`. Unpinning leaves it alone; pinning always appends (`MAX + 1`,
computed inside the UPDATE), because something coming back to the sidebar is
news and belongs where the eye lands last. Pinning something already pinned
is a no-op — it keeps its place.

The sidebar is reordered by dragging (`@dnd-kit/sortable`: pointer with a
6px activation distance so a click still opens the row, touch with a 250ms
press so a swipe still scrolls the drawer, and the keyboard sensor for
everyone else). A drop sends the whole pinned order with
`PUT /api/{agents,applications}/pin-order {ids}`. The server:

* rejects an id that is unknown, archived, unpinned, or repeated (400);
* places pinned rows the list *doesn't* name after the named ones, in their
  existing order — a tab that last looked before another tab pinned something
  must not throw that pin away;
* returns every live row, so the client replaces its list in one step.

The client applies the new order optimistically and rolls back to the
server's list if the request fails.

Session details and events replace an existing session at its current list
position. Selecting a session refreshes its data without moving it to the
bottom; a new session is appended. This keeps the session rail still while
switching conversations and leaves agent pin order under the user's control.
`lib/sessionList.test.ts`, `lib/hydrateSession.test.ts` and
`e2e/agents.spec.ts` cover repeated selection and in-place updates.

## 4. Schema

The complete current schema declares `pinned` and `pin_order` directly.
Format preparation preserves saved positions; startup does not derive them again.

Both columns ride on `AgentRead` / `ApplicationRead`. The list endpoints keep
their existing order; the sidebar sorts by `pin_order` itself, because every
other consumer of the agent list (the application form's builder picker,
delegation targets) wants the stable one.

## 5. Something unpinned that needs you

Unpinned must not mean unreachable at the moment it matters. An unpinned row
appears in the sidebar — after the pinned ones, dimmed, not draggable, with a
pin button — while:

* **agent:** it owns the session you have open; any of its sessions (other
  than an application's own conversations, which never show in the rail) is
  running or waiting on approval; or one of them has a question pending;
* **application:** it's the one you have open; it's building; or a build of
  it failed while you weren't looking at it, until you open it.

It leaves again when the condition clears. "Failed while you weren't looking"
is the one that needs state: `unseenFailedApplications` in the store is added
to by the `application_updated` handler on a transition *into* `failed` for an
app that isn't open, and cleared by `openApplication`. It is not persisted — a
reload shows the failure on the Applications page, where the status is
printed on the row.

The rules live in `lib/sidebarPins.ts` as pure functions over store state, so
both sections and their tests read the same definition.

## 6. The pages

The Agents and Applications pages' tabs become **`All · Create`** (`Edit`
when an agent is open for editing); the old Archived tab is the bottom
section of All. The page is reached the way it always was — the section's
"+" — and All is one tab away. An "All agents (N)" row at the foot of each
sidebar section was tried and dropped: it was a second door to the page the
"+" already opens, and it read as another item in the list.

`ItemLibrary` renders both tabs' All:

* a filter box (name and description);
* **In sidebar** — pinned, in sidebar order; **Not in sidebar**; **Archived**,
  each with a count, and an empty section is omitted;
* per row: a pin toggle (absent for the Default Agent and for archived rows),
  and the row's own actions — agents: **Chat** (the latest session, or a new
  one), **Edit** and **Archive**; applications: **Open** and **Archive**;
  archived rows: **Restore**.

**Nothing in the UI hard-deletes.** Every removal is an archive — a soft
delete that keeps the row, the files and the history, and that Restore
undoes:

* the All tab archives agents and applications; there is no Delete button;
* a sidebar application row's button unpins — it removes the *shortcut*;
* a sidebar session row's button archives the session *without* a
  replacement (`POST /api/sessions/{id}/archive?replace=false`, 204). The
  plain route keeps its `/archive`-command meaning — put this conversation
  away and start a fresh one in its place — which is not what a row's button
  means. The archived session stays readable and restorable from the
  archived-sessions page.

The `DELETE` routes stay in the API, for scripts and test cleanup; the UI
just doesn't reach them.

## 7. Sync

Pins have to agree across tabs and devices — a phone and a laptop showing
different sidebars is the bug this would otherwise be.

* **Applications** already broadcast `application_updated` rows, so pin and
  unpin travel through the existing handler; a reorder broadcasts only the
  rows whose position changed. `application_archived` — sent by the archive
  route since archiving landed but never handled — now removes the row from
  the store too.
* **Agents** had no broadcast channel at all, so no agent edit ever synced
  across tabs. They get the same shape now, sent from the agents router:
  `agent_created` / `agent_updated` (edit, pin, unpin, restore, and each row
  a reorder moved) upsert the row; `agent_archived` / `agent_deleted` remove
  it. A request that fails broadcasts nothing.

§3's reorder rule is what keeps two tabs from destroying each other's pins in
the window before an event lands.

## Historical evidence

The [original design and evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/sidebar-pins.md) retain their original version and conditions. Current verification follows [testing.md](../testing.md); this document does not establish production deployment.

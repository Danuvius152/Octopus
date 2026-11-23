# 侧栏置顶：可见性、排序与提醒

> **Status:** implemented — 置顶独立于归档，选择和详情刷新在原位置更新会话。

<a id="1-two-questions-two-flags"></a>
## Two questions, two flags

An agent or application answers two questions independently:

| | in the sidebar | in use (delegation, schedules, opening the app) | on the page |
|---|---|---|---|
| pinned | yes | yes | yes |
| not pinned | no¹ | yes | yes |
| archived | no | no | yes, with Restore |

¹ except [while it needs you](#something-unpinned-that-needs-you).

The sidebar holds selected shortcuts; the All page holds the complete collection.
`pinned` controls shortcut membership independently of archive state.

<a id="2-defaults"></a>
## Defaults

* **A new agent or application is pinned**, at the bottom. You almost always
  use something right after making it; making it land off-screen would turn
  "create" into "create, then go find it".
* **Restoring is pinning.** Bringing something back from the archive is
  choosing to use it; if it was pinned when it was archived it returns to its
  old place, otherwise it goes to the bottom.
* **An existing protected Default Agent cannot be unpinned.** The API refuses
  that edit (400) and the UI shows no control. First-account creation and
  registration create it in the account transaction. Ordinary startup does
  not recreate one missing from an existing account; default lookup can fail
  rather than silently altering saved configuration.

<a id="3-order"></a>
## Order

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
* returns every live row as an observation for the client's domain merge. For
  applications, one write transaction validates and reorders the current
  account's live pins, then reads that account's complete live application list.

The client applies the new order optimistically. A successful Agent reorder
confirms only its submitted ID positions; the attached list does not authorize
whole-row replacement. Failure ends that operation's optimistic order and reads
the server under its original identity, preserving newer facts instead of
restoring an old whole list. Each domain's [list contract](frontend-list-reconciliation.md)
owns the merge and confirmation rules.

Session details and events replace an existing session at its current list
position. Selecting a session refreshes its data without moving it to the
bottom; a new session is appended. This keeps the session rail still while
switching conversations and leaves agent pin order under the user's control.
`lib/sessionList.test.ts`, `lib/hydrateSession.test.ts` and
`e2e/agents.spec.ts` cover repeated selection and in-place updates.

<a id="4-schema"></a>
## Schema

The complete current schema declares `pinned` and `pin_order` directly.
Format preparation preserves saved positions; startup does not derive them again.

Both columns ride on `AgentRead` / `ApplicationRead`. The list endpoints keep
their existing order; the sidebar sorts by `pin_order` itself, because every
other consumer of the agent list (the application form's builder picker,
delegation targets) wants the stable one.

<a id="5-something-unpinned-that-needs-you"></a>
## Something unpinned that needs you

An unpinned row appears after the pinned rows, with a dimmed name and no drag
handle, while:

* **agent:** it owns the active session; or a session with `origin !== "app"` has
  `status === "running"` or a nonempty `pendingQuestions` entry. Waiting on
  approval is not a separate selector condition: it keeps the agent visible
  only while one of those actual states applies;
* **application:** it's the one you have open; it's building; or a build of
  it failed while you weren't looking at it, until you open it.

Every running agent row shows the same violet count. The count includes
builder sessions (`origin === "application"`) and delegation sessions even
when those delegation rows are hidden. App conversations (`origin === "app"`)
are excluded. The collapsed rail shows the running dot.

In the expanded sidebar, hover over an agent row on desktop or unfold it on
touch to reveal its new-session button. When the count reaches zero, these
gestures also reveal Pin or Unpin for ordinary agents still in the sidebar.
The Agents → All pin control is available independently of the sidebar
running count.

It leaves again when the condition clears. "Failed while you weren't looking"
is the one that needs state: `unseenFailedApplications` in the store is added
to by the `application_updated` handler on a transition *into* `failed` for an
app that isn't open, and cleared by `openApplication`. It is not persisted — a
reload shows the failure on the Applications page, where the status is
printed on the row.

The rules live in [sidebarPins.ts](../../web/src/lib/sidebarPins.ts) as pure
functions over store state. [sidebarPins.test.ts](../../web/src/lib/sidebarPins.test.ts)
covers running/question/active-session presence and exclusion of application
conversations, plus open/building/unseen-failure application presence.

<a id="6-the-pages"></a>
## The pages

The Agents and Applications pages offer **All · Create** (**Edit** while editing
an agent). The sidebar section's **+** opens the page. Archived rows appear in
the bottom section of All.

`ItemLibrary` renders both tabs' All:

* a filter box (name and description);
* **In sidebar** — pinned, in sidebar order; **Not in sidebar**; **Archived**,
  each with a count, and an empty section is omitted;
* per row: a pin toggle (absent for the Default Agent and for archived rows),
  and the row's own actions — agents: **Chat** (the latest session, or a new
  one), **Edit** and **Archive**; applications: **Open** and **Archive**;
  archived rows: **Restore**.

**The Agent, Application and session library UI does not hard-delete.** Its
removals use archive — a soft
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

<a id="7-sync"></a>
## Sync

| Domain | Events and effect |
| --- | --- |
| Applications | `application_updated` merges edits and pin positions; reorder broadcasts changed rows. `application_archived` removes the live row. |
| Agents | `agent_created` / `agent_updated` publish complete rows into the live or archived partition. A complete `agent_archived` row moves into archived; an ID-only event uses the known row or confirms missing data. `agent_deleted` removes both partitions. |

Failed requests broadcast nothing.

Agent ordering receipts confirm only the submitted positions; their attached live
list remains a read observation. Success and failure both finish that operation's
optimistic order before confirmation or compensation continues. Reads preserve
confirmed or pending order fields while still admitting names and archive facts.
Pin failure and ordering confirmation include archives once All has been requested,
using the original operation identity. The [Agent list contract](frontend-list-reconciliation.md#agent-联合列表)
owns scope, overlapping requests and completeness; no component keeps its own
archived authority.

The reorder rule is what keeps two tabs from destroying each other's pins in
the window before an event lands.

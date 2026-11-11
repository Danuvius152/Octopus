# 控制台：视觉规范与工作区布局

> **Status:** implemented — 蓝色工作区、设计 token、两层侧栏和页面式管理入口。

## 2. The token layer is the design

[tokens.css](../../web/src/styles/tokens.css) owns the shared palette, typography
and semantic theme values. It is not an exhaustive source of every visual value:
components and [index.css](../../web/src/index.css) also specify geometry and
utility styles. The logo deliberately uses its own fixed brand navy (`--logo`),
not the interactive `--primary` color.

| Token | Value | Where it shows |
|---|---|---|
| `--primary-600` | `#2563b8` | filled buttons, the user bubble, selected rows, links |
| `--primary-50/100` | `#eaf1fb` / `#d6e3f7` | selected fills and accent borders |
| `--gray-50` | `#f7f9fc` | the sidebar ground |
| `--gray-300/400` | `#e4eaf4` / `#dde4ee` | hairlines and card borders |
| `--gray-700/900` | `#7f8ba0` / `#2b3138` | meta text and body text |
| `--success` / `--warn` | `#3f9d6b` / `#c8703f` | healthy vs needs-attention |
| `--running` | `#8a6db5` | a delegation in flight — neither healthy nor broken |

The shared sans and mono families are Hanken Grotesk and IBM Plex Mono.
Technical labels such as ids, counts, cron, costs and command names use the mono
style where their component applies it; the font token alone does not enforce
that choice for every technical string.

Shared primitives live in `index.css`: `.tile` (identity squares), `.pill`
(status chips in four tints), `.badge-running`, `.card`, `.page-body`.

## 3. Sidebar: workspace over system

```
Octopus                     ← brand (the logo itself is unchanged)
AGENTS                   +
  ▾ 🐙 Octo              +     ← fold · tile · name · new-session
      ● root      idle         ← sessions on a hairline rail
  ▸ 🔬 Vera    ● 1 running     ← violet badge when a session is mid-turn
APPLICATIONS             +
  🪟 Habit Tracker
───────────────────────
MANAGE
  ◷ Schedules   2 · next 09:00
  ⚡ Connectors  5 ●
  ⚙ Harness     1 error ●
───────────────────────
A  Octopus / token             ← account menu
```

The selected state lives on **sessions**, not agents: an agent row is a fold,
and the thing you're actually looking at is a session. The MANAGE summaries
are the app's ambient health readout — a lapsed credential or a broken
connector is visible from anywhere without opening anything.

## 4. Pages, not dialogs

`mainView` in the store is the router (`chat` | `application` |
`application-create` | `agent-form` | `schedules` | `connectors` | `harness` |
`monitor` | `account`). The authoritative union is the store's
[MainView](../../web/src/stores/sessionStore.ts) type.
`setActiveSessionId` resets it to `chat`, so every existing "open this
session" call site keeps working. ChatView stays mounted but hidden behind
other views — it owns the composer draft, scroll position and pending
attachments, and unmounting it on a trip to Schedules would throw all of that
away.

Every view opens with the same `PageHeader`: breadcrumb left, actions right.

## 5. Archived, not a market

The Agent and Application pages use **All · Create/Edit**, with an Archived
section inside All. Archive preserves the row and files; restore makes the
item available again. The earlier design's separate Archived tabs are history.
Pin, unpin, archive, restore and current row actions are owned by
[sidebar behavior](sidebar-pins.md#6-the-pages), rather than a second navigation
specification here. There is no market/catalog screen or invented install count.

## 6. What the design didn't say, and what we did

The design draws a clean sidebar with no hover affordances and a one-click
"+". Taken literally that would have deleted real capability, so:

- **Session create** keeps a name field (inline in the rail, not a dialog),
  with working dir / engine / credential folded behind a "+ working dir ·
  engine" toggle. Default look matches the design; the overrides survive.
- **Row actions** reveal on hover or the selected touch row: session archive
  and application unpin have distinct effects, as defined by
  [sidebar behavior](sidebar-pins.md#6-the-pages).
- **Archive** for an application sits in its own header, next to reload and
  open-in-tab, because that's where you are when you decide to shelve it.
- Assistant turns lost their name label; attribution moved onto the identity
  tile (`title`/`aria-label`), which is what the design shows.

## 7. Screens with no backend

`Agent Market`, `Application Market`, per-tool connector permissions, schedule
run history with cost, bridge toggles and the app "shell v3 / core" split are
in the design but have nothing behind them. They are not faked. `next_run_at`
was the one gap worth closing — it's real now, read from APScheduler's live
trigger state and surfaced in the sidebar summary and the Schedules header.

## 8. Folding the sidebar to an icon rail

The sidebar collapses to 56px of icons and back. The handle is a small round
button on the sidebar's edge, revealed on hover, with the chevron pointing the
way it will move the edge.

Three decisions worth writing down:

- **The trigger is the edge, not the header.** When you want the sidebar
  narrower, the edge is where your pointer already is; a header button is a
  fixed target you have to go and aim for. The hover strip is 14px and lives
  *inside* the sidebar's own width so it can never sit over the main pane and
  swallow a click meant for the chat; only the button overhangs the border,
  and while it's hidden it is `pointer-events: none` for exactly that reason.
- **The fold is CSS, driven by one class.** `--sidebar-w` on `.app-layout` is
  the single source of truth for the width, and `.sidebar.collapsed` hides the
  words — names, section labels, summaries, the session rail. Nothing threads
  a `collapsed` prop through four components, and no row has two renderings to
  keep in sync.
- **It persists; per-agent folds don't.** Collapsing the sidebar is a
  statement about how you want to work, so it survives a reload
  (`octopus_sidebar_collapsed`). Which agents are unfolded is navigation, and
  still resets to all-folded on every load (§3).

Status survives the fold, because a running agent is most of the reason to
glance at the rail at all: the `N running` badge becomes a dot pinned to the
agent's tile, and an application's build dot does the same. Rows keep working
as rows — a manage icon opens its page, an app icon opens the app. The one
row that can't is an agent, whose click normally toggles a session rail that
the rail has no room for; from the icon rail it means "show me this agent", so
it unfolds the sidebar and the agent together.

Mobile is exempt. Below 769px the sidebar is already a slide-over drawer, so a
collapse persisted from a laptop must not turn the phone's drawer into a
sliver — the whole collapsed block, and the handle, are desktop-only.

## 9. Testing

Unit: `SidebarApplications`, `SidebarManage`, `ApplicationFormPage`,
`AgentFormPage`, `SidebarEdgeToggle` (+ the existing chat/card suites). E2E:
representative checks use `.chat-header .crumb-current` for the session name,
`.btn-manage-*` to reach manage pages and the inline `.session-create` row.
Library checks use All/Create and its Archived section, rather than the retired
`.btn-tab-archived` selector.
The fold has its own pair in `app.spec.ts`, driving the real mouse: the handle
is invisible and unclickable until the edge is hovered, which is the guarantee
worth pinning down.

## Historical evidence

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/console-redesign.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.

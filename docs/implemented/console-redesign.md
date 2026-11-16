# 控制台：视觉规范与工作区布局

> **Status:** implemented — 蓝色工作区、设计 token、两层侧栏和页面式管理入口。

<a id="2-the-token-layer-is-the-design"></a>
## The token layer is the design

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

<a id="3-sidebar-workspace-over-system"></a>
## Sidebar regions

| Region | Content and behavior |
| --- | --- |
| Agent rows | Identity tile, expansion, running state and new-session entry |
| Session rail | Selected session and session actions; selection belongs to a session |
| Applications | Pinned applications and their build status |
| Manage | Schedules, Connectors, Harness and Monitor entry points with available summaries |
| Account | Current account menu |

<a id="4-pages-not-dialogs"></a>
## Pages, not dialogs

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

<a id="5-archived-not-a-market"></a>
## Library and creation actions

Agent and Application pages use All and Create/Edit, with archived items inside
All. Archive retains the row and files; restore makes the object available again.
Pin, archive and row actions follow [sidebar behavior](sidebar-pins.md).

<a id="6-what-the-design-didnt-say-and-what-we-did"></a>
Session creation keeps an inline name field and expandable working-directory,
backend and credential controls. Row actions appear on hover or the selected
touch row. Application archive is in its header beside reload/open. Assistant
attribution uses the identity tile's title and accessible label.

<a id="7-screens-with-no-backend"></a>
The UI does not provide Agent/Application markets, per-tool Connector permissions
or complete schedule cost history. `next_run_at` is read from live APScheduler
trigger state for the summary and Schedules header.

<a id="8-folding-the-sidebar-to-an-icon-rail"></a>
## Folding the sidebar to an icon rail

The sidebar collapses to 56px of icons and back. The handle is a small round
button on the sidebar's edge, revealed on hover, with the chevron pointing the
way it will move the edge.

| State or mechanism | Scope |
| --- | --- |
| `sidebarCollapsed`, `octopus_sidebar_collapsed` | Desktop fold persists across reload; collapsed width is 56px |
| Agent expansion | Navigation state; resets to folded on reload |
| `sidebarOpen` | Mobile drawer visibility, independent of desktop collapse |
| `--sidebar-w` and `.sidebar.collapsed` | Width and word visibility; one row rendering |
| Edge trigger | A 14px hover strip stays inside the sidebar; the hidden overhanging button has `pointer-events: none` |

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

<a id="9-testing"></a>
## Testing

Unit: `SidebarApplications`, `SidebarManage`, `ApplicationFormPage`,
`AgentFormPage`, `SidebarEdgeToggle` (+ the existing chat/card suites). E2E:
representative checks use `.chat-header .crumb-current` for the session name,
`.btn-manage-*` to reach manage pages and the inline `.session-create` row.
Library checks use All/Create and its Archived section.
The fold has its own pair in `app.spec.ts`, driving the real mouse: the handle
is invisible and unclickable until the edge is hovered, which is the guarantee
worth pinning down.

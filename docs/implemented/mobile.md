# 手机端 Octopus

> **Status:** implemented — 自动收起抽屉、16px 输入、触摸目标与安全区布局。

<a id="1-why-this-is-not-responsive-css"></a>
## Viewport, input and hardware conditions

| trigger | what it governs |
|---|---|
| `max-width: 768px` | **layout** — the sidebar becomes a drawer, headers shed context |
| `hover: none` | **input** — hit areas, and hover-only affordances, which follow the *selection* instead |
| `env(safe-area-inset-*)` | **hardware** — the notch and the home indicator |

<a id="2-the-drawer-has-to-put-itself-away"></a>
## Drawer navigation

`sessionStore.sidebarOpen` closes in the same update as navigation through
`setActiveSessionId`, `openApplication`, `openApplicationCreate`, `openAgentForm`,
`openManage` and `showChat`. Desktop layout ignores this flag.

The drawer width is `min(270px, 84vw)` and its hidden displacement derives from
that width. The remaining overlay provides dismissal. Desktop collapse and
mobile drawer visibility remain independent.

<a id="3-typing"></a>
## Typing

* **Fields are 16px on phones.** iOS zooms the page whenever a focused field
  is smaller, and never zooms back out. This is the one rule in the file that
  needs `!important`: the sizes come from utility classes on dozens of
  fields, and this is a platform behaviour to defeat, not a style to prefer.
* **Enter is a newline on touch, not send.** There is no Shift+Enter on a
  phone keyboard, so a return key that sends makes a second paragraph
  impossible to type. The send button is under your thumb anyway, and
  `enterkeyhint` matches whichever behaviour is live.
* The token field on the login screen turns off autocapitalize, autocorrect
  and spellcheck — it's a secret, not prose.

<a id="4-headers-shed-context-not-function"></a>
## Headers shed context, not function

A 390px header can't hold a breadcrumb chain, a status pill, a credential
chip, a connection pill and four icon buttons. What goes, in order:

1. **Leading crumbs** (`Applications / OctoFeature` → `OctoFeature`). You just
   tapped the thing; you have the context. Without this the *name* is what
   truncates — to a letter and a slash.
2. **Words that a dot already says.** The connection pill keeps its coloured
   dot and drops "Connected".
3. **Long labels with short equivalents.** "Built by Octo" becomes "Ready";
   "Building" stays "Building", because that one is why you're looking.
4. **The engine-credential chip**, below 640px only. It names a credential and
   opens a picker, neither of which fits; it's a desk-bound action and it
   reappears on anything wider.

Everything that *does* something is still on the header. Nothing moved into a
hidden menu.

<a id="5-touch-targets-and-hover-only-affordances"></a>
## Touch targets and hover-only affordances

Under `hover: none`, sidebar row actions appear on the selected session
or application and the expanded Agent (`.session-item.active`,
`.application-item.active`, `.agent-item.expanded`). Other rows keep them hidden.
An Agent's running count sits to the left of its actions. On a folded Agent
row, taps in the space reserved for its hidden actions unfold the row.

The current session action archives; Agent/App sidebar actions unpin. These
library actions do not hard-delete, while credential and schedule deletion
retain their separate semantics. The behavior owner is
[sidebar pins](sidebar-pins.md#the-pages).

Credential, schedule and notifier rows have no equivalent selection;
their delete buttons remain visible on touch. Browser verification must set
`isMobile`/`hasTouch` and assert `matchMedia("(hover: none)")`; narrowing the
viewport alone does not activate touch rules.

Hit areas grow past the button's box with a pseudo-element (`inset: -10px`)
rather than by resizing the button: the console's density is deliberate, and
an 18px icon with a 38px target is the combination that keeps both. Rows get
`:active` backgrounds since `:hover` never fires, and everything tappable gets
`touch-action: manipulation` so a tap isn't held for 300ms waiting to see
whether it was a double-tap zoom.
The adjacent Agent pin and new-session targets meet at the midpoint of their gap.
The inline session form uses 24px square Create and Cancel buttons so the name
field has room on narrow screens. Their hit areas extend into the surrounding
space and meet at the midpoint of the gap between them. On touch screens, the
Create target leaves a buffer next to the name field so taps at the field's
trailing edge can still focus it.

<a id="6-notch-home-indicator-status-bar"></a>
## Notch, home indicator, status bar

`viewport-fit=cover` lets the page use the whole screen, which also means it
extends under the hardware. `.app-layout` pays back the top and side insets;
the bottom inset is paid by the two surfaces that sit on that edge (the
composer and the sidebar's account bar) so they still extend under the home
indicator instead of leaving a dead strip above it.

The iOS status bar uses `default` to retain contrast against the light header.

These compose with the visual-viewport hook (`--app-h` / `--app-top`, which
tracks the keyboard): insets are hardware, the visual viewport is software,
and both are needed.

The hook does **not** simply size the app to the visual viewport, because that
is only trustworthy while something is being typed into. After the keyboard
animates away iOS can leave a stale, smaller height behind, and an app sized
from it stops short of the bottom of the screen — a band of dead page under
the composer. The rule is: nothing editable focused → the app is exactly
`innerHeight` tall, whatever the visual viewport claims; editing → follow the
visual viewport exactly. Focus and orientation changes re-measure a few times
over the next 650ms (the resize that matters is the one at the *end* of the
keyboard animation, which iOS doesn't reliably send), and a tap anywhere
re-measures as a last resort for a keyboard dismissed while the field kept
focus.

The bottom rule is `max(var(--bottom-bar-pad), safe-area-inset-bottom - 12px)`,
not the full inset added to the bar's own padding. It clears the indicator's swipe
zone without lifting the composer by the entire 34pt inset. The composer uses
1.25rem normal padding, reduced to 0.5rem at widths up to 768px; the larger of
that padding and the adjusted safe area wins. The exact rule lives in
[index.css](../../web/src/index.css).

<a id="7-long-strings"></a>
## Long strings

Long paths, IDs and URLs wrap inside page bodies and schedule prompts;
grid/flex children must not widen the viewport to their longest token.

<a id="8-testing"></a>
## Testing

E2E at phone width: the drawer closes when a session is picked, the closed
drawer is entirely off-screen, no surface scrolls horizontally, and every
field is at least 16px. Unit: the store's navigation actions close the drawer.
[useViewportHeight.test.ts](../../web/src/hooks/useViewportHeight.test.ts) covers
focused-field sizing and stale viewport recovery; simulated viewport events do
not establish every physical iOS keyboard/browser-toolbar transition.

<a id="9-what-this-defers"></a>
## Browser and operating limits

The current navigation uses the hamburger, drawer and dismissal overlay. There
is no custom edge-swipe navigation or offline/service-worker execution. Device
keyboard and browser-toolbar behavior requires actual-browser verification;
simulated viewport events cover only their explicit inputs.

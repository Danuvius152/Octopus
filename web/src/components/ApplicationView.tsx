import { useEffect, useMemo, useRef, useState } from "react";
import { useStore } from "zustand";
import {
  IconAlertTriangle,
  IconArchive,
  IconArrowUp,
  IconExternalLink,
  IconMessage,
  IconMessages,
  IconRefresh,
  IconWand,
} from "@tabler/icons-react";
import {
  applicationUrl,
  primeAppCookie,
} from "../api/applications";
import { selectSession } from "../lib/selectSession";
import { applications as applicationList } from "../lib/applicationList";
import { applicationDraft } from "../lib/applicationDrafts";
import { isImeComposing } from "../lib/composerKeys";
import { useSessionStore } from "../stores/sessionStore";
import { PageHeader } from "./PageHeader";
import { Button } from "./ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import { Popover } from "./ui/popover";
import { AppIcon } from "./AppIcon";
import { BackendPanel } from "./BackendPanel";

/** The main pane for one application (applications.md §7) — the browser-tab
 * view. Renders the app's own document in an iframe once it's `ready`, and
 * shows the build in progress before that.
 *
 * Everything else lives in the header (app-agent-access.md §7). Asking for a
 * change used to be a composer bar pinned under the page: ~60px of every app,
 * forever, for something you do rarely. It's now the **Iterate** popover,
 * which also carries the link into the build session, and the backend panel
 * and the app's own agent conversations are header menus for the same reason.
 * Below the header there is nothing but the app. */
export function ApplicationView({
  onToggleSidebar,
}: {
  onToggleSidebar: () => void;
}) {
  const account = useSessionStore(s => s.accountGeneration);
  const id = useSessionStore(s => s.activeApplicationId);
  return <ApplicationPane key={`${account}:${id}`} onToggleSidebar={onToggleSidebar} />;
}

function ApplicationPane({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const token = useSessionStore((s) => s.token);
  const applications = useSessionStore((s) => s.applications);
  const read = useSessionStore((s) => s.applicationReads.live);
  const activeApplicationId = useSessionStore((s) => s.activeApplicationId);
  const agents = useSessionStore((s) => s.agents);

  const sessions = useSessionStore((s) => s.sessions);

  const app = applications.find((a) => a.id === activeApplicationId) ?? null;
  const agent = agents.find((a) => a.id === app?.agent_id) ?? null;

  // The threads the running app has opened with an agent (app-agent-access.md
  // §3). They're hidden from the sidebar — an app that talks all day would
  // bury the user's own sessions — so this is where "what is my app saying to
  // my agent?" gets answered.
  const conversations = useMemo(
    () =>
      sessions
        .filter((s) => s.origin === "app" && s.app_id === app?.id)
        .sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [sessions, app?.id]
  );

  const draft = useMemo(() => applicationDraft(activeApplicationId ?? ""), [activeApplicationId]);
  const { text: request, saveError } = useStore(draft.text.store);
  const { busy, error } = useStore(draft.store);
  const [iterateOpen, setIterateOpen] = useState(false);
  const dialog = useRef<symbol | null>(null);
  useEffect(() => () => { dialog.current = null; }, []);
  const closeIterate = () => { dialog.current = null; setIterateOpen(false); };
  // Manual reload and a fresh build timestamp both change the iframe URL.
  const [nonce, setNonce] = useState(0);
  const lastBuiltAt = app?.last_built_at ?? null;

  // The iframe and everything it loads authenticate with this cookie — an
  // iframe can't carry an Authorization header (applications.md §3). This has
  // to happen BEFORE the frame element reaches the DOM, because the browser
  // starts its request the moment it does — earlier than any effect would run.
  // A memo is the one hook that runs during render; the write is idempotent,
  // so a render React throws away costs nothing.
  useMemo(() => {
    if (token) primeAppCookie(token);
  }, [token]);

  const src = useMemo(
    () => (app ? applicationUrl(app.id, `${lastBuiltAt ?? ""}:${nonce}`) : ""),
    [app, lastBuiltAt, nonce]
  );

  if (!app) {
    return (
      <div className="application-view flex-1 flex items-center justify-center text-muted-foreground">
        <p className="text-sm">This application is no longer available.</p>
      </div>
    );
  }

  const archive = async () => {
    if (
      !window.confirm(
        `Archive "${app.name}"? It leaves the sidebar but keeps its files — ` +
          `restore it any time from All applications.`
      )
    )
      return;
    await draft.archive();
  };

  const openBuildSession = () => {
    if (app.session_id) selectSession(app.session_id, app.agent_id);
  };

  const askForChanges = async () => {
    const originalDialog = dialog.current;
    if (await draft.build() && originalDialog && dialog.current === originalDialog) closeIterate();
  };

  const statusBadge = (
    <span
      className={`app-status-badge app-status-${app.status} pill ${
        app.status === "building"
          ? "pill-neutral text-primary"
          : app.status === "failed"
          ? "pill-warn"
          : "pill-success"
      }`}
    >
      <span
        className={`dot ${app.status === "building" ? "animate-pulse" : ""}`}
      />
      {/* Two labels, one shown at a time (mobile.md §4). A phone header has
        * room for the app's name OR "Built by Octo", and the name is what
        * you came for — but "Building" still has to be legible, so the short
        * form says the state rather than dropping it. */}
      <span className="app-status-label">
        {app.status === "building"
          ? "Building"
          : app.status === "failed"
          ? "Build failed"
          : `Built by ${agent?.name ?? "an agent"}`}
      </span>
      <span className="app-status-label-short">
        {app.status === "building"
          ? "Building"
          : app.status === "failed"
          ? "Failed"
          : "Ready"}
      </span>
    </span>
  );

  return (
    <div className="application-view flex-1 flex flex-col min-h-0">
      <PageHeader
        onToggleSidebar={onToggleSidebar}
        icon={
          <AppIcon app={app} className="shrink-0" />
        }
        crumbs={["Applications", <span key="name" className="application-title">{app.name}</span>]}
        meta={statusBadge}
        actions={
          <>
            <button
              className="btn-application-reload inline-flex size-8 items-center justify-center rounded-lg text-gray-700 transition-colors hover:bg-gray-100 hover:text-gray-950"
              onClick={() => setNonce((n) => n + 1)}
              title="Reload the app"
              aria-label="Reload the app"
            >
              <IconRefresh size={16} />
            </button>
            <a
              className="btn-application-open-tab inline-flex size-8 items-center justify-center rounded-lg text-gray-700 transition-colors hover:bg-gray-100 hover:text-gray-950"
              href={applicationUrl(app.id)}
              target="_blank"
              rel="noreferrer"
              title="Open in a new tab"
              aria-label="Open in a new tab"
            >
              <IconExternalLink size={16} />
            </a>
            <button
              className="btn-application-archive inline-flex size-8 items-center justify-center rounded-lg text-gray-700 transition-colors hover:bg-warn-bg hover:text-warn-foreground"
              onClick={archive}
              title="Archive this application"
              aria-label="Archive this application"
              disabled={busy}
            >
              <IconArchive size={16} />
            </button>
            {app.backend && <BackendPanel backend={app.backend} />}

            {conversations.length > 0 && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    className="btn-application-chats inline-flex h-8 items-center gap-1.5 rounded-lg px-2 text-gray-700 transition-colors hover:bg-gray-100 hover:text-gray-950"
                    title="Conversations this app has had with an agent"
                  >
                    <IconMessages size={16} />
                    <span className="font-mono text-[11px]">
                      {conversations.length}
                    </span>
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-64">
                  <DropdownMenuLabel>App ↔ agent conversations</DropdownMenuLabel>
                  {conversations.slice(0, 12).map((c) => (
                    <DropdownMenuItem
                      key={c.id}
                      className="application-chat-item"
                      onClick={() => selectSession(c.id, c.agent_id ?? null)}
                    >
                      <span className="truncate">{c.name}</span>
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            )}

            <Popover
              open={iterateOpen}
              onClose={closeIterate}
              label={`Ask ${agent?.name ?? "the agent"} for a change`}
              panelClassName="w-[320px]"
              trigger={
                <Button
                  variant="outline"
                  size="sm"
                  className="btn-application-iterate"
                  onClick={() => {
                    dialog.current = iterateOpen ? null : Symbol();
                    setIterateOpen(!iterateOpen);
                  }}
                  aria-expanded={iterateOpen}
                  title={`Ask ${agent?.name ?? "the agent"} for a change`}
                >
                  <IconWand size={15} />
                  <span className="hidden sm:inline">Iterate</span>
                </Button>
              }
            >
              <textarea
                className="application-request-input w-full min-h-[72px] max-h-40 resize-none rounded-xl border-[0.7px] border-gray-400 bg-card px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground outline-none transition-colors focus:border-primary/70 focus:ring-[3px] focus:ring-primary/10"
                value={request}
                onChange={(e) => draft.text.edit(e.target.value)}
                onKeyDown={(e) => {
                  if (isImeComposing(e.nativeEvent)) return;
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    askForChanges();
                  }
                }}
                autoFocus
                rows={3}
                placeholder={`Ask ${agent?.name || "the agent"} for a change — "add a dark mode toggle"`}
              />
              <div className="mt-2 flex items-center justify-between gap-2">
                {app.session_id ? (
                  <button
                    className="btn-application-open-session inline-flex items-center gap-1.5 rounded-lg px-1.5 py-1 text-[12.5px] text-gray-700 transition-colors hover:text-primary"
                    onClick={openBuildSession}
                    title={`Open the build session${agent ? ` with ${agent.name}` : ""}`}
                  >
                    <IconMessage size={14} />
                    Build session
                  </button>
                ) : (
                  <span />
                )}
                <Button
                  className="btn-application-request"
                  size="sm"
                  onClick={askForChanges}
                  disabled={busy || !request.trim()}
                  title="Send the change request"
                  aria-label="Send the change request"
                >
                  <IconArrowUp size={15} />
                  Send
                </Button>
              </div>
            </Popover>
          </>
        }
      />

      {(error || read.error) && <div role="alert" className="application-compose-error px-3 py-2 text-xs text-destructive">
        {error || `List not synchronized: ${read.error}`}
        {read.error && <button onClick={() => void applicationList.refresh()} className="ml-2 underline">Refresh applications</button>}
      </div>}
      {saveError && <div role="alert" className="px-3 py-2 text-xs text-destructive">{saveError}</div>}

      <div className="application-frame-wrap flex-1 min-h-0 bg-background relative">
        {app.deletion ? (
          <div className="p-6 space-y-3" role="alert">
            <p>Application deletion is unfinished.</p>
            <p className="text-sm text-muted-foreground">{app.deletion.error || "Stopping work and cleaning up files…"}</p>
            <Button disabled={busy} onClick={() => void draft.remove(app.deletion!.keep_files)}>Retry deletion</Button>
          </div>
        ) : app.status === "ready" ? (
          <iframe
            key={src}
            className="application-frame w-full h-full border-0 bg-white"
            src={src}
            title={app.name}
            // `allow-same-origin` is deliberate: without it the app gets an
            // opaque origin and localStorage throws, which breaks most small
            // web apps. `allow-top-navigation` is withheld, so a buggy app
            // can't navigate the Octopus tab away (applications.md §3).
            sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-downloads"
          />
        ) : app.status === "building" ? (
          <div className="application-building absolute inset-0 flex flex-col items-center justify-center gap-3 text-center px-6">
            <span className="inline-block size-6 rounded-full border-2 border-primary/30 border-t-primary animate-spin" />
            <p className="text-sm font-medium text-foreground">
              {agent ? `${agent.name} is building ${app.name}…` : "Building…"}
            </p>
            <p className="text-xs text-muted-foreground max-w-sm leading-relaxed">
              It'll appear here the moment the entry page lands. You can watch
              the work in the build session.
            </p>
            {app.session_id && (
              <Button
                variant="outline"
                size="sm"
                className="btn-application-watch"
                onClick={openBuildSession}
              >
                <IconMessage size={15} />
                Watch the build
              </Button>
            )}
          </div>
        ) : (
          <div className="application-failed absolute inset-0 flex flex-col items-center justify-center gap-3 text-center px-6">
            <IconAlertTriangle size={22} className="text-destructive" />
            <p className="text-sm font-medium text-foreground">
              This build didn't produce a page yet
            </p>
            <p className="application-error text-xs text-muted-foreground max-w-md leading-relaxed">
              {app.error || `${app.entrypoint} is missing.`}
            </p>
            <p className="text-xs text-muted-foreground">
              Ask for a fix from <strong className="font-semibold">Iterate</strong>{" "}
              up top — it runs in the same session.
            </p>
          </div>
        )}
      </div>

    </div>
  );
}

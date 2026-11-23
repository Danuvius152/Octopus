import { accountFetch as fetch } from "../lib/accountFetch";
import { useEffect, useRef, useState } from "react";
import {
  IconArchive,
  IconCheck,
  IconLoader2,
  IconPin,
  IconPinnedOff,
  IconPlus,
  IconSubtask,
  IconX,
} from "@tabler/icons-react";
import { reorderAgentPins, setAgentPinned } from "../api/agents";
import { backendLabel, useBackendCatalog } from "../hooks/useBackendCatalog";
import { connectorInstallations } from "../lib/connectorInstallations";
import { selectSession } from "../lib/selectSession";
import { ArchiveDeadline, postArchive } from "../lib/sessionArchive";
import { sidebarAgents } from "../lib/sidebarPins";
import { knownAgentScope, refreshAgents, refreshKnownAgents, withAgentRequest } from "../lib/refreshAgents";
import { sessions as sessionList } from "../lib/sessionList";
import { isImeComposing } from "../lib/composerKeys";
import { useSessionStore, type Agent, type SessionInfo } from "../stores/sessionStore";
import { BackendCatalogStatus } from "./BackendCatalogStatus";
import { SidebarSectionHeader } from "./SidebarSectionHeader";
import { SortablePin, SortablePins, type DragHandle } from "./SortablePins";

const API = window.location.origin;

type SidebarState = ReturnType<typeof useSessionStore.getState>;

function watchForeground(start: SidebarState, leave: () => void, visible?: (state: SidebarState, before: SidebarState) => boolean) {
  return useSessionStore.subscribe((state, before) => {
    if (state.accountGeneration !== start.accountGeneration || state.token !== start.token ||
        state.mainView !== start.mainView ||
        (start.mainView === "chat" && state.activeSessionId !== start.activeSessionId) ||
        (start.mainView === "application" && state.activeApplicationId !== start.activeApplicationId) ||
        ((start.mainView === "agent-form" || start.mainView === "application-create") && state.pageTab !== start.pageTab) ||
        (start.mainView === "agent-form" && state.agentFormGeneration !== start.agentFormGeneration) ||
        visible?.(state, before) === false) leave();
  });
}

/** The AGENTS section of the sidebar — the workspace's top half.
 *
 * Two levels, per the console design: an agent row (fold caret, avatar tile,
 * name, a new-session "+", and a running badge when any of its sessions is
 * mid-turn) with its sessions nested underneath on a hairline rail. Sessions
 * are what you actually click all day, so they get the selected treatment: a
 * tinted pill with an accent border and the session's status set in mono on
 * the right.
 *
 * Only *pinned* agents are listed, in the order the user dragged them into
 * (sidebar-pins.md): the sidebar is shortcuts, the Agents page — the "+",
 * then its All tab — is everything. An unpinned agent joins the
 * list, dimmed, only while it needs you (its session is open, or running, or
 * asking something).
 *
 * The sidebar starts reads for agents, sessions and connector installations.
 */
export function SidebarAgents() {
  const token = useSessionStore((s) => s.token);
  const agents = useSessionStore((s) => s.agents);
  const sessions = useSessionStore((s) => s.sessions);
  const sessionRead = useSessionStore((s) => s.sessionRead);
  const activeAgentId = useSessionStore((s) => s.activeAgentId);
  const setActiveAgentId = useSessionStore((s) => s.setActiveAgentId);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const mainView = useSessionStore((s) => s.mainView);
  const openAgentForm = useSessionStore((s) => s.openAgentForm);
  const accountGeneration = useSessionStore((s) => s.accountGeneration);
  const showDelegations = useSessionStore((s) => s.showDelegations);
  const pendingQuestions = useSessionStore((s) => s.pendingQuestions);
  const agentRowPointerType = useRef("");

  // Which agents are unfolded. Multiple may be open; folding keeps the
  // sidebar from filling with sessions when there are many agents.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // Which agent has the inline "new session" row open, and what's typed into
  // it. The row lives in the session rail rather than in a dialog, so the
  // sidebar stays as quiet as the design draws it — but the overrides the old
  // create form carried (working dir, engine, credential) are still here,
  // folded behind "more" so they cost nothing until you want them.
  const [formAgentId, setFormAgentId] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [workingDir, setWorkingDir] = useState("");
  const [formBackend, setFormBackend] = useState("");
  const [formCredentialId, setFormCredentialId] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [archiveError, setArchiveError] = useState("");
  const [archivesInFlight, setArchivesInFlight] = useState<ReadonlySet<string>>(() => new Set());
  const [archiveConfirmation, setArchiveConfirmation] = useState<{ run: () => Promise<void>; busy: boolean } | null>(null);
  const archiveObservers = useRef(new Map<string, { stop: () => void; show: () => void }>());
  const latestArchive = useRef<(() => void) | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");
  const [createdId, setCreatedId] = useState<string | null>(null);
  const creation = useRef<(() => void) | null>(null);
  const credentials = useSessionStore((s) => s.credentials);
  const backendRead = useBackendCatalog();
  const discoveredBackends = backendRead.status === "ready" ? backendRead.catalog.available : [];

  const headers = { Authorization: `Bearer ${token}` };

  useEffect(() => {
    for (const { stop } of archiveObservers.current.values()) stop();
    latestArchive.current = null;
    setArchivesInFlight(new Set());
    setArchiveError(""); setArchiveConfirmation(null);
    creation.current?.();
    setCreating(false);
    setCreateError("");
    setCreatedId(null);
    setFormAgentId(null);
  }, [token, accountGeneration]);
  useEffect(() => {
    const observers = archiveObservers.current;
    return () => {
      creation.current?.();
      for (const { stop } of observers.values()) stop();
    };
  }, []);

  useEffect(() => {
    if (!token) return;
    void refreshKnownAgents().catch(() => {});
    void sessionList.refresh();
  }, [token, accountGeneration]);

  useEffect(() => {
    if (token) void connectorInstallations.refresh();
  }, [token, accountGeneration]);

  // Keep a valid agent selected — but do NOT unfold it. Every agent starts
  // folded, on every load: the sidebar's job at rest is to show what agents
  // exist, not to spill one agent's sessions just because it happens to sort
  // first. Unfolding is always something the user did (clicking the row, or
  // the "+" that opens the create row), never something the app decided.
  // `expanded` is deliberately not persisted, so a reload returns to folded.
  useEffect(() => {
    if (!agents.length) return;
    if (activeAgentId && agents.some((a) => a.id === activeAgentId)) return;
    const def = agents.find((a) => a.is_system) ?? agents[0];
    setActiveAgentId(def.id);
  }, [agents, activeAgentId, setActiveAgentId]);

  // The one unfold the app does on its own: an *unpinned* agent that just
  // became the owner of the open session (Chat on the Agents page). It is
  // only in the sidebar because of that session, so showing it folded would
  // hide the very row that put it there. Pinned agents keep the rule above.
  const activeSessionAgentId = sessions.find(
    (s) => s.id === activeSessionId
  )?.agent_id;
  const activeSessionAgentUnpinned =
    agents.find((a) => a.id === activeSessionAgentId)?.pinned === false;
  useEffect(() => {
    if (!activeSessionAgentId || !activeSessionAgentUnpinned) return;
    setExpanded((prev) =>
      prev.has(activeSessionAgentId)
        ? prev
        : new Set(prev).add(activeSessionAgentId)
    );
  }, [activeSessionId, activeSessionAgentId, activeSessionAgentUnpinned]);

  const toggleExpand = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  // Clicking an agent row. From the icon rail there is no rail to fold into,
  // so the click can only mean "show me this agent": unfold the sidebar and
  // the agent together, rather than toggling a fold nobody can see.
  const openAgent = (id: string) => {
    setActiveAgentId(id);
    const store = useSessionStore.getState();
    if (store.sidebarCollapsed) {
      store.setSidebarCollapsed(false);
      setExpanded((prev) => new Set(prev).add(id));
      return;
    }
    if (formAgentId === id && expanded.has(id)) closeCreateRow();
    toggleExpand(id);
  };

  const openCreateRow = (agentId: string) => {
    closeCreateRow();
    setActiveAgentId(agentId);
    setExpanded((prev) => new Set(prev).add(agentId));
    setFormAgentId(agentId);
    setNewName("");
    setWorkingDir("");
    setFormBackend("");
    setFormCredentialId("");
    setShowAdvanced(false);
  };

  const closeCreateRow = () => {
    creation.current?.();
    setCreating(false);
    setCreateError("");
    setCreatedId(null);
    setFormAgentId(null);
    setShowAdvanced(false);
  };

  const createSession = async (agentId: string) => {
    if (creation.current || createdId || formAgentId !== agentId) return;
    const start = useSessionStore.getState();
    if (formBackend && (start.backendCatalog.status !== "ready" || !start.backendCatalog.catalog.available.includes(formBackend))) {
      setCreateError("Choose a detected engine or use the agent default.");
      return;
    }
    // Stay in step with the sidebar drawer breakpoint in index.css.
    const drawer = window.matchMedia?.("(max-width: 768px)");
    const sidebarVisible = (state: SidebarState, isDrawer = drawer?.matches) =>
      isDrawer ? state.sidebarOpen : !state.sidebarCollapsed;
    const onResize = ({ matches }: MediaQueryListEvent) => {
      if (creation.current === stop && !sidebarVisible(useSessionStore.getState(), matches)) closeCreateRow();
    };
    const unwatch = watchForeground(start, closeCreateRow, (state, before) => {
      if (!sidebarVisible(state)) return false;
      if (state.agents === before.agents && state.sessions === before.sessions && state.pendingQuestions === before.pendingQuestions) return true;
      const agent = state.agents.find((row) => row.id === agentId);
      return !!agent && (agent.pinned !== false || sidebarAgents([agent], state).present.length > 0);
    });
    const stop = () => {
      unwatch();
      drawer?.removeEventListener("change", onResize);
      creation.current = null;
    };
    const ownsForm = () => {
      if (creation.current !== stop) return false;
      if (sidebarVisible(useSessionStore.getState())) return true;
      closeCreateRow();
      return false;
    };
    creation.current = stop;
    drawer?.addEventListener("change", onResize);
    setCreating(true);
    setCreateError("");
    const observation = sessionList.capture(start);
    const post = new ArchiveDeadline();
    try {
      const reply = await post.wait((async () => {
        const res = await fetch(`${API}/api/agents/${agentId}/sessions`, {
          method: "POST",
          signal: post.signal,
          headers: { ...headers, "Content-Type": "application/json" },
          body: JSON.stringify({
            name: newName.trim() || null,
            working_dir: workingDir.trim() || null,
            credential_id: formCredentialId || null,
            backend: formBackend || null,
          }),
        });
        if (res.status === 201) {
          const session: SessionInfo = await res.json();
          return typeof session?.id === "string" && session.id ? { session } : null;
        }
        if (res.status < 400 || res.status >= 500 || res.status === 408) return null;
        const body = await res.json().catch(() => null);
        return { error: typeof body?.detail === "string" ? body.detail : `Could not create session (HTTP ${res.status}).` };
      })()).catch(() => null);
      post.finish();
      if (!sessionList.accountCurrent(observation)) return;
      if (!reply) {
        if (ownsForm()) {
          closeCreateRow();
          setCreateError("Creation could not be confirmed. Check the session list before creating another conversation.");
        }
        void sessionList.refresh();
        return;
      }
      if ("error" in reply) {
        if (ownsForm()) setCreateError(reply.error);
        return;
      }
      const { session } = reply;
      const confirmation = new ArchiveDeadline();
      let accepted: boolean;
      try {
        accepted = await sessionList.created(session, observation, { deadline: confirmation });
        accepted &&= confirmation.active && sessionList.current(observation) &&
          useSessionStore.getState().sessions.some((row) => row.id === session.id);
      } finally { confirmation.finish(); }
      if (!ownsForm()) return;
      if (accepted) {
        closeCreateRow();
        void selectSession(session.id, agentId, { fresh: true });
      } else setCreatedId(session.id);
    } finally {
      if (ownsForm()) {
        stop();
        setCreating(false);
      }
    }
  };

  // A drop and its receipt/compensation retain one operation identity.
  const reorder = async (orderedIds: string[]) => {
    await withAgentRequest(async (request) => {
      const store = useSessionStore.getState();
      store.applyAgentOrder(orderedIds, request);
      try {
        const observed = (await reorderAgentPins(token, orderedIds)) as Agent[];
        store.confirmAgentOrder(orderedIds, request);
        store.applyAgentSnapshot(observed, request, "live");
      } catch {
        store.finishAgentOrder(request);
        await refreshAgents(request, knownAgentScope()).catch(() => {});
        return;
      }
      if (knownAgentScope() === "all") await refreshAgents(request, "all").catch(() => {});
    }, knownAgentScope());
  };

  const setPinned = async (agent: Agent, pinned: boolean) => {
    await withAgentRequest(async (request) => {
      try {
        useSessionStore.getState().upsertAgent((await setAgentPinned(token, agent.id, pinned)) as Agent, request);
      } catch {
        await refreshAgents(request, knownAgentScope()).catch(() => {});
      }
    }, knownAgentScope());
  };

  // Archive, not delete: the conversation leaves the rail with nothing in
  // its place, and its history stays — the archived-sessions page restores it
  // (sidebar-pins.md §6). Nothing in the sidebar destroys anything.
  const archiveSession = async (id: string) => {
    const existing = archiveObservers.current.get(id);
    if (existing) { existing.show(); return; }
    const start = useSessionStore.getState();
    const tracked = sessionList.observe(id, start, "archive");
    const { observation } = tracked;
    let foreground = start.activeSessionId === id && start.mainView === "chat";
    const unwatch = foreground ? watchForeground(start, retireForeground) : undefined;
    function retireForeground() {
      foreground = false;
      unwatch?.();
    }
    const post = new ArchiveDeadline();
    let confirmation: ArchiveDeadline | undefined;
    let retired = false, retain = false, checking = false, archived = false;
    let message = "";
    const stop = () => {
      retired = true;
      retireForeground();
      if (!sessionList.accountCurrent(observation)) { post.finish(); confirmation?.finish(); }
      tracked.finish();
      if (archiveObservers.current.get(id)?.stop === stop) archiveObservers.current.delete(id);
      if (latestArchive.current === stop) latestArchive.current = null;
    };
    latestArchive.current = stop;
    const owns = () => !retired && sessionList.accountCurrent(observation);
    const current = () => owns() && latestArchive.current === stop;
    const feedback = (next: string, readAgain = false) => {
      message = next;
      retain = readAgain;
      if (!current()) return;
      setArchiveError(message);
      setArchiveConfirmation(readAgain ? { run: () => confirm(true), busy: checking } : null);
    };
    const finishArchived = (manual: boolean) => {
      if (!owns()) return;
      const store = useSessionStore.getState();
      if (store.sessions.some((row) => row.id === id)) return;
      if (!manual && foreground && sessionList.archiveObservationCurrent(id, observation)) {
        retireForeground();
        store.setActiveSessionId(null);
      }
      feedback("");
    };
    const confirm = async (manual: boolean) => {
      if (checking || !sessionList.accountCurrent(observation) || manual && retired) return;
      if (manual) confirmation?.finish();
      confirmation = manual || !confirmation ? new ArchiveDeadline() : confirmation;
      checking = true;
      if (manual) feedback(message, true);
      const reading = manual ? sessionList.observe(id, useSessionStore.getState(), "archive") : tracked;
      try {
        const result = await sessionList.confirmArchive(id, reading.observation, confirmation);
        if (!owns()) return;
        if (result?.archived) { archived = true; finishArchived(manual); }
        else feedback(archived ? "Archive committed. The session list could not be confirmed." :
          "Archive result is still unknown. Confirm the original archive to check again; the command will not be sent again.", true);
      } finally {
        if (manual) reading.finish();
        checking = false; confirmation.finish();
        if (current()) setArchiveConfirmation((value) => value ? { ...value, busy: false } : null);
        if (manual && (!retain || !owns())) stop();
      }
    };
    archiveObservers.current.set(id, { stop, show: () => {
      if (!owns()) return;
      latestArchive.current = stop;
      feedback(message, retain);
    } });
    setArchivesInFlight((pending) => new Set(pending).add(id));
    feedback("");
    try {
      const reply = await postArchive(id, start.token, false, post);
      if (!sessionList.accountCurrent(observation)) return;
      if (reply.state === "refused") {
        feedback(reply.reason || `Could not archive session (HTTP ${reply.status}).`);
        return;
      }
      if (reply.state === "unknown") { await confirm(false); return; }
      archived = true;
      if (reply.result) {
        sessionList.archive(id, observation, reply.result.new_session_id);
        feedback(`Archive committed. ${reply.result.failure_stage.replaceAll("_", " ")} failed: ${reply.result.message}`);
        return;
      }
      if (sessionList.archive(id, observation, null)) finishArchived(false);
      else await confirm(false);
    } finally {
      retireForeground();
      if (owns()) setArchivesInFlight((pending) => {
        const next = new Set(pending);
        next.delete(id);
        return next;
      });
      if (!retain || !owns()) stop();
    }
  };

  const { pinned, present } = sidebarAgents(agents, {
    sessions,
    activeSessionId,
    pendingQuestions,
  });
  const creationLocked = creating || createdId !== null;

  // One agent with its session rail. `handle` is the drag handle for a
  // pinned row, and nothing for an unpinned one — those aren't in the order.
  const renderAgent = (a: Agent, handle: DragHandle) => {
    const isExpanded = expanded.has(a.id);
    const isSessionForm = formAgentId === a.id;
    const effectiveEngine = formBackend || a.backend;
    const inheritedCredentialConflict = !!a.credential_id && effectiveEngine !== a.backend;
    const selectedCredential = isSessionForm && formCredentialId
      ? credentials.find((credential) => credential.id === formCredentialId) : undefined;
    const invalidCredential = !!formCredentialId && selectedCredential?.backend !== effectiveEngine;
    const needsCredential = formCredentialId ? invalidCredential : inheritedCredentialConflict;
    const credentialOptions = isSessionForm && showAdvanced
      ? credentials.filter((credential) => credential.backend === effectiveEngine) : [];
    // Conversations an application is holding with this agent are its
    // business, not the rail's (app-agent-access.md §7): they're
    // reachable from the app's own header, and an app that chats ten
    // times an hour would otherwise bury the user's sessions — and
    // make the agent look permanently busy for work nobody started.
    const agentSessions = sessions.filter(
      (s) => s.agent_id === a.id && s.origin !== "app"
    );
    const visible = showDelegations
      ? agentSessions
      : agentSessions.filter((s) => s.origin !== "delegation");
    const hiddenDelegations = agentSessions.length - visible.length;
    const runningCount = agentSessions.filter(
      (s) => s.status === "running"
    ).length;

    return (
      <div className="agent-group">
        <div
          {...handle}
          className={`agent-item group flex items-center gap-2 rounded-lg px-2 py-1.5 cursor-pointer hover:bg-gray-100 transition-colors${
            isExpanded ? " expanded" : ""
          }${a.pinned === false ? " unpinned" : ""}`}
          onPointerDownCapture={(event) => { agentRowPointerType.current = event.pointerType; }}
          onClick={() => openAgent(a.id)}
          onDoubleClick={(event) => {
            // A touch double-tap can span the collapsed row and its newly visible + button.
            if (agentRowPointerType.current === "touch" || (event.target instanceof Element && event.target.closest("button"))) return;
            openAgentForm(a.id);
          }}
          /* The name leads even when there's a description: folded to
             the icon rail, the tooltip is the only thing naming the
             row. */
          title={a.description ? `${a.name} — ${a.description}` : a.name}
        >
          <span
            className={`agent-fold shrink-0 text-[10px] leading-none text-gray-600 transition-transform ${
              isExpanded ? "rotate-90" : ""
            }`}
            aria-hidden
          >
            ▸
          </span>
          <span className="agent-avatar tile tile-plain shrink-0">
            {a.avatar || "🐙"}
          </span>
          <span className="agent-name flex-1 truncate text-sm font-semibold text-gray-900">
            {a.name}
          </span>
          {runningCount > 0 ? (
            <span className="agent-running badge-running shrink-0">
              <span className="dot" />
              <span className="agent-running-count">
                {runningCount} running
              </span>
            </span>
          ) : a.pinned === false ? (
            <button
              className="btn-agent-pin inline-flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-md text-gray-600 opacity-0 group-hover:opacity-100 hover:bg-gray-200 hover:text-gray-900 transition"
              onClick={(e) => {
                e.stopPropagation();
                setPinned(a, true);
              }}
              title="Pin to sidebar"
              aria-label={`Pin ${a.name} to the sidebar`}
            >
              <IconPin size={13} />
            </button>
          ) : (
            !a.is_system && (
              <button
                className="btn-agent-unpin inline-flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-md text-gray-600 opacity-0 group-hover:opacity-100 hover:bg-gray-200 hover:text-gray-900 transition"
                onClick={(e) => {
                  e.stopPropagation();
                  setPinned(a, false);
                }}
                title="Unpin from sidebar"
                aria-label={`Unpin ${a.name} from the sidebar`}
              >
                <IconPinnedOff size={13} />
              </button>
            )
          )}
          <button
            className="btn-session-add inline-flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-md text-gray-600 opacity-0 group-hover:opacity-100 hover:bg-gray-200 hover:text-gray-900 transition"
            onClick={(e) => {
              e.stopPropagation();
              openCreateRow(a.id);
            }}
            title="New session"
            aria-label={`New session for ${a.name}`}
          >
            <IconPlus size={14} />
          </button>
        </div>

        {isExpanded && (
          <div className="session-rail ml-[15px] mt-0.5 mb-1 flex flex-col gap-0.5 border-l-[1.5px] border-gray-400/70 pl-3">
            {isSessionForm && (
              <fieldset aria-busy={creating} aria-label="New session" className="session-create min-w-0 rounded-lg border border-primary-100 bg-primary-50/60 p-2">
                <div className="flex items-center gap-1.5">
                  <input
                    className="min-w-0 flex-1 rounded-md border border-gray-400 bg-card px-2 py-1 text-[13px] text-gray-900 outline-none placeholder:text-gray-600 focus:border-primary"
                    placeholder="Session name"
                    value={newName}
                    disabled={creationLocked}
                    autoFocus
                    onChange={(e) => setNewName(e.target.value)}
                    onKeyDown={(e) => {
                      if (isImeComposing(e.nativeEvent)) return;
                      if (e.key === "Enter") {
                        e.preventDefault();
                        if (!needsCredential) void createSession(a.id);
                      }
                      if (e.key === "Escape") closeCreateRow();
                    }}
                  />
                  <button
                    type="button"
                    className="btn-create inline-flex size-6 shrink-0 items-center justify-center rounded-md bg-primary text-white transition-colors hover:bg-primary-700 disabled:opacity-50"
                    onClick={() => createSession(a.id)}
                    disabled={needsCredential || creationLocked}
                    title="Create session"
                    aria-label="Create session"
                  >
                    {creating ? <IconLoader2 size={13} className="animate-spin" aria-hidden /> : <IconCheck size={13} />}
                  </button>
                  <button
                    type="button"
                    className="btn-create-cancel inline-flex size-6 shrink-0 items-center justify-center rounded-md text-gray-700 transition-colors hover:bg-gray-200"
                    onClick={closeCreateRow}
                    title="Cancel"
                    aria-label="Cancel new session"
                  >
                    <IconX size={13} />
                  </button>
                </div>

                {creating && <p role="status" className="mt-1 text-xs text-muted-foreground">Creating session…</p>}
                {needsCredential && <p className="mt-1 text-xs text-muted-foreground">
                  Choose a credential for {backendLabel(effectiveEngine)}{inheritedCredentialConflict
                    ? " or use the agent default engine." : " or restore the default sign-in."}
                </p>}
                {/* Overrides stay one click away: a session normally
                  * inherits the agent's engine, credential and the
                  * server's default working dir, and the design's
                  * sidebar shows none of that — but the capability
                  * can't just vanish with the old form. */}
                <button
                  type="button"
                  className="btn-session-advanced mt-1.5 font-mono text-[10.5px] text-gray-700 hover:text-primary"
                  onClick={() => setShowAdvanced((v) => !v)}
                  disabled={creationLocked}
                >
                  {showAdvanced ? "− less" : "+ working dir · engine"}
                </button>

                {showAdvanced && (
                  <div className="mt-1.5 flex flex-col gap-1.5">
                    <input
                      className="session-working-dir rounded-md border border-gray-400 bg-card px-2 py-1 font-mono text-[11.5px] text-gray-900 outline-none placeholder:text-gray-600 focus:border-primary"
                      placeholder="working dir (default: server's)"
                      value={workingDir}
                      disabled={creationLocked}
                      onChange={(e) => setWorkingDir(e.target.value)}
                    />
                    <select
                      className="session-backend-select rounded-md border border-gray-400 bg-card px-2 py-1 text-[11.5px] text-gray-900 outline-none focus:border-primary"
                      value={formBackend}
                      disabled={creationLocked}
                      onChange={(e) => {
                        const choice = e.target.value;
                        const nextEngine = choice || a.backend;
                        if (nextEngine !== effectiveEngine && selectedCredential?.backend !== nextEngine) {
                          setFormCredentialId("");
                        }
                        setFormBackend(choice);
                      }}
                      aria-label="Engine"
                    >
                      <option value="">Agent default ({backendLabel(a.backend)}{backendRead.status === "ready"
                        && !discoveredBackends.includes(a.backend) ? ", CLI not detected" : ""})</option>
                      {discoveredBackends.map((backend) => (
                        <option key={backend} value={backend}>{backendLabel(backend)}</option>
                      ))}
                    </select>
                    <BackendCatalogStatus read={backendRead} />
                    {(a.credential_id || formCredentialId || credentialOptions.length > 0) && (
                      <select
                        className="session-credential-select rounded-md border border-gray-400 bg-card px-2 py-1 text-[11.5px] text-gray-900 outline-none focus:border-primary"
                        value={formCredentialId}
                        disabled={creationLocked}
                        onChange={(e) => setFormCredentialId(e.target.value)}
                        aria-label="Credential"
                      >
                        <option value="" disabled={inheritedCredentialConflict}>{a.credential_id
                          ? `Agent credential (${credentials.find((c) => c.id === a.credential_id)?.label ?? "saved credential"}${inheritedCredentialConflict ? `, requires ${backendLabel(a.backend)}` : ""})`
                          : "Host default sign-in"}</option>
                        {invalidCredential && (
                          <option value={formCredentialId} disabled>
                            {selectedCredential ? `${selectedCredential.label} (requires ${backendLabel(selectedCredential.backend)})`
                              : "Selected credential (unavailable)"}
                          </option>
                        )}
                        {credentialOptions.map((c) => (
                          <option key={c.id} value={c.id}>{c.label}</option>
                        ))}
                      </select>
                    )}
                  </div>
                )}
              </fieldset>
            )}
            {visible.map((s) => {
              const isActive =
                mainView === "chat" && s.id === activeSessionId;
              const archiving = archivesInFlight.has(s.id);
              return (
                <div
                  key={s.id}
                  data-session-id={s.id}
                  className={`session-item group/session flex items-center gap-2.5 rounded-lg px-2.5 py-1.5 cursor-pointer transition-colors ${
                    isActive
                      ? "active bg-primary-50 border border-primary-100 shadow-[0_1px_2px_rgba(37,99,184,0.06)]"
                      : "border border-transparent hover:bg-gray-100"
                  }`}
                  onClick={() => selectSession(s.id, a.id)}
                >
                  <span
                    className={`status-dot status-${s.status} inline-block size-[7px] shrink-0 rounded-full ${
                      s.status === "running"
                        ? "bg-success animate-pulse"
                        : "bg-gray-500"
                    }`}
                  />
                  <span
                    className={`session-name truncate text-[13.5px] ${
                      isActive
                        ? "font-semibold text-gray-950"
                        : "text-gray-800"
                    }`}
                  >
                    {s.name}
                  </span>
                  {s.origin === "delegation" && (
                    <span
                      className="delegation-marker inline-flex shrink-0 text-gray-600"
                      title="Delegation session"
                      aria-label="Delegation session"
                    >
                      <IconSubtask size={12} />
                    </span>
                  )}
                  {isActive && (
                    <span className="session-status ml-auto shrink-0 font-mono text-[10px] text-primary-300">
                      {s.status}
                    </span>
                  )}
                  <button
                    className={`btn-session-archive inline-flex size-5 shrink-0 items-center justify-center rounded-md text-gray-600 transition-opacity ${
                      archiving ? "opacity-100" : "opacity-0 hover:bg-warn-bg hover:text-warn-foreground group-hover/session:opacity-100"
                    } ${isActive ? "" : "ml-auto"}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      archiveSession(s.id);
                    }}
                    disabled={archiving}
                    aria-busy={archiving || undefined}
                    title={archiving ? "Archiving session…" : "Archive session"}
                    aria-label={`Archive ${s.name}`}
                  >
                    {archiving ? <IconLoader2 size={12} className="motion-safe:animate-spin" /> : <IconArchive size={12} />}
                  </button>
                </div>
              );
            })}
            {visible.length === 0 && (
              <button
                type="button"
                className="session-empty px-2.5 py-1.5 text-left text-[12.5px] text-gray-700 hover:text-gray-900"
                onClick={() => openCreateRow(a.id)}
              >
                No sessions — start one
              </button>
            )}
            {hiddenDelegations > 0 && (
              <DelegationToggle count={hiddenDelegations} />
            )}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="agent-list shrink-0">
      <SidebarSectionHeader
        label="Agents"
        className="agent-list-header"
        action={{
          icon: <IconPlus size={14} />,
          onClick: () => openAgentForm(null),
          title: "New agent",
          label: "New agent",
          className: "btn-agent-add",
        }}
      />
      {archiveError && <div role="alert" className="px-2 text-xs text-destructive"><p>{archiveError}</p>
        {archiveConfirmation && <button className="underline" disabled={archiveConfirmation.busy}
          onClick={() => void archiveConfirmation.run()}>Confirm original archive</button>}
      </div>}
      {createError && <p role="alert" className="px-2 text-xs text-destructive">{createError}</p>}
      {createdId && <div className="px-2 text-xs text-destructive">
        <p role="alert">Session created ({createdId}), but the list could not be confirmed.</p>
        <button className="underline" onClick={() => void sessionList.refresh()}>Check session list</button>
      </div>}
      {sessionRead.error && <p role="alert" className="px-2 text-xs text-destructive">
        Sessions not synchronized: {sessionRead.error}
        <button className="ml-2 underline" onClick={() => void sessionList.refresh()}>Refresh sessions</button>
      </p>}

      <div className="agent-list-items flex flex-col">
        <SortablePins
          ids={pinned.map((a) => a.id)}
          nameOf={(id) => agents.find((a) => a.id === id)?.name ?? id}
          onReorder={reorder}
        >
          {pinned.map((a) => (
            <SortablePin key={a.id} id={a.id}>
              {(handle) => renderAgent(a, handle)}
            </SortablePin>
          ))}
        </SortablePins>
        {present.map((a) => (
          <div key={a.id}>{renderAgent(a, {})}</div>
        ))}
      </div>
    </div>
  );
}

/** The hidden-delegation pill (agent-collaboration.md §6) — delegation
 * children are noise in the sidebar until you want them. */
function DelegationToggle({ count }: { count: number }) {
  const setShowDelegations = useSessionStore((s) => s.setShowDelegations);
  return (
    <button
      type="button"
      className="delegation-toggle btn-show-delegations px-2.5 py-1 text-left font-mono text-[10.5px] text-gray-700 hover:text-primary"
      onClick={() => setShowDelegations(true)}
      title="Show delegation sessions"
    >
      + {count} delegation{count === 1 ? "" : "s"}
    </button>
  );
}

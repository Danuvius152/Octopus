import { IconCpu } from "@tabler/icons-react";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { selectSession } from "../lib/selectSession";
import { Button } from "./ui/button";
import { backendLabel } from "../hooks/useBackendCatalog";
import type { RuntimeModelDefault } from "../hooks/useModelSettings";
import { PageHeader } from "./PageHeader";

export function ChatSessionHeading({ session: activeSession, connected, runtimeModelDefaults, onOpenModel, onToggleSidebar }: {
  session?: SessionInfo;
  connected: boolean;
  runtimeModelDefaults?: RuntimeModelDefault[];
  onOpenModel?: () => void;
  onToggleSidebar: () => void;
}) {
  const activeAgent = useSessionStore((state) => state.agents.find((agent) => agent.id === activeSession?.agent_id));
  const defaults = activeSession?.defaults_source !== "backend" ? activeAgent : undefined;
  const credential = activeSession?.credential_id || defaults?.credential_id || null;
  const backend = activeSession?.backend ?? "claude-code";
  const defaultsKnown = activeSession?.defaults_source === "backend" || !activeSession?.agent_id || !!activeAgent;
  const observedDefault = defaultsKnown && runtimeModelDefaults
    ?.find((entry) => entry.backend === backend && entry.credential === credential)?.name;
  const model = activeSession?.model || defaults?.model || observedDefault || "Default";
  const effort = activeSession?.reasoning_effort || defaults?.reasoning_effort;
  const speed = activeSession?.service_tier || defaults?.service_tier;
  const engine = backendLabel(backend);
  const settingsLabel = [engine, model, effort, speed === "fast" && "Fast"].filter(Boolean).join(" · ");
  const renderStatusBadge = (status: string | undefined) => {
    const base = "status-badge font-mono text-[11px]";
    if (status === "running") {
      return (
        <span className={`${base} status-running inline-flex items-center gap-1.5 text-primary`}>
          <span className="inline-block size-1.5 rounded-full bg-primary animate-pulse" />
          running
        </span>
      );
    }
    // Idle stays in the DOM as a test hook and a low-key cue.
    return <span className={`${base} status-idle text-gray-700`}>idle</span>;
  };

  // Delegation child sessions get a small banner under the header that
  // names the parent agent + session and offers a "Open parent" link.
  // The parent_session_id field is set by DelegationManager when the
  // child Session row is created (agent-collaboration.md §4.1).
  const parentSession = useSessionStore((s) => activeSession?.parent_session_id
    ? s.sessions.find(session => session.id === activeSession.parent_session_id)
      ?? s.archivedSessions.find(session => session.id === activeSession.parent_session_id) : undefined);
  const parentAgent = useSessionStore((s) => s.agents.find(agent => agent.id === parentSession?.agent_id));
  const openParent = () => {
    if (!parentSession) return;
    void selectSession(parentSession.id, parentSession.agent_id);
  };

  const header = (
    <PageHeader
      className="chat-header"
      onToggleSidebar={onToggleSidebar}
      icon={
        activeSession && activeAgent ? (
          <span className="tile tile-plain shrink-0" aria-hidden>
            {activeAgent.avatar || "🐙"}
          </span>
        ) : undefined
      }
      crumbs={
        activeSession
          ? [activeAgent?.name ?? "Agent", activeSession.name || "Session"]
          : ["Octopus"]
      }
      meta={activeSession ? renderStatusBadge(activeSession.status) : undefined}
      actions={
        <>
          {activeSession && onOpenModel && !activeSession.archived && (
            <Button
              className="btn-model-settings" variant="ghost" size="sm"
              title={settingsLabel} aria-label={`Conversation settings: ${settingsLabel}`}
              onClick={onOpenModel}
            >
              <IconCpu size={16} className="shrink-0" />
              <span className="whitespace-nowrap">{engine}</span>
              <span className="chat-model-label truncate">· {model}</span>
              {effort && <span className="chat-model-detail whitespace-nowrap">· {effort}</span>}
              {speed === "fast" && <span className="chat-model-detail whitespace-nowrap">· Fast</span>}
            </Button>
          )}
          <span
            className={`conn-status ${connected ? "on" : "off"} pill ${
              connected ? "pill-success" : "pill-warn"
            }`}
            title={connected ? "Connected" : "Disconnected"}
          >
            <span className={`dot ${connected ? "" : "animate-pulse"}`} />
            {/* The word is the first thing to go on a phone; the dot and its
              * colour say the same thing in 6px (mobile.md §4). */}
            <span className="conn-label">
              {connected ? "Connected" : "Disconnected"}
            </span>
          </span>
        </>
      }
    />
  );

  // Fork sessions get a banner mirroring the delegation one
  // (session-rewind.md §6.4): "Forked from <parent> at message N".
  // The parent may be deleted (dangling ref) → "(deleted session)".
  const forkParentSession = useSessionStore((s) => activeSession?.forked_from_session_id
    ? s.sessions.find(session => session.id === activeSession.forked_from_session_id)
      ?? s.archivedSessions.find(session => session.id === activeSession.forked_from_session_id) : undefined);
  const openForkParent = () => {
    if (!forkParentSession) return;
    void selectSession(forkParentSession.id, forkParentSession.agent_id);
  };

  const forkBanner =
    activeSession?.forked_from_session_id != null ? (
      <div
        className="chat-fork-banner flex items-center gap-2 px-4 py-1.5 shrink-0 border-b border-border bg-primary/5 text-xs text-muted-foreground"
        data-testid="fork-banner"
      >
        <span className="text-primary" aria-hidden>
          ⑂
        </span>
        <span>
          Forked from{" "}
          <span className="font-medium text-foreground">
            {forkParentSession?.name ?? "(deleted session)"}
          </span>
          {activeSession.fork_is_full_copy ? (
            <>{" "}— full copy of the working dir</>
          ) : (
            <>
              {" "}at message{" "}
              <strong>{(activeSession.fork_after_seq ?? -1) + 1}</strong>
            </>
          )}
        </span>
        {forkParentSession && (
          <button
            type="button"
            onClick={openForkParent}
            className="btn-open-fork-parent ml-auto text-primary hover:underline"
          >
            back to parent →
          </button>
        )}
      </div>
    ) : null;

  const delegationBanner =
    activeSession?.parent_session_id ? (
      <div
        className="chat-delegation-banner flex items-center gap-2 px-4 py-1.5 shrink-0 border-b border-border bg-primary/5 text-xs text-muted-foreground"
        data-testid="delegation-banner"
      >
        <span className="text-primary">↑</span>
        <span>
          Delegated from{" "}
          <span className="font-medium text-foreground">
            {parentAgent?.name || "another agent"}
          </span>
          {parentSession && (
            <>
              {" "}(session{" "}
              <code className="font-mono text-[10px]">
                {parentSession.id.slice(0, 8)}
              </code>
              )
            </>
          )}
        </span>
        {parentSession && (
          <button
            type="button"
            onClick={openParent}
            className="btn-open-parent ml-auto text-primary hover:underline"
          >
            Open parent →
          </button>
        )}
      </div>
    ) : null;

  return <>{header}{forkBanner}{delegationBanner}</>;
}

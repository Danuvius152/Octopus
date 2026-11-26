import { useCallback, useEffect, useEffectEvent, useRef, useState } from "react";
import { useStore } from "zustand";
import { IconEye, IconFile, IconRestore, IconTrash } from "@tabler/icons-react";
import {
  useSessionStore,
  type Agent,
  type SessionInfo,
} from "../stores/sessionStore";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { selectSession } from "../lib/selectSession";
import { sessions as sessionList } from "../lib/sessionList";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}

/** Manage page for archived sessions, reached from the account menu. Lists
 * every archived session grouped by its owning agent (they no longer clutter
 * the sidebar). "View" opens it read-only; "Unarchive" brings it back live. */
export function ArchivedSessionsDialog(props: Props) {
  const account = useSessionStore((s) => s.accountGeneration);
  return props.open ? <ArchiveOpening key={account} {...props} /> : null;
}

function ArchiveOpening({ open, onOpenChange }: Props) {
  const token = useSessionStore((s) => s.token);
  const agents = useSessionStore((s) => s.agents);
  const archived = useSessionStore((s) => s.archivedSessions);
  const canOpenFile = useSessionStore((s) => s.mainView === "chat" && s.activeSessionId !== null);
  const read = useSessionStore((s) => s.sessionRead);
  const identity = useSessionStore((s) => s.identity?.user_id);
  const live = useSessionStore((s) => s.sessions);
  const restoring = useStore(sessionList.restorations.store, state => state.entries);
  const restoreError = useStore(sessionList.restorations.store, state => state.error);
  const deletions = useStore(sessionList.deletions);
  const deletingAll = useStore(sessionList.archiveBatch, state => state.running);
  const batchRestored = useStore(sessionList.archiveBatch, state => state.restored);
  const [deleteFeedback, setDeleteFeedback] = useState("");
  const [errors, setErrors] = useState<{ [id: string]: string }>({});
  const feedback = useRef(new Map<string, object>());
  const navigation = useRef<object | null>(null);
  const [openingView] = useState(() => useSessionStore.getState().mainView);
  const foreground = useRef(false);
  const close = useEffectEvent(() => onOpenChange(false));
  useEffect(() => {
    foreground.current = true;
    const unsubscribe = useSessionStore.subscribe((state) => {
      if (state.mainView !== openingView && foreground.current) {
        foreground.current = false;
        close();
      }
    });
    return () => { foreground.current = false; unsubscribe(); };
  }, [openingView]);

  const fetchArchived = useCallback(async () => {
    await sessionList.refresh(true);
  }, []);

  useEffect(() => {
    if (open) {
      void fetchArchived();
      sessionList.restorations.observePending();
    }
  }, [open, token, identity, fetchArchived]);

  // Select an archived session for read-only viewing. ChatView detects the
  // active session is archived (it's in archivedSessions) and renders the
  // read-only banner + history. Mirrors SessionList's old selectSession.
  const view = async (s: SessionInfo) => {
    navigation.current = null;
    foreground.current = false;
    onOpenChange(false);
    await selectSession(s.id, s.agent_id);
  };

  const unarchive = async (s: Pick<SessionInfo, "id">, confirming = false) => {
    if (!foreground.current) return;
    const pending = confirming ? sessionList.restorations.confirm(s.id) : sessionList.restorations.restore(s.id);
    if (!pending) return;
    const intent = {};
    feedback.current.set(s.id, intent);
    navigation.current = intent;
    setErrors(previous => ({ ...previous, [s.id]: "" }));
    const { observation, finish } = sessionList.observe();
    try {
      const result = await pending;
      if (!foreground.current || feedback.current.get(s.id) !== intent || !sessionList.accountCurrent(observation)) return;
      if (result.message) setErrors(previous => ({ ...previous, [s.id]: result.message! }));
      if (navigation.current === intent && result.session &&
          (sessionList.current(observation) || sessionList.initialGenerationCurrent(observation))) {
        foreground.current = false;
        onOpenChange(false);
        await selectSession(result.session.id, result.session.agent_id, { fresh: true });
      }
    } finally {
      finish();
      if (feedback.current.get(s.id) === intent) feedback.current.delete(s.id);
    }
  };

  const unsettled = Object.keys(restoring).filter(id => !archived.some(row => row.id === id) &&
    ["writing", "unknown", "confirming"].includes(restoring[id].phase));

  const deleteArchived = async (target: Pick<SessionInfo, "id" | "name">, retry = false) => {
    if (!retry) {
      if (!window.confirm(`Permanently delete "${target.name}"?`)) return;
      setDeleteFeedback("");
    }
    if (await sessionList.deleteArchived(target) === "restored" && foreground.current) {
      setDeleteFeedback(`"${target.name}" was restored and has been kept.`);
    }
  };

  const deleteAll = () => {
    const confirmation = sessionList.capture();
    const targets = archived.map(({ id, name }) => ({ id, name }));
    const noun = targets.length === 1 ? "session" : "sessions";
    if (!window.confirm(`Permanently delete ${targets.length} archived ${noun}?`)) return;
    setDeleteFeedback("");
    void sessionList.deleteArchivedBatch(targets, confirmation);
  };

  const batchFeedback = batchRestored === 1
    ? "1 session was restored and has been kept."
    : `${batchRestored} sessions were restored and have been kept.`;

  const groups: { agent: Agent | null; items: SessionInfo[] }[] = agents
    .map((a) => ({
      agent: a as Agent | null,
      items: archived.filter((s) => s.agent_id === a.id),
    }))
    .filter((g) => g.items.length > 0);
  const knownAgentIds = new Set(agents.map((a) => a.id));
  const orphans = archived.filter(
    (s) => !s.agent_id || !knownAgentIds.has(s.agent_id)
  );
  if (orphans.length) groups.push({ agent: null, items: orphans });

  return (
    <Dialog open={open} onOpenChange={(next) => {
      if (!next) foreground.current = false;
      onOpenChange(next);
    }}>
      <DialogContent className="archived-sessions-dialog max-w-xl">
        {read.error && <p role="alert">Sessions not synchronized: {read.error}</p>}
        {restoreError && <div><p role="alert">{restoreError}</p><button onClick={() => sessionList.restorations.observePending()}>Retry restoration identities</button></div>}
        <DialogHeader>
          <DialogTitle>Archived sessions</DialogTitle>
          <DialogDescription>
            Sessions you've archived, grouped by agent. View one read-only, or
            unarchive it to continue the conversation as a live session.
          </DialogDescription>
        </DialogHeader>

        {(archived.length > 0 || deletingAll) && <button
          type="button"
          className="inline-flex items-center gap-1 justify-self-start rounded-md px-2 py-1 text-sm text-destructive hover:bg-destructive/10 disabled:opacity-50"
          onClick={deleteAll}
          disabled={deletingAll || Object.values(deletions).some(entry => entry.phase === "deleting")}
        >
          <IconTrash size={15} />
          {deletingAll ? "Deleting all…" : "Delete all"}
        </button>}
        {deleteFeedback && <p role="status" className="text-sm">{deleteFeedback}</p>}
        {!deletingAll && batchRestored > 0 && <p role="status" className="text-sm">{batchFeedback}</p>}
        {Object.values(deletions).filter(entry => entry.phase === "retry").map(entry => (
          <div key={entry.id} className="text-sm">
            <p role="alert">Deletion of "{entry.name}" could not be confirmed.</p>
            <button type="button" className="underline disabled:opacity-50" disabled={deletingAll} onClick={() => void deleteArchived(entry, true)}>Retry deletion</button>
          </div>
        ))}

        {unsettled.map(id => <div key={id} className="restoration-pending text-sm">
          <p>Restoration of {live.find(row => row.id === id)?.name ?? id} is unconfirmed.</p>
          {errors[id] && <p role="alert">{errors[id]}</p>}
          <button onClick={() => unarchive({ id }, true)} disabled={restoring[id].phase === "confirming"}>Confirm restoration</button>
        </div>)}

        {archived.length === 0 ? (
          <div className="archived-empty rounded-lg border border-dashed border-border bg-muted/30 px-4 py-8 text-center text-sm text-muted-foreground">
            No archived sessions. Type{" "}
            <code className="font-mono text-foreground">/archive</code> in a
            chat to archive its history.
          </div>
        ) : (
          <div className="archived-groups space-y-4 max-h-[60vh] overflow-y-auto">
            {groups.map(({ agent, items }, gi) => (
              <div
                key={agent?.id ?? `orphan-${gi}`}
                className="archived-agent-group"
              >
                <div className="archived-agent-header flex items-center gap-1.5 px-1 pb-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  <span className="text-sm leading-none">
                    {agent?.avatar || "🐙"}
                  </span>
                  <span className="truncate">
                    {agent?.name ?? "Unknown agent"}
                  </span>
                </div>
                <div className="flex flex-col gap-1">
                  {items.map((s) => (
                    <div
                      key={s.id}
                      className="archived-session-row group flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card px-3 py-2"
                    >
                      <span className="archived-session-name min-w-0 flex-1 truncate text-sm italic text-foreground">
                        {s.name}
                      </span>
                      <button
                        className="btn-archived-view inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
                        onClick={() => view(s)}
                        title="View read-only history"
                      >
                        <IconEye size={13} />
                        View
                      </button>
                      {canOpenFile && <button
                        type="button"
                        className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
                        aria-haspopup="dialog"
                        onClick={(event) => {
                          event.currentTarget.focus({ preventScroll: true });
                          useSessionStore.getState().openViewer(s.id, "", { mode: "edit" });
                        }}
                      >
                        <IconFile size={13} aria-hidden="true" />
                        Open file
                      </button>}
                      <button
                        className="btn-archived-unarchive inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-primary hover:bg-primary/10"
                        onClick={() => unarchive(s)}
                        disabled={!!restoreError || !!restoring[s.id] && !["ready", "failed"].includes(restoring[s.id].phase)}
                        title="Unarchive — bring this session back as a live session"
                      >
                        <IconRestore size={13} />
                        Unarchive
                      </button>
                      <button
                        type="button"
                        className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-destructive hover:bg-destructive/10 disabled:opacity-50"
                        onClick={() => void deleteArchived(s)}
                        disabled={deletingAll || !!deletions[s.id]}
                      >
                        <IconTrash size={13} />
                        {deletions[s.id]?.phase === "deleting" ? "Deleting…" : "Delete"}
                      </button>
                      {restoring[s.id] && ["writing", "unknown", "confirming"].includes(restoring[s.id].phase) && <button
                        onClick={() => unarchive(s, true)} disabled={restoring[s.id].phase === "confirming"}>
                        Confirm restoration
                      </button>}
                      {restoring[s.id]?.phase === "unknown" && <p className="basis-full text-xs">Restoration is unconfirmed. Confirm its state before restoring again.</p>}
                      {errors[s.id] && <p role="alert" className="basis-full text-xs">{errors[s.id]}</p>}
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

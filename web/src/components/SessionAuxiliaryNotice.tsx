import { useMemo } from "react";
import { confirmAuxiliaryTask, loadBgTaskSnapshot, loadResearchSnapshot } from "../lib/hydrateSession";
import { representedBgTaskIds } from "../lib/toolIdentity";
import { useSessionStore, type SessionAuxiliaryKind } from "../stores/sessionStore";

const readers = { background: loadBgTaskSnapshot, research: loadResearchSnapshot };
const labels = { background: "background task details", research: "research updates" };

/** Auxiliary failures retain their cards without blocking the conversation. */
export function SessionAuxiliaryNotice({ sessionId }: { sessionId: string }) {
  const errors = useSessionStore((s) => s.sessionAuxiliaryErrors[sessionId]);
  const failedBgTasks = useSessionStore((s) => s.bgTaskReadFailures[sessionId]);
  const transcript = useSessionStore((s) => failedBgTasks ? s.messages[sessionId] : undefined);
  const represented = useMemo(() => representedBgTaskIds(transcript), [transcript]);
  const hasUnrepresentedBgError = failedBgTasks?.some((id) => !represented.has(id));
  const failedResearch = useSessionStore((s) => s.researchReadFailures[sessionId]);
  if (!errors && !hasUnrepresentedBgError && !failedResearch) return null;
  const messages = { ...errors };
  if (hasUnrepresentedBgError) messages.background ??= "Could not refresh background task details.";
  if (failedResearch) messages.research ??= "Could not refresh research updates.";
  return <div className="shrink-0 px-4 py-1 text-xs text-muted-foreground">
    {(Object.keys(messages) as SessionAuxiliaryKind[]).map((kind) => <p key={kind} role="alert">
      {messages[kind]}{" "}
      <button type="button" className="underline" aria-label={`Retry ${labels[kind]}`}
        onClick={() => {
          const state = useSessionStore.getState();
          if (state.sessionAuxiliaryErrors[sessionId]?.[kind]) void readers[kind](sessionId).catch(() => {});
          const failures = kind === "background" ? state.bgTaskReadFailures : state.researchReadFailures;
          for (const taskId of failures[sessionId] ?? []) {
            if (kind !== "background" || !represented.has(taskId)) confirmAuxiliaryTask(kind, sessionId, taskId, "retry");
          }
        }}>Retry</button>
    </p>)}
  </div>;
}

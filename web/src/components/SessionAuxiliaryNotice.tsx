import { loadBgTaskSnapshot, loadResearchSnapshot } from "../lib/hydrateSession";
import { useSessionStore, type SessionAuxiliaryKind } from "../stores/sessionStore";

const readers = { background: loadBgTaskSnapshot, research: loadResearchSnapshot };
const labels = { background: "background task details", research: "research updates" };

/** Auxiliary failures retain their cards without blocking the conversation. */
export function SessionAuxiliaryNotice({ sessionId }: { sessionId: string }) {
  const errors = useSessionStore((s) => s.sessionAuxiliaryErrors[sessionId]);
  if (!errors) return null;
  return <div className="shrink-0 px-4 py-1 text-xs text-muted-foreground">
    {(Object.keys(errors) as SessionAuxiliaryKind[]).map((kind) => <p key={kind} role="alert">
      {errors[kind]}{" "}
      <button type="button" className="underline" aria-label={`Retry ${labels[kind]}`}
        onClick={() => { void readers[kind](sessionId).catch(() => {}); }}>Retry</button>
    </p>)}
  </div>;
}

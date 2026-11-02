import { accountFetch as fetch } from "./accountFetch";
import { useSessionStore, type Message, type MessagePageResult, type StreamingItem } from "../stores/sessionStore";
import type { MessageInput } from "./turnOutcome";
import { sessions } from "./sessionList";

const API = window.location.origin;

/** Apply the transcript half of a `GET /api/sessions/{id}` snapshot.
 *
 * Three places load a snapshot — selecting a session, the WebSocket reconnect
 * refetch, and the archived-session viewer — and each owes the store the same
 * three things: the messages, the WS dedup baseline, and where the window
 * starts so scroll-back knows what to ask for (polish-2026-09.md §4 B2). It
 * lives here so a fourth caller cannot get two of the three right.
 */
export function applyTranscript(
  sessionId: string,
  data: Record<string, unknown>,
  nativeBaseline?: Record<string, StreamingItem>,
): void {
  const store = useSessionStore.getState();
  if (data.transcript_integrity === "duplicate_seq") {
    store.setMessages(sessionId, []);
    store.setMessageWindow(sessionId, null, false);
    sessions.patch(sessionId, { transcript_integrity: "duplicate_seq" });
    return;
  }
  const messages = (data.messages as MessageInput[]) || [];
  const snapshotHigh = typeof data.next_message_seq === "number"
    ? data.next_message_seq - 1 : Math.max(-1, ...messages.map((m) => m.seq ?? -1));
  const currentHigh = store.lastAppliedSeq[sessionId] ?? -1;
  const oldest = typeof data.oldest_loaded_seq === "number" ? data.oldest_loaded_seq : null;
  const currentOldest = store.oldestSeq[sessionId];
  const current = store.messages[sessionId] ?? [];
  // A prefix can only be kept when every row up to the snapshot is present.
  // Otherwise its old cursor hides a disconnected middle after a long outage.
  const prefixSeqs = current.filter((m) => typeof m.seq === "number" &&
    typeof oldest === "number" && m.seq < oldest).map((m) => m.seq!).sort((a, b) => a - b);
  const preservePrefix = typeof oldest === "number" && typeof currentOldest === "number" &&
    currentOldest < oldest && prefixSeqs[0] === currentOldest &&
    prefixSeqs.length === oldest - currentOldest &&
    prefixSeqs.every((seq, index) => seq === currentOldest + index);
  const retained = current.filter((m) => typeof m.seq === "number" &&
    (m.seq > snapshotHigh || (preservePrefix && m.seq < oldest!)));
  const merged = [...retained, ...messages];
  const sequenced = new Map(merged.filter((m) => typeof m.seq === "number").map((m) => [m.seq!, m]));
  const rows = [...sequenced.values()].sort((a, b) => a.seq! - b.seq!);
  rows.push(...messages.filter((m) => typeof m.seq !== "number"));
  store.setMessages(sessionId, rows, currentHigh >= snapshotHigh && Boolean(
    store.streamingText[sessionId] || Object.keys(store.streamingItems[sessionId] || {}).length), nativeBaseline);
  store.setMessageWindow(
    sessionId,
    preservePrefix ? currentOldest : oldest,
    preservePrefix ? Boolean(store.hasMoreMessages[sessionId]) : Boolean(data.has_more_messages)
  );
  // Any WS event at or below the snapshot's high-water mark is already in the
  // messages above, so the handler must drop it rather than render it twice.
  if (typeof data.next_message_seq === "number") {
    store.setLastAppliedSeq(sessionId, (data.next_message_seq as number) - 1);
  }
}

export type OlderMessagesResult = MessagePageResult | { state: "unneeded" | "in_flight" };
const inFlight = new Map<string, object>();

/** One reader owns the original observation and window. Ending a view frees
 * its read slot without allowing its late response to publish into a new view. */
export function observeOlderMessages(sessionId: string, beforeMerge?: () => void) {
  const start = useSessionStore.getState();
  const lifetime = sessions.observe(sessionId, start);
  const identity = {};
  const oldest = start.oldestSeq[sessionId];
  const basis = { window: start.messageWindows[sessionId], beforeSeq: oldest as number };
  let active = true;
  const current = () => active && sessions.current(lifetime.observation) &&
    useSessionStore.getState().messageWindows[sessionId] === basis.window;
  const finish = () => {
    if (inFlight.get(sessionId) === identity) inFlight.delete(sessionId);
    lifetime.finish();
  };
  const done = (async (): Promise<OlderMessagesResult> => {
    try {
      if (!sessionId || !start.token || !start.hasMoreMessages[sessionId] || typeof oldest !== "number" || oldest <= 0) return { state: "unneeded" };
      if (inFlight.has(sessionId)) return { state: "in_flight" };
      inFlight.set(sessionId, identity);
      const response = await fetch(`${API}/api/sessions/${encodeURIComponent(sessionId)}/messages?before_seq=${oldest}`, {
        headers: { Authorization: `Bearer ${start.token}` }, signal: AbortSignal.timeout(30_000),
      });
      if (!response.ok) throw new Error(`Could not load earlier messages (HTTP ${response.status}).`);
      const page = await response.json();
      if (!current()) return { state: "obsolete" };
      if (!Array.isArray(page.messages)) throw new Error("Invalid history page.");
      beforeMerge?.();
      return useSessionStore.getState().prependMessages(sessionId, page.messages as Message[],
        typeof page.oldest_loaded_seq === "number" ? page.oldest_loaded_seq : null,
        Boolean(page.has_more_messages), basis);
    } catch (error) {
      return current() ? { state: "failed", error: error instanceof Error ? error.message : String(error) } : { state: "obsolete" };
    } finally {
      finish();
    }
  })();
  return { done, current, cancel: () => { active = false; finish(); } };
}

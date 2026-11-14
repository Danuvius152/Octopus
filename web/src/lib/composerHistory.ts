import { requestSource } from "./injectedTurns";

/**
 * Shell-style history for the composer: ArrowUp walks back through what you
 * sent, ArrowDown walks forward, and coming back past the newest entry restores
 * whatever you were typing when you started.
 *
 * Selection belongs to a message identity, never an index in a mutable window.
 */

interface HistoryMessage {
  seq?: number;
  role?: string;
  type?: string;
  content?: unknown;
  body_state?: "inline" | "preview";
  detail_ref?: string | null;
  tool_input?: Record<string, unknown>;
}

export interface HistoryEntry {
  identity: number | HistoryMessage;
  text: string;
  seq?: number;
  body_state?: "inline" | "preview";
  detail_ref?: string | null;
}

export interface HistoryState {
  entry: HistoryEntry | null;
  /** What was in the box when navigation started, restored on the way back. */
  draft: string;
}

export const IDLE: HistoryState = { entry: null, draft: "" };

export interface Recall {
  state: HistoryState;
  text: string;
}

/**
 * The user's own sent messages, oldest first — what ArrowUp walks back through.
 *
 * Only real typed turns: an agent reply injected as a user-role message (a
 * delegation reply, a background-task result, a scheduled fire) is something
 * the user never typed, and recalling it would be nonsense. Consecutive
 * duplicates collapse, the way a shell's history does.
 */
export function historyFromMessages(
  messages: HistoryMessage[],
  isInjected: (text: string) => boolean = () => false
): HistoryEntry[] {
  const out: HistoryEntry[] = [];
  for (const m of messages) {
    if (m.role !== "user" || m.type !== "text") continue;
    const text = typeof m.content === "string" ? m.content : "";
    const source = requestSource(m);
    if (!text.trim() || (source === undefined ? isInjected(text) : source !== "human")) continue;
    const previous = out[out.length - 1];
    if (m.body_state !== "preview" && previous?.body_state !== "preview" && previous?.text === text) continue;
    out.push({ identity: m.seq ?? m, text, seq: m.seq, body_state: m.body_state, detail_ref: m.detail_ref });
  }
  return out;
}

/** A removed candidate ends the walk without changing the shared draft. */
export function reconcileHistory(history: HistoryEntry[], state: HistoryState): HistoryState {
  if (!state.entry) return IDLE;
  const entry = history.find((entry) => entry.identity === state.entry!.identity);
  return entry ? { ...state, entry } : IDLE;
}

/** ArrowUp. `null` means "not ours" — let the textarea have the key. */
export function recallPrev(
  history: HistoryEntry[],
  state: HistoryState,
  current: string
): Recall | null {
  if (history.length === 0) return null;
  if (state.entry === null) {
    // Starting out: remember the draft so ArrowDown can bring it back.
    const entry = history[history.length - 1];
    return { state: { entry, draft: current }, text: entry.text };
  }
  const cursor = history.findIndex((entry) => entry.identity === state.entry!.identity);
  if (cursor === -1) return { state: IDLE, text: current };
  if (cursor === 0) {
    // At the oldest. Consume the key rather than letting the caret jump —
    // a shell stays put here too, and falling through would move the caret
    // to the top of a recalled multi-line message unexpectedly.
    return { state: { ...state, entry: history[0] }, text: current };
  }
  const entry = history[cursor - 1];
  return { state: { ...state, entry }, text: entry.text };
}

/** ArrowDown. `null` means "not ours". */
export function recallNext(
  history: HistoryEntry[],
  state: HistoryState,
  current: string
): Recall | null {
  if (state.entry === null) return null; // not navigating; normal caret move
  const cursor = history.findIndex((entry) => entry.identity === state.entry!.identity);
  if (cursor === -1) return { state: IDLE, text: current };
  if (cursor === history.length - 1) {
    return { state: IDLE, text: state.draft }; // back out to the draft
  }
  const entry = history[cursor + 1];
  return { state: { ...state, entry }, text: entry.text };
}

/**
 * Whether ArrowUp/ArrowDown at this caret position should mean "history"
 * rather than "move the caret".
 *
 * The rule is the one editors use: the key is history's only while the caret is
 * on the first line (Up) or the last line (Down), so a recalled multi-line
 * message stays navigable with the same keys.
 */
export function caretWantsHistory(
  value: string,
  caret: number,
  direction: "up" | "down"
): boolean {
  const before = value.slice(0, caret);
  const after = value.slice(caret);
  return direction === "up" ? !before.includes("\n") : !after.includes("\n");
}

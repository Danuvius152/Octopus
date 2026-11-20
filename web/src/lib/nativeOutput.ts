import type { RuntimeMetadata, SubagentRun } from "../api";
import type { Message, StreamingItem } from "../stores/sessionStore";
import { readRuntimeMetadata } from "./toolIdentity";

export function nativeItemKey(value: unknown): string | undefined {
  const identity = readRuntimeMetadata(value);
  const source = identity?.native;
  if (!identity || !source?.item_id) return undefined;
  return JSON.stringify([identity.request_id, identity.attempt_id, source.thread_id, source.turn_id, source.item_id]);
}

export function isNativeChild(value: RuntimeMetadata | null | undefined): boolean {
  return Boolean(value?.native && value.native.thread_id !== value.native.root_thread_id);
}

function workKey(identity: RuntimeMetadata, thread: string | null | undefined, turn: string | null | undefined): string {
  // Missing fields can still match older collected rows. Keep them distinct
  // from an explicit null in a status patch or snapshot.
  return JSON.stringify([identity.request_id, identity.attempt_id,
    thread === undefined ? false : thread, turn === undefined ? false : turn]);
}

export function nativeWorkKey(value: unknown, thread?: unknown, turn?: unknown): string | undefined {
  const identity = readRuntimeMetadata(value);
  return identity && typeof thread === "string" && thread && typeof turn === "string" && turn
    ? workKey(identity, thread, turn) : undefined;
}

type StreamingItems = Record<string, StreamingItem>;
const EMPTY_STREAMING_ITEMS: StreamingItems = {};
const previewsBySnapshot = new WeakMap<StreamingItems, { root: StreamingItems; work: Map<string, StreamingItems> }>();

function streamingGroups(items: StreamingItems) {
  const cached = previewsBySnapshot.get(items);
  if (cached) return cached;
  const root: StreamingItems = {};
  const work = new Map<string, StreamingItems>();
  for (const [key, item] of Object.entries(items)) {
    const metadata = item.runtime_metadata;
    if (!isNativeChild(metadata)) root[key] = item;
    const owner = nativeWorkKey(metadata, metadata.native?.thread_id, metadata.native?.turn_id);
    if (owner) {
      let group = work.get(owner);
      if (!group) work.set(owner, group = {});
      group[key] = item;
    }
  }
  const groups = { root, work };
  previewsBySnapshot.set(items, groups);
  return groups;
}

export function nativeWorkPreviews(items: StreamingItems | undefined, work: string | undefined): StreamingItems {
  return (items && work ? streamingGroups(items).work.get(work) : undefined) ?? EMPTY_STREAMING_ITEMS;
}

export function nativeRootPreviews(items: StreamingItems | undefined): StreamingItems {
  return items ? streamingGroups(items).root : EMPTY_STREAMING_ITEMS;
}

const OBSERVED_CARD_PREFIX = "observed:";

interface NativeHistory {
  statuses: Map<unknown, Message[]>;
  work: Map<string, Message[]>;
  answers: Map<string, Message[]>;
}

// The store replaces a saved window on append, prepend or refresh. Cards share
// its derived index for as long as that exact array can still be read.
const nativeHistories = new WeakMap<Message[], NativeHistory>();

function groupRow<Key>(groups: Map<Key, Message[]>, key: Key, message: Message) {
  const group = groups.get(key);
  if (group) group.push(message);
  else groups.set(key, [message]);
}

function nativeHistory(messages: Message[]): NativeHistory {
  const indexed = nativeHistories.get(messages);
  if (indexed) return indexed;
  const history: NativeHistory = { statuses: new Map(), work: new Map(), answers: new Map() };
  for (const message of messages) {
    if (message.type === "native_subagent") groupRow(history.statuses, message.tool_input?.card_id, message);
    const identity = message.runtime_metadata;
    if (identity && (isNativeChild(identity) || (message.type === "text" && message.role === "assistant"))) {
      const key = workKey(identity, identity.native?.thread_id, identity.native?.turn_id);
      groupRow(history.work, key, message);
      if (message.type === "text" && message.role === "assistant") groupRow(history.answers, key, message);
    }
  }
  nativeHistories.set(messages, history);
  return history;
}

function nativeAnswerRows(messages: Message[], run: SubagentRun): Message[] | undefined {
  const identity = run.runtime_metadata;
  return identity ? nativeHistory(messages).answers.get(workKey(identity, run.native_thread_id, run.native_turn_id)) : undefined;
}

type NativeAnswerPart = { text: string } & (
  { source: "saved"; message: Message } | { source: "preview"; key: string } | { source: "summary" }
);

const NATIVE_ANSWER_SEPARATOR = "\n\n";

export function nativeAnswer(run: SubagentRun, workPreviews: StreamingItems | undefined, messages?: Message[]) {
  const collected = messages && nativeAnswerRows(messages, run);
  const parts: NativeAnswerPart[] = collected && (collected.length > 1 || collected[0]?.content)
    ? collected.map((message) => ({ source: "saved", message, text: message.content ?? "" }))
    : run.summary ? [{ source: "summary", text: run.summary }] : [];
  for (const [key, item] of Object.entries(workPreviews ?? EMPTY_STREAMING_ITEMS)) {
    if (item.content) parts.push({ source: "preview", key, text: item.content });
  }
  let offset = 0;
  const occurrences = new Map<string, number>();
  const positioned = parts.map((part, index) => {
    const separator = index ? NATIVE_ANSWER_SEPARATOR : "";
    offset += separator.length;
    const start = offset;
    offset += part.text.length;
    const item = part.source === "preview" ? part.key : part.source === "saved" ? nativeItemKey(part.message.runtime_metadata) : undefined;
    const identity = item ? `item:${item}` : part.source === "saved" ? `saved:${part.message.seq}` : "summary";
    const occurrence = occurrences.get(identity) ?? 0;
    occurrences.set(identity, occurrence + 1);
    return { ...part, start, separator, id: JSON.stringify([identity, occurrence]) };
  });
  return { text: positioned.map((part) => part.separator + part.text).join(""), parts: positioned };
}

/** Only real persisted seqs may identify a detail read, never snapshot ids. */
export function nativeDetailAnchor(messages: Message[], card: SubagentRun): number | undefined {
  const identity = card.runtime_metadata;
  const history = nativeHistory(messages);
  const status = history.statuses.get(card.card_id)?.find((message) => message.seq !== undefined && message.runtime_metadata?.native &&
    message.runtime_metadata.request_id === identity?.request_id && message.runtime_metadata.attempt_id === identity?.attempt_id);
  if (status) return status.seq;
  if (!identity || !card.native_thread_id || !card.native_turn_id) return undefined;
  return history.work.get(workKey(identity, card.native_thread_id, card.native_turn_id))?.find((message) =>
    message.seq !== undefined && isNativeChild(message.runtime_metadata) &&
    message.runtime_metadata?.native?.root_thread_id === identity.native?.root_thread_id &&
    message.runtime_metadata?.native?.root_turn_id === identity.native?.root_turn_id)?.seq;
}

function observedCard(message: Message): SubagentRun | undefined {
  const identity = readRuntimeMetadata(message.runtime_metadata);
  const source = identity?.native;
  if (!["text", "thinking", "tool_use", "tool_result"].includes(message.type) || !isNativeChild(identity) || !source) return undefined;
  return { card_id: `${OBSERVED_CARD_PREFIX}${workKey(identity, source.thread_id, source.turn_id)}`,
    task_id: source.thread_id, native_thread_id: source.thread_id, native_turn_id: source.turn_id,
    parent_thread_id: source.parent_thread_id, runtime_metadata: identity,
    status: "unknown", name: "", description: "", prompt: "", summary: "", steps: [] };
}

function cardAnchor(card: SubagentRun, anchor?: Message): Message {
  const { runtime_metadata, ...details } = card;
  return { ...anchor, role: "system", type: "native_subagent", content: undefined,
    runtime_metadata: runtime_metadata || anchor?.runtime_metadata, tool_input: details };
}

/** Display filtering never changes the transcript or its global sequence. */
export function nativeChatMessages(messages: Message[], snapshots: SubagentRun[] = [], activeRequest?: string | null): Message[] {
  const cards = new Set<string>();
  const known = new Set(messages.filter((message) => message.type === "native_subagent").map((message) =>
    nativeWorkKey(message.runtime_metadata, message.tool_input?.native_thread_id, message.tool_input?.native_turn_id)));
  const snapshotsByWork = new Map<string | undefined, SubagentRun>();
  for (const run of snapshots) {
    if (!run.card_id) continue;
    const work = nativeWorkKey(run.runtime_metadata, run.native_thread_id, run.native_turn_id);
    if (!snapshotsByWork.has(work)) snapshotsByWork.set(work, run);
  }
  const visible: Message[] = [];
  for (const message of messages) {
    // Snoozing updates the pending question. Its event stays in the history
    // for transcript continuity.
    if (message.type === "question_snoozed") continue;
    if (message.type === "native_subagent") {
      const key = message.tool_input?.card_id;
      if (typeof key !== "string" || cards.has(key)) continue;
      cards.add(key);
      visible.push(message);
    } else if (isNativeChild(message.runtime_metadata)) {
      const source = message.runtime_metadata?.native;
      const snapshot = snapshotsByWork.size
        ? snapshotsByWork.get(nativeWorkKey(message.runtime_metadata, source?.thread_id, source?.turn_id)) : undefined;
      const card = snapshot || observedCard(message);
      if (!card?.card_id || known.has(nativeWorkKey(message.runtime_metadata, card.native_thread_id, card.native_turn_id)) || cards.has(card.card_id)) continue;
      // A window may start after the status observation. Keep the real body
      // reachable without inventing completion or changing persisted rows.
      cards.add(card.card_id);
      visible.push(cardAnchor(card, message));
    } else if (message.type !== "thinking") {
      // MessageBubble hides root reasoning. Exclude its list row as well,
      // otherwise Virtuoso leaves padding for a body that renders nothing.
      visible.push(message);
    }
  }
  for (const run of snapshots) {
    if (run.card_id && run.native_thread_id && !cards.has(run.card_id) &&
        (["running", "unknown"].includes(run.status) || activeRequest && run.runtime_metadata?.request_id === activeRequest)) {
      cards.add(run.card_id);
      visible.push(cardAnchor(run));
    }
  }
  return visible;
}

/** Rebuild a card from collected rows; no separate persistent body or ledger. */
export function nativeCardState(messages: Message[], cardId: string, cached?: SubagentRun): SubagentRun | undefined {
  const history = nativeHistory(messages);
  let card: SubagentRun | undefined;
  let identity: RuntimeMetadata | undefined;
  for (const message of history.statuses.get(cardId) ?? []) {
    const patch = message.tool_input as Partial<SubagentRun>;
    identity = readRuntimeMetadata(message.runtime_metadata) || identity;
    const confirmed = card && ["completed", "failed", "cancelled", "interrupted"].includes(card.status) ? card.status : undefined;
    card = { task_id: "", status: "running", name: "", description: "", prompt: "", summary: "", steps: [],
      ...card, ...Object.fromEntries(Object.entries(patch).filter(([, value]) => value !== null && value !== undefined && value !== "")) };
    if (confirmed) card.status = confirmed;
  }
  if (cached?.card_id === cardId) {
    const confirmed = card && ["completed", "failed", "cancelled", "interrupted"].includes(card.status) ? card.status : undefined;
    card = { ...card, ...cached };
    if (confirmed && cached.status !== "completed") card.status = confirmed;
    identity = readRuntimeMetadata(cached.runtime_metadata) || identity;
  }
  if (!card && cardId.startsWith(OBSERVED_CARD_PREFIX)) {
    for (const message of history.work.get(cardId.slice(OBSERVED_CARD_PREFIX.length)) ?? []) {
      card = observedCard(message);
      if (card) {
        identity = card.runtime_metadata ?? undefined;
        break;
      }
    }
  }
  if (!card) return undefined;
  return { ...card, runtime_metadata: identity };
}

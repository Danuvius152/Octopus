import type { RuntimeMetadata, SubagentRun } from "../api";
import type { Message } from "../stores/sessionStore";
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

export function nativeWorkKey(value: unknown, thread?: unknown, turn?: unknown): string | undefined {
  const identity = readRuntimeMetadata(value);
  return identity && typeof thread === "string" && thread && typeof turn === "string" && turn
    ? JSON.stringify([identity.request_id, identity.attempt_id, thread, turn]) : undefined;
}

/** Only real persisted seqs may identify a detail read, never snapshot ids. */
export function nativeDetailAnchor(messages: Message[], card: SubagentRun): number | undefined {
  const identity = card.runtime_metadata;
  const status = messages.find((message) => message.seq !== undefined && message.type === "native_subagent" &&
    message.tool_input?.card_id === card.card_id && message.runtime_metadata?.native &&
    message.runtime_metadata.request_id === identity?.request_id && message.runtime_metadata.attempt_id === identity?.attempt_id);
  if (status) return status.seq;
  if (!identity || !card.native_thread_id || !card.native_turn_id) return undefined;
  return messages.find((message) => message.seq !== undefined && isNativeChild(message.runtime_metadata) &&
    message.runtime_metadata?.request_id === identity.request_id && message.runtime_metadata?.attempt_id === identity.attempt_id &&
    message.runtime_metadata?.native?.root_thread_id === identity.native?.root_thread_id &&
    message.runtime_metadata?.native?.root_turn_id === identity.native?.root_turn_id &&
    message.runtime_metadata?.native?.thread_id === card.native_thread_id &&
    message.runtime_metadata?.native?.turn_id === card.native_turn_id)?.seq;
}

function observedCard(message: Message): SubagentRun | undefined {
  const identity = readRuntimeMetadata(message.runtime_metadata);
  const source = identity?.native;
  if (!["text", "thinking", "tool_use", "tool_result"].includes(message.type) || !isNativeChild(identity) || !source) return undefined;
  return { card_id: `observed:${nativeWorkKey(identity, source.thread_id, source.turn_id)}`,
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
  const visible: Message[] = [];
  for (const message of messages) {
    if (message.type === "native_subagent") {
      const key = message.tool_input?.card_id;
      if (typeof key !== "string" || cards.has(key)) continue;
      cards.add(key);
      visible.push(message);
    } else if (isNativeChild(message.runtime_metadata)) {
      const source = message.runtime_metadata?.native;
      const snapshot = snapshots.find((run) => run.card_id && nativeWorkKey(run.runtime_metadata, run.native_thread_id, run.native_turn_id) ===
        nativeWorkKey(message.runtime_metadata, source?.thread_id, source?.turn_id));
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
export function nativeCard(messages: Message[], cardId: string, snapshots: SubagentRun[] = []): SubagentRun | undefined {
  let card: SubagentRun | undefined;
  let identity: RuntimeMetadata | undefined;
  for (const message of messages) {
    if (message.type !== "native_subagent" || message.tool_input?.card_id !== cardId) continue;
    const patch = message.tool_input as Partial<SubagentRun>;
    identity = readRuntimeMetadata(message.runtime_metadata) || identity;
    const confirmed = card && ["completed", "failed", "cancelled", "interrupted"].includes(card.status) ? card.status : undefined;
    card = { task_id: "", status: "running", name: "", description: "", prompt: "", summary: "", steps: [],
      ...card, ...Object.fromEntries(Object.entries(patch).filter(([, value]) => value !== null && value !== undefined && value !== "")) };
    if (confirmed) card.status = confirmed;
  }
  const cached = snapshots.find((run) => run.card_id === cardId);
  if (cached) {
    const confirmed = card && ["completed", "failed", "cancelled", "interrupted"].includes(card.status) ? card.status : undefined;
    card = { ...card, ...cached };
    if (confirmed && cached.status !== "completed") card.status = confirmed;
    identity = readRuntimeMetadata(cached.runtime_metadata) || identity;
  }
  if (!card) {
    const observed = messages.find((message) => observedCard(message)?.card_id === cardId);
    if (!observed) return undefined;
    card = observedCard(observed);
    identity = readRuntimeMetadata(observed.runtime_metadata);
  }
  if (!card) return undefined;
  const body = messages.filter((message) => message.type === "text" && message.role === "assistant" &&
    identity && message.runtime_metadata?.request_id === identity.request_id &&
    message.runtime_metadata?.attempt_id === identity.attempt_id &&
    message.runtime_metadata?.native?.thread_id === card.native_thread_id &&
    message.runtime_metadata?.native?.turn_id === card.native_turn_id);
  card.summary = body.map((message) => message.content || "").join("\n\n") || card.summary;
  return { ...card, runtime_metadata: identity };
}

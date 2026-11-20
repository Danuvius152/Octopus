import { createContext } from "react";
import katex from "katex";
import { isNativeChild, nativeAnswer, nativeCardState, nativeChatMessages, nativeItemKey, nativeWorkKey, nativeWorkPreviews } from "../lib/nativeOutput";
import { readAttemptIdentity, readRuntimeMetadata, sameAttempt } from "../lib/toolIdentity";
import { streamingTranscriptSource, type Message, type SubagentRun, type useSessionStore } from "../stores/sessionStore";
import type { RuntimeMetadata } from "../api";
import { clippedMathmlSelector, formulaBodySelector, formulaProbeOptions, projectMarkdownOutput } from "./chatMarkdown";

type SessionState = ReturnType<typeof useSessionStore.getState>;

export interface LiveOutputChange {
  previous: string;
  text: string;
}

export interface SavedOutputChange {
  previous: string;
  message: Message;
}

interface TranscriptTransfer extends SavedOutputChange { displayed: string }

interface TextSpan { start: number; end: number }

interface HelperOutputChange {
  text: string;
  previousByPart: Map<string, string>;
  spans: TextSpan[];
}

export interface PendingTranscriptOutput {
  live: Map<string, LiveOutputChange>;
  saved: Map<number, SavedOutputChange>;
  helpers: Map<string, HelperOutputChange>;
}

export const HelperOutputLayout = createContext<(() => void) | undefined>(undefined);
export const helperAnswerCardWidthClass = "box-border border-[0.7px] px-3";
export const collapsedHelperAnswerClass = "subagent-summary mt-1.5 line-clamp-2 text-[12px] text-gray-800";

const EMPTY_ROWS: Message[] = [];
type SavedHelpers = Record<string, SubagentRun>;
const EMPTY_HELPERS: SavedHelpers = {};

interface HelperOutputOwner { work: string; id: string; native: boolean }
interface ChatProjection {
  activeRequest: string | null | undefined;
  rows: Message[];
}
interface ChatProjectionIndex {
  positions: Map<Message, number>;
  helpers: Map<string, HelperOutputOwner & { index: number }>;
}

// Rendering and paused-output tracking share the projection for the same
// saved inputs, including before React commits a newly saved row.
const projectedWindows = new WeakMap<Message[], WeakMap<SavedHelpers, ChatProjection>>();
const projectionIndexes = new WeakMap<Message[], ChatProjectionIndex>();

export function chatProjection(messages = EMPTY_ROWS, cards = EMPTY_HELPERS, activeRequest?: string | null): Message[] {
  let snapshots = projectedWindows.get(messages);
  if (!snapshots) projectedWindows.set(messages, snapshots = new WeakMap());
  const cached = snapshots.get(cards);
  if (cached && cached.activeRequest === activeRequest) return cached.rows;
  const projection = { activeRequest, rows: nativeChatMessages(messages, Object.values(cards), activeRequest) };
  snapshots.set(cards, projection);
  return projection.rows;
}

export function matchTranscriptTransfers(sid: string, state: SessionState, before: SessionState) {
  const transfers = new Map<string, TranscriptTransfer>();
  if (state.messages[sid] === before.messages[sid]) return transfers;
  const known = new Set(before.messages[sid]?.map((message) => message.seq));
  const oldest = before.oldestSeq[sid] ?? ((before.lastAppliedSeq[sid] ?? -1) + 1);
  for (const message of state.messages[sid] ?? []) {
    if (message.seq === undefined || message.seq < oldest || known.has(message.seq) || message.role !== "assistant" ||
      message.type !== "text" || !message.content || isNativeChild(message.runtime_metadata)) continue;
    const key = nativeItemKey(message.runtime_metadata) ?? "";
    if (transfers.has(key)) continue;
    const remaining = key ? state.streamingItems[sid]?.[key] : state.streamingText[sid];
    if (remaining) continue;
    const attempt = readAttemptIdentity(message);
    const oldRoot = before.streamingText[sid];
    const preview = key ? before.streamingItems[sid]?.[key] : oldRoot;
    if (!preview || (!key && attempt && !sameAttempt(attempt, oldRoot?.attempt))) continue;
    const { text, afterSeq } = streamingTranscriptSource(preview);
    if (!text || message.seq <= (afterSeq ?? -1) || !message.content.startsWith(text)) continue;
    // Rows arrive in sequence order. Only the first compatible completion
    // past the observed preview can consume it, even when another has equal text.
    transfers.set(key, { message, previous: text, displayed: preview.content });
  }
  return transfers;
}

export function legacyHelperWork(toolCallId?: string | null) {
  return toolCallId ? JSON.stringify(["tool", toolCallId]) : undefined;
}

function helperOutputOwner(row: Message): HelperOutputOwner | undefined {
  if (row.type === "native_subagent" && typeof row.tool_input?.card_id === "string") {
    const work = nativeWorkKey(row.runtime_metadata, row.tool_input.native_thread_id, row.tool_input.native_turn_id);
    return work ? { work, id: row.tool_input.card_id, native: true } : undefined;
  }
  const id = row.type === "tool_use" ? readRuntimeMetadata(row.runtime_metadata)?.tool_call_id : undefined;
  return id ? { work: legacyHelperWork(id)!, id, native: false } : undefined;
}

export function chatProjectionIndex(rows: Message[]): ChatProjectionIndex {
  const cached = projectionIndexes.get(rows);
  if (cached) return cached;
  const positions = new Map<Message, number>();
  const helpers: ChatProjectionIndex["helpers"] = new Map();
  rows.forEach((row, index) => {
    if (!positions.has(row)) positions.set(row, index);
    const owner = helperOutputOwner(row);
    if (owner && !helpers.has(owner.work)) helpers.set(owner.work, { ...owner, index });
  });
  const indexed = { positions, helpers };
  projectionIndexes.set(rows, indexed);
  return indexed;
}

export function recordTranscriptOutput(pending: PendingTranscriptOutput, sid: string, state: SessionState, before: SessionState,
  transfers: ReturnType<typeof matchTranscriptTransfers>): boolean {
  const oldRows = before.messages[sid];
  const rows = state.messages[sid];
  const oldRoot = before.streamingText[sid];
  const root = state.streamingText[sid];
  const oldItems = before.streamingItems[sid];
  const items = state.streamingItems[sid];
  const oldCards = before.subagents[sid];
  const cards = state.subagents[sid];
  if (oldRows === rows && oldRoot === root && oldItems === items && oldCards === cards) return false;
  const affectedHelpers = new Set(oldRows !== rows || oldCards !== cards ? pending.helpers.keys() : undefined);
  const newHelperRows = new Set<number>();
  const noteHelper = (metadata: RuntimeMetadata | null | undefined) => {
    const work = nativeWorkKey(metadata, metadata?.native?.thread_id, metadata?.native?.turn_id);
    if (work) affectedHelpers.add(work);
  };
  if (cards !== oldCards) for (const [id, card] of Object.entries(cards ?? {})) {
    if (!card.summary || card.summary === oldCards?.[id]?.summary) continue;
    if (card.card_id) noteHelper(card.runtime_metadata);
    else {
      const work = legacyHelperWork(card.tool_call_id);
      if (work) affectedHelpers.add(work);
    }
  }
  if (oldRows !== rows && rows) {
    const known = new Set(oldRows?.map((message) => message.seq));
    const oldest = before.oldestSeq[sid] ?? ((before.lastAppliedSeq[sid] ?? -1) + 1);
    for (const message of rows) {
      if (message.seq === undefined || message.seq < oldest || known.has(message.seq) || !message.content?.trim()) continue;
      if (message.type === "tool_result" && !isNativeChild(message.runtime_metadata)) {
        pending.saved.set(message.seq, { message, previous: "" });
        continue;
      }
      if (message.role !== "assistant" || message.type !== "text") continue;
      if (isNativeChild(message.runtime_metadata)) {
        newHelperRows.add(message.seq);
        noteHelper(message.runtime_metadata);
        continue;
      }
      const key = nativeItemKey(message.runtime_metadata) ?? "";
      const transfer = transfers.get(key);
      const previous = transfer?.message === message ? pending.live.get(key)?.previous ?? transfer.previous : "";
      if (message.content !== previous) pending.saved.set(message.seq, { message, previous });
    }
  }
  const live = (key: string, previous: string | undefined, text: string | undefined) => {
    if (text === previous) return;
    if (text?.trim()) pending.live.set(key, { previous: pending.live.get(key)?.previous ?? previous ?? "", text });
    else pending.live.delete(key);
  };
  live("", oldRoot?.content, root?.content);
  if (items !== oldItems) {
    for (const [key, item] of Object.entries(items ?? {})) {
      if (!isNativeChild(item.runtime_metadata)) live(key, oldItems?.[key]?.content, item.content);
      else if (oldItems?.[key]?.content !== item.content) noteHelper(item.runtime_metadata);
    }
    for (const [key, item] of Object.entries(oldItems ?? {})) if (!items?.[key]) {
      if (isNativeChild(item.runtime_metadata)) noteHelper(item.runtime_metadata);
      else pending.live.delete(key);
    }
  }
  if (!affectedHelpers.size) return false;
  const { helpers } = chatProjectionIndex(chatProjection(rows, cards, state.dispatchStates[sid]?.active_request_id));
  let helperOutputChanged = false;
  for (const work of affectedHelpers) {
    const owner = helpers.get(work);
    if (!owner) {
      pending.helpers.delete(work);
      continue;
    }
    const { id } = owner;
    const card = owner.native ? nativeCardState(rows ?? EMPTY_ROWS, id, cards?.[id]) : cards?.[id];
    const answer = card && nativeAnswer(card, nativeWorkPreviews(items,
      nativeWorkKey(card.runtime_metadata, card.native_thread_id, card.native_turn_id)), rows ?? EMPTY_ROWS);
    const awaiting = pending.helpers.get(work)?.previousByPart;
    const previousByPart = new Map<string, string>();
    const spans: TextSpan[] = [];
    for (const part of answer?.parts ?? []) {
      let key: string | undefined;
      let previous: string | undefined;
      if (part.source === "preview") {
        key = `preview:${part.key}`;
        previous = awaiting?.get(key) ?? oldItems?.[part.key]?.content ?? "";
      } else if (part.source === "saved" && part.message.seq !== undefined) {
        key = `saved:${part.message.seq}`;
        previous = awaiting?.get(key);
        if (newHelperRows.has(part.message.seq)) {
          const item = nativeItemKey(part.message.runtime_metadata);
          const preview = item && oldItems?.[item];
          previous ??= item ? awaiting?.get(`preview:${item}`) ?? (preview ? streamingTranscriptSource(preview).text : "") : "";
        }
      } else if (part.source === "summary") {
        key = "summary";
        previous = awaiting?.get(key) ?? oldCards?.[id]?.summary ?? "";
      }
      if (key && previous !== undefined) {
        const { start, end } = changedCharacters(part.text, previous);
        if (part.text.slice(start, end).trim()) {
          previousByPart.set(key, previous);
          spans.push({ start: part.start + start, end: part.start + end });
        }
      }
    }
    if (answer && spans.length) {
      pending.helpers.set(work, { previousByPart, spans, text: answer.text });
      helperOutputChanged = true;
    } else pending.helpers.delete(work);
  }
  return helperOutputChanged;
}

export function scrollportBounds(viewport: HTMLElement) {
  const zoom = viewport.currentCSSZoom ?? 1;
  const top = viewport.getBoundingClientRect().top + viewport.clientTop * zoom;
  return { top, bottom: top + viewport.clientHeight * zoom };
}

function changedCharacters(text: string, previous: string): TextSpan {
  let start = 0;
  while (start < text.length && text[start] === previous[start]) start++;
  let end = text.length;
  let oldEnd = previous.length;
  while (end > start && oldEnd > start && text[end - 1] === previous[oldEnd - 1]) { end--; oldEnd--; }
  return { start, end };
}

function hasVisibleColor(value: string) {
  return !!value && value !== "none" && value !== "transparent" && !/^rgba\(.*,\s*0(?:\.0+)?\)$/.test(value);
}

function paintsMathGraphic(element: Element, style: CSSStyleDeclaration) {
  if (element.localName === "svg") {
    return (hasVisibleColor(style.fill) && Number(style.fillOpacity) > 0) ||
      (hasVisibleColor(style.stroke) && Number(style.strokeOpacity) > 0 && Number.parseFloat(style.strokeWidth) > 0);
  }
  return hasVisibleColor(style.backgroundColor) || ["top", "right", "bottom", "left"].some((side) =>
    Number.parseFloat(style.getPropertyValue(`border-${side}-width`)) > 0 &&
    hasVisibleColor(style.getPropertyValue(`border-${side}-color`)));
}

function formulaPaintOutside(formula: Element, port?: ReturnType<typeof scrollportBounds>) {
  const outside = (box: DOMRect) => box.width > 0 && box.height > 0 &&
    (!port || box.top < port.top - 1 || box.bottom > port.bottom + 1);
  const range = formula.ownerDocument.createRange();
  // Look at computed styles here so a class-based border and an inline
  // border are measured the same way. MathML is outside this visual subtree.
  for (const element of [formula, ...formula.querySelectorAll("*")]) {
    const style = getComputedStyle(element);
    if (style.visibility !== "visible" || Number(style.opacity) === 0) continue;
    if (paintsMathGraphic(element, style) && outside(element.getBoundingClientRect())) return true;
    if (!hasVisibleColor(style.color)) continue;
    for (const node of element.childNodes) {
      if (node.nodeType !== Node.TEXT_NODE || !/[^\s\u200b]/.test(node.textContent ?? "")) continue;
      range.selectNodeContents(node);
      if (Array.from(range.getClientRects()).some(outside)) return true;
    }
  }
  return false;
}

/** Measure the changed characters themselves. Row spacing, an unchanged
 * portion of a preview and text clipped by a closed card cannot signal output. */
export function readableSpansOutside(body: HTMLElement, spans: TextSpan[], port?: ReturnType<typeof scrollportBounds>,
  clip?: DOMRect, images?: true | number[]) {
  const outside = (box: DOMRect) => {
    const top = Math.max(box.top, clip?.top ?? -Infinity);
    const bottom = Math.min(box.bottom, clip?.bottom ?? Infinity);
    return box.width > 0 && bottom - top > 1 && (!port || top < port.top - 1 || bottom > port.bottom + 1);
  };
  const range = document.createRange();
  for (const { start, end } of spans) {
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    let offset = 0;
    for (let node = walker.nextNode(); node && offset < end; node = walker.nextNode()) {
      const value = node.textContent ?? "";
      const first = Math.max(0, start - offset);
      const changed = value.slice(first, Math.max(0, end - offset));
      offset += value.length;
      if (!changed.trim() || node.parentElement?.closest(clippedMathmlSelector)) continue;
      range.setStart(node, first + changed.length - changed.trimStart().length);
      range.setEnd(node, first + changed.trimEnd().length);
      if (Array.from(range.getClientRects()).some(outside)) return true;
    }
  }
  return !!images && Array.from(body.querySelectorAll("img")).some((img, index) =>
    (images === true || images.includes(index)) && outside(img.getBoundingClientRect()));
}

export function collapsedHelperHasChangedText(change: HelperOutputChange, row: HTMLElement) {
  if (!row.clientWidth) return false;
  const probe = document.createElement("div");
  probe.className = helperAnswerCardWidthClass;
  // Measure wrapping under the same width and CSS used by a newly mounted
  // collapsed card. A clipped suffix cannot certify any offscreen text.
  Object.assign(probe.style, { position: "fixed", inset: "0 auto auto -10000px", visibility: "hidden",
    width: `${row.clientWidth}px`, fontFamily: getComputedStyle(row).fontFamily });
  const summary = document.createElement("p");
  summary.className = collapsedHelperAnswerClass;
  summary.textContent = change.text;
  probe.append(summary);
  row.ownerDocument.body.append(probe);
  try {
    return readableSpansOutside(summary, change.spans, undefined, summary.getBoundingClientRect());
  } finally {
    probe.remove();
  }
}

export function readableChangeOutside(body: HTMLElement, previous: string, port: ReturnType<typeof scrollportBounds>) {
  return readableSpansOutside(body, [changedCharacters(body.textContent ?? "", previous)], port, undefined, previous ? undefined : true);
}

function savedMarkdownChange(change: SavedOutputChange) {
  const source = change.message.content ?? "";
  return projectMarkdownOutput(source, change.previous ? changedCharacters(source, change.previous) : undefined);
}

export function savedOutputHasBody(change: SavedOutputChange, viewport: HTMLElement) {
  if (change.message.type === "tool_result") return true;
  const { spans, images, mathOutput } = savedMarkdownChange(change);
  if (spans.length || images.length) return true;
  if (!mathOutput.length) return false;
  // The virtual range proves the row is offscreen. Render its formulas
  // outside the transcript to check whether they occupy any painted area.
  const doc = viewport.ownerDocument;
  const probe = doc.createElement("div");
  probe.className = "msg-content markdown text-[14px] text-gray-900";
  probe.setAttribute("aria-hidden", "true");
  Object.assign(probe.style, { position: "fixed", inset: "0 auto auto -10000px", opacity: "0", pointerEvents: "none",
    width: `${viewport.clientWidth}px` });
  doc.body.append(probe);
  try {
    return mathOutput.some(({ source, displayMode }) => {
      katex.render(source, probe, { ...formulaProbeOptions, displayMode });
      const formula = probe.querySelector(formulaBodySelector);
      return !!formula && formulaPaintOutside(formula);
    });
  } finally {
    probe.remove();
  }
}

export function savedOutputOutside(row: HTMLElement, change: SavedOutputChange, port: ReturnType<typeof scrollportBounds>) {
  const selector = change.message.type === "tool_result" ? ".tool-preview" : ".msg-content";
  const body = row.querySelector<HTMLElement>(selector);
  if (!body) return false;
  if (!change.previous && !body.querySelector(formulaBodySelector)) return readableChangeOutside(body, "", port);
  const projected = savedMarkdownChange(change);
  const text = body.textContent ?? "";
  if (!text.endsWith(projected.text)) return false;
  const offset = text.length - projected.text.length;
  if (readableSpansOutside(body, projected.spans.map(({ start, end }) => ({ start: start + offset, end: end + offset })),
    port, undefined, projected.images)) return true;
  const formulas = body.querySelectorAll(formulaBodySelector);
  return projected.mathOutput.some(({ index }) => !!formulas[index] && formulaPaintOutside(formulas[index], port));
}

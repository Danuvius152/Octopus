import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { Virtuoso, type ListProps, type VirtuosoHandle } from "react-virtuoso";
import { hasEmptyTranscript, hasTranscriptBaseline, observeAcceptedText, useSessionStore, type Message, type PendingQuestion,
  type SessionInfo, type StreamingItem, type SubagentRun } from "../stores/sessionStore";
import { observeOlderMessages } from "../lib/transcript";
import { observeSession } from "../lib/hydrateSession";
import { matchesPendingQuestion } from "../lib/questionPresentation";
import { isNativeChild, nativeChatMessages } from "../lib/nativeOutput";
import type { QuestionCommandTarget, SubmissionReply } from "../lib/submission";
import { Button } from "./ui/button";
import { MessageBubble } from "./MessageBubble";
import { MessageDetails, SavedMessageRecord } from "./MessageDetails";
import { MessageTime } from "./MessageTime";
import { QuestionPrompt, type AnswerPayload } from "./QuestionPrompt";
import { TranscriptIntegrityNotice } from "./TranscriptIntegrityNotice";
import { HelperOutputLayout, collapsedHelperHasChangedText, helperOutputWork, matchTranscriptTransfers,
  readableChangeOutside, readableSpansOutside, recordTranscriptOutput,
  savedOutputHasBody, savedOutputOutside, scrollportBounds,
  type PendingTranscriptOutput } from "./chatOutputVisibility";
import { projectMarkdownOutput } from "./chatMarkdown";

const EMPTY_MESSAGES: Message[] = [];
const EMPTY_STREAMING_ITEMS: Record<string, StreamingItem> = {};
const EMPTY_SUBAGENTS: Record<string, SubagentRun> = {};
// An avatar-height row and its list spacing are a better unknown than the
// height of whichever conversation card Virtuoso happens to probe first.
const DEFAULT_CHAT_ROW_HEIGHT = 48;

/** A nested output pane can consume navigation or block scroll chaining.
 * Downward input at the physical boundary still belongs to the main list. */
function canScrollChat(viewport: HTMLElement | null, target: EventTarget | null, up: boolean, from = viewport?.scrollTop ?? 0): boolean {
  if (!viewport?.clientHeight || up && from <= 0) return false;
  let element = target instanceof Element ? target : null;
  // The preparation surface overlays this same list while its layout stays
  // measurable. Navigation there has the same owner as navigation on the list.
  if (element?.closest("[data-chat-initial-surface]")?.parentElement?.querySelector(".chat-messages") === viewport) element = viewport;
  while (element && element !== viewport) {
    const style = getComputedStyle(element);
    if (/^(auto|scroll)$/.test(style.overflowY) && element.scrollHeight > element.clientHeight
      && ((up ? element.scrollTop > 0 : element.scrollHeight - element.clientHeight - element.scrollTop > 0)
        || style.overscrollBehaviorY !== "auto")) return false;
    element = element.parentElement;
  }
  return element === viewport;
}

function sampleScroll(viewport: HTMLElement) {
  return { top: viewport.scrollTop, maximum: Math.max(0, viewport.scrollHeight - viewport.clientHeight) };
}

function transcriptSelection(viewport: HTMLElement | null): Selection | null {
  const selection = viewport?.ownerDocument.getSelection();
  if (!viewport || !selection || selection.isCollapsed ||
    !viewport.contains(selection.anchorNode) || !viewport.contains(selection.focusNode)) return null;
  const selector = "[data-message-key], [data-live-output]";
  const inBody = (node: Node | null) => {
    const element = node instanceof Element ? node : node?.parentElement;
    return element?.closest(selector);
  };
  if (!selection.toString()) return null;
  if (inBody(selection.anchorNode) || inBody(selection.focusNode)) return selection;
  const range = selection.getRangeAt(0);
  return Array.from(viewport.querySelectorAll(selector)).some((body) => range.intersectsNode(body)) ? selection : null;
}

function changesScrollLayout(viewport: HTMLElement, records: MutationRecord[] = []) {
  return records.some(({ target, type }) => {
    let element = target instanceof Element ? target : target.parentElement;
    // An attribute on the element itself can change its participation in
    // layout, including making it hidden. Changes inside a hidden subtree cannot.
    while (element && element !== viewport) {
      const parent = element.parentElement;
      if (parent instanceof HTMLDetailsElement && !parent.open && element !== parent.querySelector(":scope > summary")) return false;
      if ((type !== "attributes" || element !== target) && getComputedStyle(element).display === "none") return false;
      element = parent;
    }
    return element === viewport;
  });
}

function atLoadedStart(viewport: HTMLElement, first: Message | undefined): boolean {
  if (viewport.scrollTop <= 4) return true;
  if (!first || viewport.scrollTop > viewport.clientHeight) return false;
  const row = viewport.querySelector<HTMLElement>("[data-message-key]");
  if (row?.dataset.messageKey !== chatMessageKey(first)) return false;
  const top = row.getBoundingClientRect().top - viewport.getBoundingClientRect().top;
  return top >= -2 && top < viewport.clientHeight;
}

interface ReadingAnchor {
  key: string;
  offset: number;
  textOffset?: true;
  selected?: { range: Range; restored?: Range; projected: ReturnType<typeof projectMarkdownOutput>; offset: number; backward: boolean };
}

function visibleReadingRow(viewport: HTMLElement) {
  const bounds = viewport.getBoundingClientRect();
  return Array.from(viewport.querySelectorAll<HTMLElement>("[data-message-key]")).find((element) => {
    const rectangle = element.getBoundingClientRect();
    return rectangle.bottom > bounds.top && rectangle.top < bounds.bottom;
  });
}

function captureReadingAnchor(viewport: HTMLElement): ReadingAnchor | null {
  const row = visibleReadingRow(viewport);
  return row ? { key: row.dataset.messageKey!, offset: row.getBoundingClientRect().top - viewport.getBoundingClientRect().top } : null;
}

function captureTransferredSelection(body: HTMLElement, selection: Selection, viewportTop: number) {
  const range = selection.getRangeAt(0);
  if (!body.contains(range.startContainer) || !body.contains(range.endContainer)) return;
  const text = range.toString();
  const top = Array.from(range.getClientRects())[0]?.top;
  if (!text.trim() || top === undefined) return;
  const preceding = document.createRange();
  preceding.selectNodeContents(body);
  preceding.setEnd(range.startContainer, range.startOffset);
  const start = preceding.toString().length;
  const backward = selection.anchorNode === range.endContainer && selection.anchorOffset === range.endOffset;
  return { range, source: { start, end: start + text.length }, offset: top - viewportTop, backward };
}

function transferredSelection(row: HTMLElement, selected: NonNullable<ReadingAnchor["selected"]>): Range | null {
  const body = row.querySelector(".msg-content");
  const text = body?.textContent;
  const { spans, text: rendered } = selected.projected;
  if (!body || !text?.endsWith(rendered) || !spans.length) return null;
  const prefix = text.length - rendered.length;
  const start = prefix + spans[0].start;
  const end = prefix + spans[spans.length - 1].end;
  const range = document.createRange();
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  let offset = 0;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const next = offset + (node.textContent?.length ?? 0);
    if (offset <= start && start < next) range.setStart(node, start - offset);
    if (offset < end && end <= next) { range.setEnd(node, end - offset); return range; }
    offset = next;
  }
  return null;
}

function transcriptTextTop(row: HTMLElement): number {
  const content = row.querySelector(".msg-content");
  const text = content && document.createTreeWalker(content, NodeFilter.SHOW_TEXT).nextNode();
  if (!text?.textContent) return row.getBoundingClientRect().top;
  const range = document.createRange();
  range.setStart(text, 0);
  range.setEnd(text, 1);
  return range.getBoundingClientRect().top;
}

// A saved row can keep its key while the accepted content changes. Associate
// the rendered node with the content React actually committed.
const renderedMessages = new WeakMap<HTMLElement, Message>();
const renderedFooters = new WeakMap<HTMLElement, {
  sessionId: string | null | undefined;
  running: boolean | undefined;
  text: string;
  items: Record<string, StreamingItem>;
}>();

const INITIAL_TAIL_TOLERANCE = 1;

function measureInitialTail(viewport: HTMLElement, last: Message | undefined, sessionId: string) {
  // Virtuoso sizes this box to the scrollport, including when a native
  // horizontal scrollbar takes space. Its height survives scrolling.
  const layout = viewport.querySelector(':scope > [data-viewport-type="element"]');
  if (!layout) return "waiting";
  const zoom = viewport.currentCSSZoom ?? 1;
  const top = viewport.getBoundingClientRect().top + viewport.clientTop * zoom;
  const bottom = top + layout.getBoundingClientRect().height;
  const footer = viewport.querySelector<HTMLElement>("[data-chat-footer]");
  const footerBox = footer?.parentElement?.getBoundingClientRect();
  const row = last && [...viewport.querySelectorAll<HTMLElement>("[data-message-key]")].at(-1);
  const rowBox = row && renderedMessages.get(row) === last ? row.parentElement?.getBoundingClientRect() : undefined;
  const tail = footerBox?.height ? footerBox : rowBox;
  // The wrapper includes tail spacing and, for the Footer, every live item.
  // Use the rounded scroll range only while the actual tail lacks geometry.
  const gap = tail?.height ? tail.bottom - bottom : (viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop) * zoom;
  if (gap > INITIAL_TAIL_TOLERANCE) return "position";

  const state = useSessionStore.getState();
  const session = state.sessions.find((row) => row.id === sessionId) ??
    state.archivedSessions.find((row) => row.id === sessionId);
  const live = footer && renderedFooters.get(footer);
  if (session?.status === "running") {
    if (!live?.running || live.sessionId !== sessionId ||
      live.text !== (state.streamingText[sessionId]?.content ?? "") ||
      live.items !== (state.streamingItems[sessionId] ?? EMPTY_STREAMING_ITEMS)) return "waiting";
  } else if (live?.running) return "waiting";
  const inView = (box: DOMRect) => box.height > 0 && box.bottom > top && box.bottom <= bottom + INITIAL_TAIL_TOLERANCE;
  if (footerBox?.height) {
    if (!inView(footerBox)) return "waiting";
    if (!last) return "readable";
    // A full-screen Footer can be readable with every saved row virtualized.
    // The list still needs to have committed its space before we expose it.
    if (footerBox.top <= top) {
      return viewport.querySelector("[data-chat-rows]")?.getBoundingClientRect().height ? "readable" : "waiting";
    }
  } else if (footer?.childElementCount) return "waiting";
  return !last || rowBox && inView(rowBox) ? "readable" : "waiting";
}

function scrollToTail(viewport: HTMLElement) {
  // The browser clamps this request to the actual range, including scrollbars
  // and zoom. Virtuoso observes the resulting native scroll event.
  viewport.scrollTo({ top: viewport.scrollHeight, behavior: "instant" });
}

function ChatRows(props: ListProps) {
  return <div {...props} data-chat-rows />;
}

const EMPTY_QUESTIONS: PendingQuestion[] = [];

interface ChatFooterContext {
  sessionId: string | null;
  agentName?: string;
  agentAvatar?: string | null;
  running: boolean;
  onLayout: () => void;
}

// The live Footer owns its delta subscription. Virtuoso and the saved rows
// keep their inputs while an existing preview gains text.
function ChatFooter({ context }: { context?: ChatFooterContext }) {
  const sid = context?.sessionId;
  const text = useSessionStore((state) => sid ? state.streamingText[sid]?.content ?? "" : "");
  const items = useSessionStore((state) => sid ? state.streamingItems[sid] ?? EMPTY_STREAMING_ITEMS : EMPTY_STREAMING_ITEMS);
  const rootItems = useMemo(() => Object.entries(items).filter(([, item]) => !isNativeChild(item.runtime_metadata)), [items]);
  const layout = context?.onLayout;
  const running = context?.running;
  const footer = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (footer.current) renderedFooters.set(footer.current, { sessionId: sid, running, text, items });
    layout?.();
  }, [layout, sid, running, text, items]);

  return <div ref={footer} data-chat-footer>{running && (text || rootItems.length ? (
    <div className="msg-streaming" data-testid="streaming-text">
      {text && <div data-live-output="" className="px-4 pb-3">
        <MessageBubble message={{ role: "assistant", type: "text", content: text }}
          sessionId={sid ?? ""} agentName={context?.agentName} agentAvatar={context?.agentAvatar ?? undefined} plain />
      </div>}
      {rootItems.map(([key, item]) => <div key={key} data-live-output={key} className="px-4 pb-3">
        <MessageBubble message={{ role: "assistant", type: "text", ...item }}
          sessionId={sid ?? ""} agentName={context?.agentName} agentAvatar={context?.agentAvatar ?? undefined} plain />
      </div>)}
    </div>
  ) : (
    <div className="msg msg-loading flex items-center gap-1.5 px-3 py-2">
      <span className="loading-dot inline-block size-2 rounded-full bg-muted-foreground/60 animate-pulse [animation-delay:-0.32s]" />
      <span className="loading-dot inline-block size-2 rounded-full bg-muted-foreground/60 animate-pulse [animation-delay:-0.16s]" />
      <span className="loading-dot inline-block size-2 rounded-full bg-muted-foreground/60 animate-pulse" />
    </div>
  ))}</div>;
}
// Local notices have no persisted seq. Their occurrence identity follows the
// immutable store message through prepends, without retaining another transcript.
const localMessageKeys = new WeakMap<Message, string>();
let localMessageSerial = 0;

function chatMessageKey(message: Message): string {
  if (message.type === "native_subagent") {
    const source = message.runtime_metadata;
    return source && message.tool_input?.native_thread_id && message.tool_input?.native_turn_id
      ? JSON.stringify(["child", source.request_id, source.attempt_id, message.tool_input.native_thread_id, message.tool_input.native_turn_id])
      : `child:${message.tool_input?.card_id}`;
  }
  if (message.seq !== undefined) return `message:${message.seq}`;
  let key = localMessageKeys.get(message);
  if (!key) { key = `local-message:${++localMessageSerial}`; localMessageKeys.set(message, key); }
  return key;
}

interface ChatViewportView {
  sessionId: string | null;
  account: number;
  window: symbol | undefined;
}
interface ViewportScope {
  view: ChatViewportView;
  viewport: HTMLElement;
}
interface ReadingHandoff {
  view: ChatViewportView;
  anchor: ReadingAnchor | null;
  physical?: { viewport: HTMLElement; top: number; restored?: true };
  scrollbarPosition?: { viewport: HTMLElement; top: number };
}
function pendingReadingHandoff(handoff: ReadingHandoff | null, view: ChatViewportView, viewport: HTMLElement) {
  return handoff !== null && (handoff.view !== view || !!handoff.scrollbarPosition ||
    (handoff.physical?.viewport ?? viewport) !== viewport);
}
function restoredReadingAt(handoff: ReadingHandoff | null, view: ChatViewportView, viewport: HTMLElement) {
  const physical = handoff?.physical;
  if (handoff?.view === view && physical?.restored && physical.viewport === viewport &&
    physical.top === viewport.scrollTop) return physical;
}
function movedFromScrollbarPosition(handoff: ReadingHandoff, viewport: HTMLElement) {
  const guard = handoff.scrollbarPosition;
  return guard?.viewport === viewport && guard.top !== viewport.scrollTop &&
    guard.top <= viewport.scrollHeight - viewport.clientHeight;
}
type ViewportPosition = ViewportScope & {
  frame: number | null;
} & ({ kind: "initial" } |
  { kind: "restore"; handoff: ReadingHandoff; limit: number } |
  ({ kind: "history"; physicalTop: number; expectedTop?: number; alignedHeight?: number; alignOnMount?: true } & ReadingAnchor));
function alignReadingAnchor(operation: Extract<ViewportPosition, { kind: "history" }>, element: HTMLElement,
  writePosition: (viewport: HTMLElement, view: ChatViewportView, write: () => void) => boolean) {
  const { viewport } = operation;
  const movedSelection = operation.selected && transferredSelection(element, operation.selected);
  const selectedTop = movedSelection && Array.from(movedSelection.getClientRects())[0]?.top;
  const top = typeof selectedTop === "number" ? selectedTop : operation.textOffset ? transcriptTextTop(element) : element.getBoundingClientRect().top;
  const offset = typeof selectedTop === "number" ? operation.selected!.offset : operation.offset;
  const delta = top - viewport.getBoundingClientRect().top - offset;
  const before = viewport.scrollTop;
  const aligned = Math.abs(delta) <= 1;
  if (!aligned && !writePosition(viewport, operation.view, () => { viewport.scrollTop = before + delta; })) return "superseded";
  const selection = movedSelection && viewport.ownerDocument.getSelection();
  // Removing the preview collapses the original Range in place. A later
  // selection or an explicit clear has already taken ownership away from it.
  if (movedSelection && selection && selection.rangeCount && selection.getRangeAt(0) === operation.selected?.range) {
    if (operation.selected.backward) {
      selection.setBaseAndExtent(movedSelection.endContainer, movedSelection.endOffset, movedSelection.startContainer, movedSelection.startOffset);
    } else {
      selection.removeAllRanges();
      selection.addRange(movedSelection);
    }
    operation.selected.restored = selection.getRangeAt(0);
  }
  if (aligned) return "aligned";
  operation.alignedHeight = undefined;
  if (operation.expectedTop !== undefined) operation.expectedTop = viewport.scrollTop;
  return viewport.scrollTop !== before ? "moved" : "clamped";
}

interface InitialPresentation {
  phase: "preparing" | "revealing" | "ready" | "reading" | "incomplete";
  remaining: number;
  since: number | null;
  deadline: number | null;
  error?: string;
  timedOut?: boolean;
  timeoutHint?: boolean;
  recovery?: { pending: boolean; release: () => void };
  automaticReturn?: boolean;
}
function newPresentation(phase: "preparing" | "ready" = "preparing"): InitialPresentation {
  return { phase, remaining: 2000, since: null, deadline: null };
}
function presentationSnapshot(owner: InitialPresentation, following: boolean) {
  return {
    owner,
    phase: owner.phase,
    error: owner.error,
    showTimeoutNotice: !!owner.timedOut && !following,
    timeoutHint: !!owner.timeoutHint,
    offerTimeoutRecovery: !!owner.timeoutHint && !!owner.recovery,
  };
}
function pausePreparation(owner: InitialPresentation) {
  if (owner.deadline !== null) window.clearTimeout(owner.deadline);
  owner.deadline = null;
  if (owner.since !== null) owner.remaining = Math.max(0, owner.remaining - Math.max(0, performance.now() - owner.since));
  owner.since = null;
}
interface NavigationContact {
  sessionId: string | null;
  account: number;
  source: "pointer" | "touch" | "scrollbar";
  id: number;
  y: number;
  scrollbarStart?: number;
  invalid: boolean;
  pointers: Map<number, string>;
  touch: { id: number; y: number; multiple: boolean } | null;
}

interface ScrollNavigation extends ViewportScope {
  top: number;
  maximum: number;
  until: number;
  direction: "idle" | "up" | "down";
  contact?: NavigationContact;
  upward?: "scrolled" | "history";
  downward?: "scrolled" | "bottom";
  wasAwayFromTop?: boolean;
  writingPosition?: boolean;
}

interface PendingWheel extends ViewportScope {
  event: WheelEvent;
  from: number;
  maximum: number;
  atBottom: boolean;
  timer: number;
}

export interface TranscriptPreparation {
  current: () => boolean;
  committing: boolean;
  commit: () => void;
  fail: (message: string) => void;
}

interface Props {
  sessionId: string;
  session?: SessionInfo;
  onFork?: (seq: number) => void;
  onRestoreInput?: (message: Message) => void;
  restoreInputDisabled: boolean;
  preparation?: TranscriptPreparation;
  minimumViewportHeight?: string;
  answerQuestion: (sessionId: string, questionId: string, answers: AnswerPayload[],
    target?: QuestionCommandTarget) => void | Promise<SubmissionReply>;
  snoozeQuestion?: (sessionId: string, questionId: string, target: QuestionCommandTarget) => void | Promise<SubmissionReply>;
}

export function ChatTranscript({ sessionId: activeSessionId, session: activeSession, answerQuestion,
  snoozeQuestion, onFork, onRestoreInput, restoreInputDisabled, preparation, minimumViewportHeight }: Props) {
  const transcriptBaseline = useSessionStore((s) => hasTranscriptBaseline(s, activeSessionId));
  const empty = useSessionStore((s) => !!activeSession && hasEmptyTranscript(s, activeSession));
  const transcriptReadError = useSessionStore((s) => s.transcriptReadErrors[activeSessionId]);
  const transcriptDamaged = activeSession?.transcript_integrity === "duplicate_seq";
  const messages = useSessionStore((s) => s.messages[activeSessionId] ?? EMPTY_MESSAGES);
  const subagentSnapshots = useSessionStore((s) => s.subagents[activeSessionId] ?? EMPTY_SUBAGENTS);
  const activeRequestId = useSessionStore((s) => s.dispatchStates[activeSessionId]?.active_request_id);
  const chatMessages = useMemo(() => nativeChatMessages(messages, Object.values(subagentSnapshots), activeRequestId),
    [messages, subagentSnapshots, activeRequestId]);
  const pendingQuestions = useSessionStore((s) => s.pendingQuestions[activeSessionId] ?? EMPTY_QUESTIONS);
  const activeAgent = useSessionStore((s) => s.agents.find((agent) => agent.id === activeSession?.agent_id));
  const agentLabel = activeAgent?.name || "Assistant";
  const isRunning = activeSession?.status === "running";
  const staging = useRef(preparation);
  useLayoutEffect(() => { staging.current = preparation; }, [preparation]);
  const committed = useRef({ messages, subagentSnapshots, activeRequestId, pendingQuestions });
  // A prepend can also move or regroup child cards. Preserve the actual
  // visible identity and offset, independently of total row counts.
  const virtuoso = useRef<VirtuosoHandle>(null);
  const scroller = useRef<HTMLElement | null>(null);
  // A local observation of the existing identities survives React batching:
  // leaving A retires its work even when B→A completes before the next commit.
  const [viewportView, setViewportView] = useState<ChatViewportView>(() => {
    const state = useSessionStore.getState();
    return { sessionId: activeSessionId, account: state.accountGeneration, window: state.messageWindows[activeSessionId] };
  });
  const currentView = useRef<ChatViewportView | null>(null);
  // The opening owns reading intent; a window/node replacement only hands off
  // its optional position. A native scrollbar contact can outlive the commit.
  const readingHandoff = useRef<ReadingHandoff | null>(null);
  const [readingBoundary, setReadingBoundary] = useState(false);
  const [scrolledIntoHistory, setScrolledIntoHistory] = useState(false);
  const [outputOutside, setOutputOutside] = useState(false);
  const pausedOutput = useRef<PendingTranscriptOutput | null>({ saved: new Map(), live: new Map(), helpers: new Map() });
  const resetPausedOutput = useCallback((keepOffer = false) => {
    const pending = pausedOutput.current;
    if (pending) { pending.live.clear(); pending.saved.clear(); pending.helpers.clear(); }
    else if (!keepOffer) {
      pausedOutput.current = { saved: new Map(), live: new Map(), helpers: new Map() };
      setOutputOutside(false);
    }
  }, []);
  // Input retires follow intent before queued layout work can overwrite it.
  // Position alone cannot distinguish native navigation from virtual layout.
  const following = useRef(true);
  const initialPresentation = useRef<InitialPresentation>(newPresentation(!preparation && empty ? "ready" : "preparing"));
  const [presentation, setPresentation] = useState(() => presentationSnapshot(initialPresentation.current, following.current));
  const publishedPresentation = useRef(presentation);
  const publishPresentation = useCallback((owner: InitialPresentation, phase: InitialPresentation["phase"]) => {
    if (owner !== initialPresentation.current) return;
    owner.phase = phase;
    const next = presentationSnapshot(owner, following.current);
    // Compare against the last publication even when React batches input
    // and layout callbacks before committing the next view.
    const previous = publishedPresentation.current;
    if (previous.owner === next.owner && previous.phase === next.phase && previous.error === next.error &&
      previous.showTimeoutNotice === next.showTimeoutNotice && previous.timeoutHint === next.timeoutHint &&
      previous.offerTimeoutRecovery === next.offerTimeoutRecovery) return;
    publishedPresentation.current = next;
    setPresentation(next);
  }, []);
  const position = useRef<ViewportPosition | null>(null);
  const [transferOverscan, setTransferOverscan] = useState(0);
  const projectedMessages = useRef(chatMessages);
  const followFrame = useRef<(ViewportScope & { frame: number; time: number }) | null>(null);
  const reducedMotion = useRef<MediaQueryList | null>(null);
  const viewportObserver = useRef<ResizeObserver | null>(null);
  const contentObserver = useRef<MutationObserver | null>(null);
  const lastScroll = useRef({ top: 0, maximum: 0 });
  // Native scroll needs direction from current input. After contact release,
  // the evidence ends at scrollend or 500ms without progress.
  const scrollNavigation = useRef<ScrollNavigation | null>(null);
  const navigationContact = useRef<NavigationContact | null>(null);
  const pendingWheel = useRef<PendingWheel | null>(null);
  const viewportInputHeld = useCallback(() => !!(navigationContact.current || pendingWheel.current), []);
  const selectedViewport = useRef<HTMLElement | null>(null);
  const selectionPaused = useCallback((viewport = scroller.current) => {
    if (!transcriptSelection(viewport)) return false;
    selectedViewport.current = viewport;
    return true;
  }, []);
  const retireSelection = useCallback((viewport: HTMLElement | null) => {
    selectedViewport.current = null;
    transcriptSelection(viewport)?.removeAllRanges();
  }, []);
  const retireWheel = useCallback(() => {
    if (!pendingWheel.current) return;
    window.clearTimeout(pendingWheel.current.timer);
    pendingWheel.current = null;
  }, []);
  const focusReading = useRef<{ owner: InitialPresentation; source: Element } | null>(null);
  const cancelFollow = useCallback((view?: ChatViewportView) => {
    const work = followFrame.current;
    if (!work || view && work.view !== view) return;
    cancelAnimationFrame(work.frame);
    followFrame.current = null;
  }, []);
  const suspendViewportMotion = useCallback(() => {
    cancelFollow();
    const operation = position.current;
    if (operation?.frame != null) {
      cancelAnimationFrame(operation.frame);
      operation.frame = null;
    }
  }, [cancelFollow]);
  const retirePosition = useCallback((operation = position.current) => {
    if (operation && operation.frame !== null) cancelAnimationFrame(operation.frame);
    if (operation) operation.frame = null;
    if (position.current === operation) {
      if (operation?.kind === "history" && operation.textOffset) setTransferOverscan(0);
      position.current = null;
    }
  }, []);
  const currentViewportView = useCallback((view: ChatViewportView) => {
    const state = useSessionStore.getState();
    const preparation = staging.current;
    return currentView.current === view && state.mainView === "chat" && state.accountGeneration === view.account &&
      (state.activeSessionId === view.sessionId && (!preparation || preparation.committing) || preparation?.current() === true) &&
      (view.sessionId ? state.messageWindows[view.sessionId] : undefined) === view.window;
  }, []);
  const currentPosition = useCallback((operation: ViewportPosition) => {
    return position.current === operation && scroller.current === operation.viewport &&
      currentViewportView(operation.view);
  }, [currentViewportView]);
  const measurePausedOutput = useCallback(() => {
    const pending = pausedOutput.current;
    const view = currentView.current;
    const viewport = scroller.current;
    if (!pending || (!pending.live.size && !pending.saved.size && !pending.helpers.size) || !view?.sessionId ||
      !viewport?.clientHeight || document.hidden || (position.current && position.current.kind !== "initial") ||
      !currentViewportView(view) || (following.current && !selectionPaused(viewport))) return;
    const port = scrollportBounds(viewport);
    const offerReturn = () => {
      pausedOutput.current = null;
      setOutputOutside(true);
    };
    if (pending.live.size) {
      for (const output of viewport.querySelectorAll<HTMLElement>("[data-live-output]")) {
        const key = output.dataset.liveOutput!;
        const change = pending.live.get(key);
        const body = change && output.querySelector<HTMLElement>(".msg-streaming-text");
        if (!change || !body || body.textContent !== change.text) continue;
        pending.live.delete(key);
        if (readableChangeOutside(body, change.previous, port)) { offerReturn(); return; }
      }
    }
    if (pending.helpers.size) {
      for (const card of viewport.querySelectorAll<HTMLElement>("[data-helper-work]")) {
        const work = card.dataset.helperWork!;
        const change = pending.helpers.get(work);
        const body = change && card.querySelector<HTMLElement>(".subagent-summary");
        if (!change || !body || body.textContent !== change.text) continue;
        pending.helpers.delete(work);
        const clip = body.closest(".subagent-detail") ? undefined : body.getBoundingClientRect();
        if (readableSpansOutside(body, change.spans, port, clip)) { offerReturn(); return; }
      }
    }
    if ((!pending.saved.size && !pending.helpers.size) ||
      committed.current.messages !== (useSessionStore.getState().messages[view.sessionId] ?? EMPTY_MESSAGES)) return;
    const rows = projectedMessages.current;
    const mounted = Array.from(viewport.querySelectorAll<HTMLElement>("[data-message-key]"));
    const first = mounted[0];
    const last = mounted.at(-1);
    const firstMessage = first && renderedMessages.get(first);
    const lastMessage = last && renderedMessages.get(last);
    const firstIndex = firstMessage ? rows.indexOf(firstMessage) : -1;
    const lastIndex = lastMessage ? rows.indexOf(lastMessage) : -1;
    // The mounted range must cover the scrollport edge before it can certify
    // the position of an unmounted row in the committed projection.
    const outsideMountedRange = (index: number) =>
      (first && index < firstIndex && first.getBoundingClientRect().top <= port.top) ||
      (last && lastIndex >= 0 && index > lastIndex && last.getBoundingClientRect().bottom >= port.bottom);
    for (const [seq, change] of pending.saved) {
      const index = rows.findIndex((message) => message.seq === seq);
      if (index < 0 || rows[index].content !== change.message.content) { pending.saved.delete(seq); continue; }
      const row = mounted.find((element) => renderedMessages.get(element) === rows[index]);
      if (row) {
        pending.saved.delete(seq);
        if (savedOutputOutside(row, change, port)) { offerReturn(); return; }
      } else if (outsideMountedRange(index)) {
        pending.saved.delete(seq);
        if (savedOutputHasBody(change, viewport)) { offerReturn(); return; }
      }
    }
    for (const [work, change] of pending.helpers) {
      const index = rows.findIndex((row) => helperOutputWork(row) === work);
      if (index < 0 || !outsideMountedRange(index)) continue;
      pending.helpers.delete(work);
      if (first && collapsedHelperHasChangedText(change, first)) { offerReturn(); return; }
    }
  }, [currentViewportView, selectionPaused]);
  const followMeasuredOutput = useCallback(() => {
    const viewport = scroller.current;
    const view = currentView.current;
    if (!view?.sessionId || !viewport || followFrame.current) return;
    const sessionId = view.sessionId;
    const canAdvance = () => scroller.current === viewport && following.current && !viewportInputHeld() &&
      !position.current && !document.hidden && viewport.clientHeight > 0 && currentViewportView(view) &&
      !selectionPaused(viewport) && hasTranscriptBaseline(useSessionStore.getState(), sessionId);
    if (!canAdvance()) return;
    if (!reducedMotion.current && typeof window.matchMedia === "function") {
      reducedMotion.current = window.matchMedia("(prefers-reduced-motion: reduce)");
    }
    const work = { view, viewport, frame: 0, time: performance.now() };
    followFrame.current = work;
    const current = () => followFrame.current === work && canAdvance();
    const stop = () => { if (followFrame.current === work) followFrame.current = null; };
    const advance = () => {
      if (!current()) { stop(); return; }
      // Sample execution time consistently with creation. A delivered frame
      // timestamp can precede the request during the same rendering update.
      const time = performance.now();
      const dt = time - work.time;
      // The target belongs to the measured DOM, including the live Footer.
      // Keep this owner throughout the write: another height notification
      // updates its next sample instead of starting a parallel movement.
      const before = sampleScroll(viewport);
      const gap = before.maximum - before.top;
      if (gap > 0 && dt > 0 && current()) {
        const scale = window.devicePixelRatio * (viewport.currentCSSZoom ?? 1);
        const step = Math.max(1, 1 / scale, gap * (1 - Math.exp(-dt / 60)));
        if (reducedMotion.current?.matches || gap - step <= 2) scrollToTail(viewport);
        else {
          viewport.scrollTo({ top: Math.ceil(before.top + step), behavior: "instant" });
          // A rounded intermediate step can leave a reachable tail. Test the
          // physical limit once if this view still owns the same range.
          if (current() && viewport.scrollTop === before.top &&
            viewport.scrollHeight - viewport.clientHeight === before.maximum) scrollToTail(viewport);
        }
      }
      if (!current()) { stop(); return; }
      const after = sampleScroll(viewport);
      lastScroll.current = after;
      // Equal/coarse readings cannot retire an unfinished target. Keep its
      // time origin until a positive interval permits the next actual write.
      if (dt > 0) work.time = time;
      // Continue while making progress or when the write exposes a larger
      // extent. A clamped write against the same range cannot move next frame.
      if (after.maximum - after.top > 2 && (dt <= 0 || after.top > before.top || after.maximum > before.maximum)) {
        work.frame = requestAnimationFrame(advance);
      } else {
        initialPresentation.current.automaticReturn = false;
        stop();
      }
    };
    work.frame = requestAnimationFrame(advance);
  }, [currentViewportView, selectionPaused, viewportInputHeld]);
  const retireRecovery = useCallback((owner = initialPresentation.current) => {
    const recovery = owner.recovery;
    owner.recovery = undefined;
    owner.automaticReturn = false;
    recovery?.release();
  }, []);
  const resumeVisibleFollowing = useCallback((reason?: "accepted-output") => {
    const view = currentView.current;
    if (!view?.sessionId || !currentViewportView(view) ||
      !hasTranscriptBaseline(useSessionStore.getState(), view.sessionId)) return;
    const owner = initialPresentation.current;
    retireRecovery(owner);
    owner.automaticReturn = reason === "accepted-output";
    retirePosition(); cancelFollow(); retireWheel();
    scrollNavigation.current = null;
    readingHandoff.current = null;
    following.current = true;
    setReadingBoundary(false);
    setScrolledIntoHistory(false);
    resetPausedOutput();
    owner.timeoutHint = false;
    // Following grants visible movement; it does not manufacture the retired
    // initial operation's measurement proof or open a new preparation budget.
    publishPresentation(owner, "reading");
    followMeasuredOutput();
  }, [cancelFollow, currentViewportView, followMeasuredOutput, publishPresentation, resetPausedOutput, retirePosition, retireRecovery, retireWheel]);
  const consumeTimeoutRecovery = useCallback((owner = initialPresentation.current) => {
    if (owner !== initialPresentation.current || !owner.recovery?.pending || document.hidden ||
      !scroller.current?.clientHeight || viewportInputHeld() || selectionPaused()) return;
    resumeVisibleFollowing("accepted-output");
  }, [resumeVisibleFollowing, selectionPaused, viewportInputHeld]);
  useLayoutEffect(() => () => retireRecovery(), [retireRecovery]);
  const failPreparation = useCallback((owner: InitialPresentation, error: string) => {
    if (owner !== initialPresentation.current ||
      owner.phase !== "preparing" && owner.phase !== "revealing" && owner.phase !== "incomplete") return;
    const entering = owner.phase !== "incomplete";
    if (entering) {
      pausePreparation(owner);
      retirePosition(); cancelFollow();
      following.current = false;
    }
    owner.error = error;
    publishPresentation(owner, "incomplete");
    if (entering) staging.current?.fail(error);
  }, [cancelFollow, retirePosition, publishPresentation]);
  const expirePreparation = useCallback((owner: InitialPresentation) => {
    const view = currentView.current;
    const viewport = scroller.current;
    if (owner !== initialPresentation.current || !view?.sessionId || !currentViewportView(view) ||
      !hasTranscriptBaseline(useSessionStore.getState(), view.sessionId)) return;
    pausePreparation(owner);
    retirePosition(); cancelFollow();
    if (staging.current) {
      staging.current.fail("The successor could not be positioned. Open it when you are ready to continue.");
      return;
    }
    following.current = false;
    scrollNavigation.current = null;
    readingHandoff.current = viewport ? { view, anchor: captureReadingAnchor(viewport),
      physical: { viewport, top: viewport.scrollTop } } : null;
    const focused = document.activeElement;
    focusReading.current = focused && viewport?.parentElement?.querySelector("[data-chat-initial-surface]")?.contains(focused)
      ? { owner, source: focused } : null;
    owner.timedOut = true;
    owner.timeoutHint = true;
    const recovery = { pending: false, release: () => {} };
    owner.recovery = recovery;
    // Register at the timeout boundary itself. An effect would miss accepted
    // output between expiry and React's next commit, including zero publication.
    recovery.release = observeAcceptedText(view.sessionId, view.account, () => {
      if (owner !== initialPresentation.current || owner.recovery !== recovery) return;
      recovery.pending = true;
      consumeTimeoutRecovery(owner);
    });
    publishPresentation(owner, "reading");
  }, [cancelFollow, consumeTimeoutRecovery, retirePosition, currentViewportView, publishPresentation]);
  const preparationBudget = useCallback(function updateBudget() {
    const owner = initialPresentation.current;
    const view = currentView.current;
    const viewport = scroller.current;
    const active = (owner.phase === "preparing" || owner.phase === "revealing") && view?.sessionId &&
      currentViewportView(view) && hasTranscriptBaseline(useSessionStore.getState(), view.sessionId) &&
      viewport?.clientHeight && !document.hidden && !viewportInputHeld() && !selectionPaused(viewport);
    if (!active) { pausePreparation(owner); return; }
    if (owner.since !== null) return;
    if (owner.remaining <= 0) { expirePreparation(owner); return; }
    owner.since = performance.now();
    owner.deadline = window.setTimeout(() => {
      if (owner !== initialPresentation.current) return;
      pausePreparation(owner);
      updateBudget();
    }, owner.remaining);
  }, [currentViewportView, expirePreparation, selectionPaused, viewportInputHeld]);
  const pauseFollowing = useCallback((source?: "handoff" | "details") => {
    retireRecovery();
    retireWheel();
    following.current = false;
    cancelFollow();
    retirePosition();
    scrollNavigation.current = null;
    const viewport = scroller.current;
    const view = currentView.current;
    if (source !== "handoff" && viewport?.clientHeight && view && currentViewportView(view)) {
      // Expanding details can precede the restoration write's scroll report
      // without changing the current position or its source.
      const restored = source === "details" ? restoredReadingAt(readingHandoff.current, view, viewport) : undefined;
      readingHandoff.current = { view, anchor: captureReadingAnchor(viewport), physical: restored ?? { viewport, top: viewport.scrollTop } };
    }
    const owner = initialPresentation.current;
    pausePreparation(owner);
    publishPresentation(owner, "reading");
  }, [cancelFollow, retirePosition, currentViewportView, publishPresentation, retireRecovery, retireWheel]);
  const pauseForReading = useCallback((event: MouseEvent<HTMLElement>) => {
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest("button[aria-expanded], summary")) pauseFollowing("details");
  }, [pauseFollowing]);
  const pauseForScrollbar = useCallback((contact: NavigationContact) => {
    const navigation = scrollNavigation.current;
    pauseFollowing();
    if (!contact.invalid && navigation?.contact === contact) scrollNavigation.current = navigation;
  }, [pauseFollowing]);
  const rebaseContactAfterLayout = useCallback((viewport: HTMLElement, sample?: ReturnType<typeof sampleScroll>) => {
    const navigation = scrollNavigation.current;
    if (viewport !== scroller.current) return;
    if (!navigation?.contact || navigation.viewport !== viewport) {
      contentObserver.current?.disconnect();
      return;
    }
    const measured = sample ?? sampleScroll(viewport);
    if (navigation.contact !== navigationContact.current &&
      (navigation.contact.source === "scrollbar" || performance.now() > navigation.until || navigation.maximum !== measured.maximum)) {
      scrollNavigation.current = null;
      contentObserver.current?.disconnect();
    } else {
      Object.assign(navigation, measured);
    }
  }, []);
  const observeUpwardScroll = useCallback((viewport: HTMLElement, view: ChatViewportView,
    sample = sampleScroll(viewport), source: "scroll" | "measure" | "home" = "scroll") => {
    const navigation = scrollNavigation.current;
    if (navigation?.direction !== "up") return;
    const { top, maximum } = sample;
    const scrollbar = navigation.contact?.source === "scrollbar";
    if (navigation.contact && !scrollbar) return;
    const held = scrollbar && navigation.contact === navigationContact.current;
    if (navigation.viewport !== viewport || navigation.view !== view || !currentViewportView(view) ||
      (!held && performance.now() > navigation.until) ||
      (position.current && !held && !scrollbar)) {
      scrollNavigation.current = null;
      return;
    }
    // A layout clamp can lower scrollTop before the original range returns.
    // The native scroll report alone contains no intermediate geometry.
    if (scrollbar && contentObserver.current?.takeRecords().length) {
      rebaseContactAfterLayout(viewport, sample);
      return;
    }
    if ((source !== "home" && maximum !== navigation.maximum) || (source !== "measure" && top > navigation.top)) {
      if (scrollbar) rebaseContactAfterLayout(viewport, sample);
      else scrollNavigation.current = null;
      return;
    }
    if (source === "measure" || top === navigation.top) return;
    navigation.top = top;
    navigation.until = performance.now() + 500;
    if (maximum - top <= 4) return;
    scrollNavigation.current = null;
    if (scrollbar || following.current) pauseFollowing();
    setScrolledIntoHistory(true);
  }, [currentViewportView, pauseFollowing, rebaseContactAfterLayout]);
  const navigate = useCallback((up: boolean, from: number, atBottom = false) => {
    const viewport = scroller.current;
    const view = currentView.current;
    if (!viewport?.clientHeight || document.hidden || !view || !currentViewportView(view)) return false;
    if (viewport.scrollTop > 0) setPagingReady(true);
    pauseFollowing();
    const { top, maximum } = sampleScroll(viewport);
    const observation = { view, viewport, top: from, maximum, until: performance.now() + 500 };
    if (up) {
      scrollNavigation.current = { ...observation, top: lastScroll.current.maximum === maximum ? Math.max(top, from) : top,
        direction: "up" };
      observeUpwardScroll(viewport, view);
    } else if (!navigationContact.current && (atBottom || maximum - top <= 4)) {
      resumeVisibleFollowing();
    } else scrollNavigation.current = { ...observation, direction: "down" };
    return true;
  }, [pauseFollowing, currentViewportView, observeUpwardScroll, resumeVisibleFollowing]);
  const sampleHeldContact = useCallback((viewport: HTMLElement, view: ChatViewportView, sample: ReturnType<typeof sampleScroll>,
    confirmMovement: boolean) => {
    const navigation = scrollNavigation.current;
    if (!navigation?.contact || navigation.contact !== navigationContact.current || navigation.contact.invalid ||
      navigation.contact.source === "scrollbar" || navigation.viewport !== viewport || navigation.view !== view) return;
    const layoutChanged = changesScrollLayout(viewport, contentObserver.current?.takeRecords()) || navigation.maximum !== sample.maximum;
    if (confirmMovement && !layoutChanged) {
      const movedUp = navigation.direction === "up" && sample.top < navigation.top;
      const movedDown = navigation.direction === "down" && sample.top > navigation.top;
      if (movedUp || movedDown) navigation.wasAwayFromTop ||= navigation.top > 4 || sample.top > 4;
      if (movedUp) {
        navigation.upward = sample.maximum - sample.top > 4 ? "history" : navigation.upward ?? "scrolled";
      }
      if (movedDown) navigation.downward = sample.maximum - sample.top <= 4 ? "bottom" : "scrolled";
      else if (navigation.direction === "down" && sample.top < navigation.top) navigation.downward = undefined;
    }
    // Measurement can retire geometry but cannot prove navigation. A scroll
    // before the first direction supplies the next baseline without proof.
    if (confirmMovement || layoutChanged) Object.assign(navigation, sample);
    return navigation;
  }, []);
  const observeContactLayout = useCallback((viewport: HTMLElement) => {
    contentObserver.current?.disconnect();
    contentObserver.current?.observe(viewport, { subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ["style", "class", "hidden", "open"] });
  }, []);
  const considerContactNavigation = (contact: NavigationContact, target: EventTarget, up: boolean) => {
    const viewport = scroller.current;
    const view = currentView.current;
    if (contact.invalid || !viewport?.clientHeight || !view || !currentViewportView(view) || document.hidden) return;
    const sample = sampleScroll(viewport);
    const current = scrollNavigation.current;
    const from = up && current?.contact === contact && current.direction === "up" && current.maximum === sample.maximum
      ? Math.max(sample.top, current.top) : sample.top;
    const direction = canScrollChat(viewport, target, up, from) ? up ? "up" : "down" : "idle";
    const navigation = current?.contact === contact
      ? sampleHeldContact(viewport, view, sample, true)
      : undefined;
    if (navigation) {
      if (direction !== navigation.direction) navigation.downward = undefined;
      navigation.direction = direction;
    } else {
      // A separate explicit input during the contact retires earlier contact
      // evidence. Subsequent movement starts from a fresh baseline.
      scrollNavigation.current = { view, viewport, ...sample, direction, contact, until: 0 };
      observeContactLayout(viewport);
    }
  };
  const invalidateNavigationContact = useCallback((contact: NavigationContact) => {
    contact.invalid = true;
    if (scrollNavigation.current?.contact === contact) {
      scrollNavigation.current = null;
      contentObserver.current?.disconnect();
    }
  }, []);
  const startNavigationContact = useCallback((source: NavigationContact["source"], id: number, y: number, pointerType?: string) => {
    const viewport = scroller.current;
    const view = currentView.current;
    if (!viewport?.clientHeight || document.hidden || !view || !currentViewportView(view)) return;
    retireWheel();
    suspendViewportMotion();
    // Contact suspends writes. The current reading decision remains with the
    // opening, including any input that arrives during this contact.
    const sample = sampleScroll(viewport);
    const contact: NavigationContact = { source, id, y, scrollbarStart: source === "scrollbar" ? sample.top : undefined,
      invalid: false, pointers: new Map(pointerType === undefined ? [] : [[id, pointerType]]),
      touch: null, sessionId: view.sessionId, account: view.account };
    navigationContact.current = contact;
    // Both a thumb drag and a track click can move up without another pointer event.
    scrollNavigation.current = { view, viewport, ...sample, direction: source === "scrollbar" ? "up" : "idle", until: 0, contact };
    observeContactLayout(viewport);
    preparationBudget();
  }, [currentViewportView, observeContactLayout, preparationBudget, retireWheel, suspendViewportMotion]);
  // Automatic paging needs a genuine departure from the physical top.
  // Settling or cancelling an initial target at mount-time 0 does not grant it.
  const [pagingReady, setPagingReady] = useState(false);
  const [atTop, setAtTop] = useState(false);
  // Failure stops automatic retries at the same top. The manual entry stays usable.
  const [olderStalled, setOlderStalled] = useState(false);
  const reportPagingPosition = useCallback((viewport: HTMLElement) => {
    if (viewport.scrollHeight <= viewport.clientHeight) {
      setAtTop(false);
      return;
    }
    const atStart = atLoadedStart(viewport, projectedMessages.current[0]);
    setAtTop(atStart);
    if (!atStart) setOlderStalled(false);
    if (!position.current && viewport.scrollTop > 4) setPagingReady(true);
  }, []);
  const yieldPositionToContact = useCallback((navigation: ScrollNavigation) => {
    retirePosition();
    readingHandoff.current = null;
    if (navigation.wasAwayFromTop) setPagingReady(true);
    reportPagingPosition(navigation.viewport);
  }, [retirePosition, reportPagingPosition]);
  const observeReleasedContactScroll = useCallback((viewport: HTMLElement, view: ChatViewportView,
    sample = sampleScroll(viewport)) => {
    const navigation = scrollNavigation.current;
    if (navigation?.direction !== "down" || !navigation.contact || navigation.writingPosition || navigationContact.current || pendingWheel.current ||
      navigation.viewport !== viewport || navigation.view !== view) return false;
    const hasUnmeasuredLayout = changesScrollLayout(viewport, contentObserver.current?.takeRecords());
    const now = performance.now();
    const rangeChanged = navigation.maximum !== sample.maximum;
    if (now <= navigation.until && !rangeChanged && !hasUnmeasuredLayout && sample.top > navigation.top) {
      navigation.wasAwayFromTop ||= navigation.top > 4 || sample.top > 4;
      navigation.top = sample.top;
      navigation.until = now + 500;
      yieldPositionToContact(navigation);
      if (sample.maximum - sample.top <= 4) {
        contentObserver.current?.disconnect();
        resumeVisibleFollowing();
      }
      return true;
    }
    if (now > navigation.until || rangeChanged || !hasUnmeasuredLayout && sample.top < navigation.top) {
      scrollNavigation.current = null;
      contentObserver.current?.disconnect();
    } else if (hasUnmeasuredLayout) {
      Object.assign(navigation, sample);
    }
    return false;
  }, [resumeVisibleFollowing, yieldPositionToContact]);
  const settleContactBeforeLayout = useCallback((viewport: HTMLElement, view: ChatViewportView) => {
    const navigation = scrollNavigation.current;
    if (!navigation?.contact || navigation.contact.source === "scrollbar" || navigation.viewport !== viewport ||
      navigation.view !== view || !currentViewportView(view) || document.hidden) return false;
    if (navigation.direction === "idle") return true;
    if (!viewport.clientHeight) return false;
    const sample = sampleScroll(viewport);
    if (navigationContact.current) sampleHeldContact(viewport, view, sample, true);
    else observeReleasedContactScroll(viewport, view, sample);
    return true;
  }, [currentViewportView, observeReleasedContactScroll, sampleHeldContact]);
  const writePosition = useCallback((viewport: HTMLElement, view: ChatViewportView, write: () => void) => {
    const sample = sampleScroll(viewport);
    if (observeReleasedContactScroll(viewport, view, sample)) return false;
    const navigation = scrollNavigation.current;
    if (navigation?.direction !== "down" || !navigation.contact || navigation.viewport !== viewport || navigation.view !== view) {
      write();
      return true;
    }
    // A report delivered inside the write still belongs to that write. When
    // the browser clamps or rounds it to the same position, keep the evidence.
    navigation.writingPosition = true;
    try {
      write();
    } finally {
      navigation.writingPosition = false;
      if (scrollNavigation.current === navigation && viewport.scrollTop !== sample.top) {
        scrollNavigation.current = null;
        contentObserver.current?.disconnect();
      }
    }
    return true;
  }, [observeReleasedContactScroll]);
  const alignCommittedReadingRow = useCallback((element: HTMLDivElement | null) => {
    const operation = position.current;
    if (!element || operation?.kind !== "history" || !operation.alignOnMount ||
      element.dataset.messageKey !== operation.key || !currentPosition(operation) ||
      document.hidden || viewportInputHeld()) return;
    const { viewport, expectedTop } = operation;
    if (!viewport.clientHeight || observeReleasedContactScroll(viewport, operation.view) ||
      expectedTop !== undefined && expectedTop !== viewport.scrollTop) return;
    alignReadingAnchor(operation, element, writePosition);
  }, [currentPosition, observeReleasedContactScroll, viewportInputHeld, writePosition]);
  const restorePhysicalReading = useCallback((view: ChatViewportView, viewport: HTMLElement, top: number) => {
    if (!writePosition(viewport, view, () => { viewport.scrollTop = top; })) return false;
    // Keep the actual application write as the current reading sample. Its
    // delayed scroll notification must not become a new navigation report.
    readingHandoff.current = { view, anchor: null, physical: { viewport, top: viewport.scrollTop, restored: true } };
    return true;
  }, [writePosition]);
  const positionMeasuredOutput = useCallback(function schedulePosition() {
    preparationBudget();
    const operation = position.current;
    const canPosition = (operation: ViewportPosition) => currentPosition(operation) && operation.viewport.clientHeight &&
      !document.hidden && !viewportInputHeld() && (operation.kind !== "initial" || !selectionPaused(operation.viewport));
    if (!operation || !canPosition(operation)) return;
    // A live Footer can grow between the reveal commit and the first paint.
    // The initial operation still owns that layout and lands without animation.
    const sessionId = operation.view.sessionId;
    if (operation.kind === "initial" && sessionId && initialPresentation.current.phase === "ready" &&
      measureInitialTail(operation.viewport, projectedMessages.current.at(-1), sessionId) === "position") {
      if (!writePosition(operation.viewport, operation.view, () => scrollToTail(operation.viewport))) return;
    }
    if (operation.frame !== null) return;
    const frame = requestAnimationFrame(() => {
      if (operation.frame !== frame) return;
      operation.frame = null;
      if (!canPosition(operation)) return;
      const viewport = operation.viewport;
      if (observeReleasedContactScroll(viewport, operation.view)) return;
      const rows = projectedMessages.current;
      if (operation.kind === "restore") {
        const handoff = operation.handoff;
        const physical = handoff.physical;
        if (readingHandoff.current !== handoff || !physical) { retirePosition(operation); return; }
        if (movedFromScrollbarPosition(handoff, viewport)) {
          readingHandoff.current = null;
          retirePosition(operation);
          return;
        }
        const limit = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
        if (limit < physical.top && limit !== operation.limit) {
          operation.limit = limit;
          schedulePosition();
          return;
        }
        retirePosition(operation);
        restorePhysicalReading(operation.view, viewport, Math.min(physical.top, limit));
        measurePausedOutput();
        return;
      }
      const finish = () => {
        if (!currentPosition(operation)) return;
        // A replacement can commit before the browser delivers this write's
        // scroll event. Publish the reading handoff with the position itself.
        if (!following.current) {
          const anchor = captureReadingAnchor(viewport);
          readingHandoff.current = { view: operation.view, anchor };
        }
        retirePosition(operation);
        measurePausedOutput();
        followMeasuredOutput();
      };
      if (operation.kind === "initial") {
        if (!operation.view.sessionId || !hasTranscriptBaseline(useSessionStore.getState(), operation.view.sessionId)) return;
        const tail = measureInitialTail(viewport, rows.at(-1), operation.view.sessionId);
        const owner = initialPresentation.current;
        const before = viewport.scrollTop;
        if (tail === "position" && !writePosition(viewport, operation.view, () => scrollToTail(viewport))) return;
        if (tail === "position" && viewport.scrollTop > before) {
          // The extent can change while Virtuoso installs the new range.
          schedulePosition();
        } else if (owner.phase === "ready") {
          finish();
        } else if (tail === "readable") {
          if (owner.phase === "preparing") publishPresentation(owner, "revealing");
          else if (owner.phase === "revealing" && staging.current?.current()) {
            const state = useSessionStore.getState();
            const sid = operation.view.sessionId;
            const snapshot = committed.current;
            if (snapshot.messages !== (state.messages[sid] ?? EMPTY_MESSAGES) ||
              snapshot.subagentSnapshots !== (state.subagents[sid] ?? EMPTY_SUBAGENTS) ||
              snapshot.activeRequestId !== state.dispatchStates[sid]?.active_request_id ||
              snapshot.pendingQuestions !== (state.pendingQuestions[sid] ?? EMPTY_QUESTIONS)) {
              schedulePosition();
            } else staging.current.commit();
          }
        }
        return;
      }
      // A native scroll can arrive after pointerup, even without a delivered
      // scroll event yet. A guarded handoff yields to any intervening movement.
      const canCorrect = () => {
        if (!currentPosition(operation) || viewportInputHeld()) return false;
        if (operation.expectedTop === undefined || operation.expectedTop === viewport.scrollTop) return true;
        finish();
        return false;
      };
      if (!canCorrect()) return;
      const index = rows.findIndex((message) => chatMessageKey(message) === operation.key);
      if (index < 0) {
        const top = Math.max(0, Math.min(operation.physicalTop, viewport.scrollHeight - viewport.clientHeight));
        if (!writePosition(viewport, operation.view, () => { viewport.scrollTop = top; })) return;
        setReadingBoundary(true);
        finish(); return;
      }
      const element = Array.from(viewport.querySelectorAll<HTMLElement>("[data-message-key]"))
        .find((row) => row.dataset.messageKey === operation.key);
      if (element) {
        const result = alignReadingAnchor(operation, element, writePosition);
        if (result === "superseded") return;
        if (operation.selected && result !== "moved" && element.parentElement && virtuoso.current) {
          const height = element.parentElement.getBoundingClientRect().height;
          let measured = false;
          virtuoso.current.getState(({ ranges }) => {
            measured = ranges.some(({ startIndex, endIndex, size }) =>
              startIndex <= index && index <= endIndex && Math.abs(size - height) <= 1);
          });
          // The first DOM write can precede Virtuoso's size compensation.
          // The list's measurement notifications wake the still-owned correction.
          if (!measured) return;
        }
        if (result === "moved" || result === "aligned" && operation.alignedHeight !== viewport.scrollHeight) {
          // A write can change the virtual range. Confirm the same anchor
          // after measurement, and end the operation after a physical clamp.
          if (result === "aligned") operation.alignedHeight = viewport.scrollHeight;
          schedulePosition();
          return;
        }
        const fonts = element.ownerDocument.fonts;
        const selected = operation.selected;
        const restored = selected?.restored;
        if (selected && restored && fonts?.status === "loading") {
          const { view } = operation;
          const settledTop = viewport.scrollTop;
          // A font swap can move a glyph without changing the row height.
          // Let normal positioning settle and recheck the still-owned range once.
          void fonts.ready.then(() => {
            if (position.current || scroller.current !== viewport || !viewport.clientHeight || !currentViewportView(view) || document.hidden ||
              viewportInputHeld() || viewport.scrollTop !== settledTop || transcriptSelection(viewport)?.getRangeAt(0) !== restored) return;
            if (!element.contains(restored.startContainer) || !element.contains(restored.endContainer)) return;
            const top = Array.from(restored.getClientRects())[0]?.top;
            if (top === undefined) return;
            const delta = top - viewport.getBoundingClientRect().top - selected.offset;
            if (Math.abs(delta) <= 1 || !writePosition(viewport, view, () => { viewport.scrollTop += delta; })) return;
            if (!following.current) readingHandoff.current = { view, anchor: captureReadingAnchor(viewport) };
            measurePausedOutput();
          });
        }
        finish();
      } else {
        operation.alignedHeight = undefined;
        // Probe the row itself before applying an offset inside a tall row:
        // an unmounted row may currently be estimated as only one short line.
        // A transient projection beyond the physical list is not usable yet;
        // the existing render/measurement notifications deliver its successor.
        // Returning null avoids starting the library's index retry lifecycle.
        virtuoso.current?.scrollIntoView({ index, calculateViewLocation: ({ itemTop, viewportTop }) => {
          if (canCorrect() && viewport.clientHeight) {
            const top = viewport.scrollTop + itemTop - viewportTop;
            if (top >= 0 && top < viewport.scrollHeight && Math.abs(top - viewport.scrollTop) > 1 &&
              currentPosition(operation) &&
              writePosition(viewport, operation.view, () => virtuoso.current?.scrollTo({ top, behavior: "auto" }))) {
              if (operation.expectedTop !== undefined) operation.expectedTop = viewport.scrollTop;
              schedulePosition();
            }
          }
          return null;
        } });
      }
    });
    operation.frame = frame;
  }, [currentPosition, currentViewportView, followMeasuredOutput, observeReleasedContactScroll, preparationBudget, publishPresentation,
    measurePausedOutput, retirePosition, restorePhysicalReading, selectionPaused, viewportInputHeld, writePosition]);
  const restoreReading = useCallback((view: ChatViewportView, viewport: HTMLElement) => {
    if (!currentViewportView(view) || !viewport.clientHeight || document.hidden || viewportInputHeld()) return;
    const handoff = readingHandoff.current;
    const maxTop = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
    if (handoff?.scrollbarPosition?.viewport === viewport && handoff.scrollbarPosition.top > maxTop) {
      // A geometrically impossible prior offset is no witness of movement.
      handoff.scrollbarPosition = undefined;
    }
    const guard = handoff?.scrollbarPosition;
    const pending = position.current?.kind === "restore" ? position.current : null;
    if (handoff && movedFromScrollbarPosition(handoff, viewport)) {
      if (pending) retirePosition(pending);
      readingHandoff.current = null;
      reportPagingPosition(viewport);
      return;
    }
    const physical = handoff?.physical;
    // A visible pane may get its viewport box before the old list extent.
    if (handoff && !handoff.anchor && physical?.viewport === viewport && handoff.view.window === view.window &&
      physical.top > maxTop) {
      if (!pending) {
        position.current = { kind: "restore", view, viewport, frame: null, handoff, limit: maxTop };
      }
      positionMeasuredOutput();
      return;
    }
    if (pending) retirePosition(pending);
    readingHandoff.current = null;
    if (handoff?.anchor) {
      position.current = { kind: "history", view, viewport, ...handoff.anchor, frame: null,
        physicalTop: physical?.viewport === viewport ? physical.top : 0,
        expectedTop: guard ? viewport.scrollTop : undefined };
      positionMeasuredOutput();
    } else if (physical?.viewport === viewport) {
      if (restorePhysicalReading(view, viewport, Math.min(physical.top, maxTop)) && handoff?.view.window !== view.window) setReadingBoundary(true);
    } else {
      if (restorePhysicalReading(view, viewport, 0)) setReadingBoundary(true);
    }
  }, [currentViewportView, positionMeasuredOutput, reportPagingPosition, restorePhysicalReading,
    retirePosition, viewportInputHeld]);
  const measuredOutput = useCallback(() => {
    preparationBudget();
    const viewport = scroller.current;
    if (!viewport?.clientHeight || document.hidden) {
      suspendViewportMotion();
      return;
    }
    const view = currentView.current;
    const sample = sampleScroll(viewport);
    if (view) {
      observeUpwardScroll(viewport, view, sample, "measure");
      sampleHeldContact(viewport, view, sample, false);
    }
    if (!following.current && view && initialPresentation.current.phase !== "incomplete" &&
      pendingReadingHandoff(readingHandoff.current, view, viewport)) {
      restoreReading(view, viewport);
    }
    positionMeasuredOutput();
    consumeTimeoutRecovery();
    measurePausedOutput();
    followMeasuredOutput();
  }, [consumeTimeoutRecovery, positionMeasuredOutput, followMeasuredOutput, measurePausedOutput, observeUpwardScroll,
    preparationBudget, restoreReading, sampleHeldContact, suspendViewportMotion]);
  const onItemsRendered = useCallback(() => {
    positionMeasuredOutput();
    measurePausedOutput();
  }, [positionMeasuredOutput, measurePausedOutput]);
  useEffect(() => {
    const changed = () => {
      const viewport = scroller.current;
      const view = currentView.current;
      if (!viewport || !view || !currentViewportView(view)) return;
      if (selectionPaused(viewport)) {
        cancelFollow();
        preparationBudget();
      } else if (selectedViewport.current === viewport) {
        selectedViewport.current = null;
        if (following.current) resetPausedOutput();
        measuredOutput();
      }
    };
    document.addEventListener("selectionchange", changed);
    return () => document.removeEventListener("selectionchange", changed);
  }, [cancelFollow, currentViewportView, measuredOutput, preparationBudget, resetPausedOutput, selectionPaused]);
  const settleWheel = useCallback((wheel = pendingWheel.current) => {
    if (!wheel || pendingWheel.current !== wheel || wheel.event.eventPhase !== Event.NONE) return;
    retireWheel();
    if (scroller.current !== wheel.viewport || !currentViewportView(wheel.view)) return;
    if (!wheel.event.defaultPrevented) {
      const { top, maximum } = sampleScroll(wheel.viewport);
      if (navigate(wheel.event.deltaY < 0, maximum === wheel.maximum ? wheel.from : top, wheel.atBottom)) return;
    }
    measuredOutput();
  }, [currentViewportView, measuredOutput, navigate, retireWheel]);
  const releaseNavigationContact = useCallback((contact: NavigationContact, y: number) => {
    if (navigationContact.current !== contact) return;
    settleWheel();
    if (navigationContact.current !== contact) return;
    const until = performance.now() + 500;
    const heldNavigation = scrollNavigation.current;
    if (heldNavigation?.contact === contact) heldNavigation.until = until;
    const viewport = scroller.current;
    const view = currentView.current;
    const canMeasure = viewport?.clientHeight && view && contact.sessionId === view.sessionId &&
      contact.account === view.account && currentViewportView(view);
    const owner = initialPresentation.current;
    const scrollbar = contact.source === "scrollbar";
    const gesture = !scrollbar && canMeasure ? sampleHeldContact(viewport, view, sampleScroll(viewport), true) : undefined;
    navigationContact.current = null;
    if (gesture) {
      const bottom = gesture.maximum - gesture.top <= 4;
      // Only downward input can hand off to native inertia. The release
      // sample closes all upward evidence, including a zero-movement stroke.
      const continuesDown = gesture.direction === "down";
      if (!continuesDown) {
        scrollNavigation.current = null;
        contentObserver.current?.disconnect();
      }
      if (gesture.upward || gesture.downward) yieldPositionToContact(gesture);
      if (bottom && gesture.downward === "bottom") {
        contentObserver.current?.disconnect();
        resumeVisibleFollowing();
      }
      else if (gesture.upward) {
        pauseFollowing();
        if (continuesDown) scrollNavigation.current = gesture;
        if (gesture.upward === "history") setScrolledIntoHistory(true);
      }
      measuredOutput();
      return;
    }
    if (scrollbar && (owner.recovery || owner.automaticReturn) &&
      (!canMeasure || contact.invalid || y === contact.y)) {
      // A same-coordinate native release may start scrolling after pointerup.
      // Keep reading until an explicit return supplies a new permission.
      pauseFollowing(readingHandoff.current?.scrollbarPosition ? "handoff" : undefined);
      measuredOutput();
      return;
    }
    // A primary scrollbar release can show movement without pointermove.
    // A handoff invalidates the old geometry for returning down, but the
    // mouse can still withdraw permission to move the reader's position.
    if (scrollbar && y !== contact.y) pauseForScrollbar(contact);
    else if (scrollbar && !following.current && position.current?.kind === "history") retirePosition();
    if (following.current) readingHandoff.current = null;
    if (!canMeasure) return;
    observeUpwardScroll(viewport, view);
    const navigation = scrollNavigation.current;
    const directed = navigation?.viewport === viewport && navigation.view === view;
    const downward = directed && navigation.direction === "down" && !navigation.contact;
    const { top, maximum } = sampleScroll(viewport);
    const movedDown = downward && top >= navigation.top;
    // Native thumb drags may omit pointermove.
    const returned = (!contact.invalid && contact.scrollbarStart !== undefined && y > contact.y &&
      top > contact.scrollbarStart || movedDown) &&
      maximum - top <= 4;
    if (returned) resumeVisibleFollowing();
    else if (directed) navigation.until = until;
    if (movedDown) {
      retirePosition();
      readingHandoff.current = null;
    }
    measuredOutput();
  }, [currentViewportView, measuredOutput, observeUpwardScroll, pauseFollowing, pauseForScrollbar, resumeVisibleFollowing,
    retirePosition, sampleHeldContact, settleWheel, yieldPositionToContact]);
  useEffect(() => {
    // The opening owns a held contact, even if its original viewport is
    // replaced or the release lands outside the list. Geometry stays scoped.
    const pointerDown = (event: PointerEvent) => {
      const contact = navigationContact.current;
      if (!contact || contact.touch && event.pointerType === "touch" || contact.pointers.has(event.pointerId)) return;
      contact.pointers.set(event.pointerId, event.pointerType);
      contact.invalid = true;
      // Mixed contact cannot grant a return. The native scrollbar can still
      // report actual upward movement under its existing geometry guard.
      if (contact.source !== "scrollbar") scrollNavigation.current = null;
    };
    const pointerEnd = (event: PointerEvent) => {
      const contact = navigationContact.current;
      if (!contact || !contact.pointers.delete(event.pointerId)) return;
      if (event.type === "pointercancel") invalidateNavigationContact(contact);
      const y = event.type === "pointerup" && event.pointerId === contact.id ? event.clientY : contact.y;
      if (!contact.touch && !contact.pointers.size) {
        releaseNavigationContact(contact, y);
      } else if (contact.source === "scrollbar" && y !== contact.y) {
        pauseFollowing();
      }
    };
    const touchStart = (event: TouchEvent) => {
      const contact = navigationContact.current;
      if (contact?.touch && event.touches.length > 1) {
        contact.touch.multiple = true;
        invalidateNavigationContact(contact);
      }
    };
    const touchEnd = (event: TouchEvent) => {
      const contact = navigationContact.current;
      if (!contact?.touch) return;
      const { id, multiple } = contact.touch;
      const touch = Array.from(event.changedTouches).find((item) => item.identifier === id);
      if (!touch && !multiple) return;
      if (event.type === "touchcancel" || event.touches.length) invalidateNavigationContact(contact);
      if (!event.touches.length) {
        contact.touch = null;
        if (!contact.pointers.size) releaseNavigationContact(contact, contact.y);
      }
    };
    window.addEventListener("pointerdown", pointerDown, true);
    window.addEventListener("pointerup", pointerEnd, true);
    window.addEventListener("pointercancel", pointerEnd, true);
    window.addEventListener("touchstart", touchStart, true);
    window.addEventListener("touchend", touchEnd, true);
    window.addEventListener("touchcancel", touchEnd, true);
    return () => {
      window.removeEventListener("pointerdown", pointerDown, true);
      window.removeEventListener("pointerup", pointerEnd, true);
      window.removeEventListener("pointercancel", pointerEnd, true);
      window.removeEventListener("touchstart", touchStart, true);
      window.removeEventListener("touchend", touchEnd, true);
      window.removeEventListener("touchcancel", touchEnd, true);
    };
  }, [invalidateNavigationContact, pauseFollowing, releaseNavigationContact]);
  useEffect(() => {
    const visibilityChanged = () => {
      // A hidden page may stop delivering frames altogether. Retire now;
      // returning starts from the current extent and a fresh execution clock.
      measuredOutput();
    };
    document.addEventListener("visibilitychange", visibilityChanged);
    return () => document.removeEventListener("visibilitychange", visibilityChanged);
  }, [cancelFollow, measuredOutput]);
  const startInitialPosition = useCallback((viewport: HTMLElement) => {
    retirePosition();
    scrollNavigation.current = null;
    if (navigationContact.current?.source !== "scrollbar") readingHandoff.current = null;
    const owner = initialPresentation.current;
    // Timeout retires initial for the entire opening, including later window
    // and DOM handoffs. A new follow permission uses the visible follow owner.
    if (owner.timedOut) { followMeasuredOutput(); return; }
    const view = currentView.current;
    if (!view?.sessionId || !currentViewportView(view)) return;
    if (!staging.current) {
      const state = useSessionStore.getState();
      const session = state.sessions.find((row) => row.id === view.sessionId) ??
        state.archivedSessions.find((row) => row.id === view.sessionId);
      if (session && hasEmptyTranscript(state, session)) {
        if (owner.phase !== "ready") publishPresentation(owner, "ready");
        return;
      }
    }
    if (owner.phase !== "preparing") publishPresentation(owner, "preparing");
    position.current = { kind: "initial", view, viewport, frame: null };
    positionMeasuredOutput();
  }, [retirePosition, positionMeasuredOutput, currentViewportView, publishPresentation, followMeasuredOutput]);
  const retryHistoryRead = useCallback(() => {
    const view = currentView.current;
    if (!view?.sessionId || !currentViewportView(view)) return;
    void observeSession(view.sessionId, "detail").done.catch(() => {});
  }, [currentViewportView]);
  const saveReadingHandoff = useCallback((view: ChatViewportView, viewport: HTMLElement) => {
    const scrollbar = navigationContact.current?.source === "scrollbar";
    const previous = readingHandoff.current;
    const guard = previous?.scrollbarPosition;
    // A second retirement can precede the first usable measurement. Keep the
    // outstanding handoff until it is consumed or native movement supersedes it.
    if (previous && (!viewport.clientHeight || pendingReadingHandoff(previous, view, viewport) &&
      (!guard || guard.viewport.scrollTop === guard.top))) return;
    const top = viewport.scrollTop;
    const operation = position.current;
    const correction = operation?.kind === "history" && operation.view === view && operation.viewport === viewport ? operation : null;
    // Rebind a pending anchor only while its own position witness still
    // holds. A second handoff must not make an obsolete anchor current again.
    const current = correction && (correction.expectedTop === undefined || correction.expectedTop === top) ? correction : null;
    const anchor = current ? { key: current.key, offset: current.offset, textOffset: current.textOffset }
      : viewport.clientHeight ? captureReadingAnchor(viewport) : null;
    readingHandoff.current = { view, anchor,
      physical: { viewport, top: current?.physicalTop ?? top },
      scrollbarPosition: scrollbar || guard || correction?.expectedTop !== undefined ? { viewport, top } : undefined };
  }, []);
  const setScroller = useCallback((element: HTMLElement | Window | null) => {
    const next = element instanceof HTMLElement ? element : null;
    const previous = scroller.current;
    if (previous === next) return;
    setPagingReady(false);
    setOlderStalled(false);
    const view = currentView.current;
    if (previous && view && currentViewportView(view)) {
      const keepReading = !following.current || navigationContact.current?.source === "scrollbar";
      if (keepReading) saveReadingHandoff(view, previous);
      else readingHandoff.current = null;
    }
    retireSelection(previous);
    if (previous) resetPausedOutput(!following.current);
    cancelFollow();
    retirePosition();
    retireWheel();
    scrollNavigation.current = null;
    viewportObserver.current?.disconnect();
    viewportObserver.current = null;
    contentObserver.current?.disconnect();
    contentObserver.current = null;
    if (navigationContact.current) navigationContact.current.invalid = true;
    scroller.current = next;
    const handoff = readingHandoff.current;
    const guard = handoff?.scrollbarPosition;
    if (next && handoff && guard && guard.viewport !== next) {
      const scrollbarPosition = { viewport: next, top: next.scrollTop };
      if (guard.viewport.scrollTop === guard.top) handoff.scrollbarPosition = scrollbarPosition;
      else readingHandoff.current = { view: handoff.view, anchor: null, physical: scrollbarPosition, scrollbarPosition };
    }
    const viewport = scroller.current;
    lastScroll.current = viewport ? sampleScroll(viewport) : { top: 0, maximum: 0 };
    if (next) {
      // Virtuoso publishes this ref after layout effects. Attach to the
      // actual node, even when a replacement commits while chat is hidden.
      if (initialPresentation.current.phase !== "incomplete") {
        if (following.current) startInitialPosition(next);
        else if (view) restoreReading(view, next);
      }
      viewportObserver.current = new ResizeObserver(measuredOutput);
      viewportObserver.current.observe(next);
      contentObserver.current = new MutationObserver((records) => {
        if (scrollNavigation.current?.contact?.source === "scrollbar" || changesScrollLayout(next, records)) {
          rebaseContactAfterLayout(next);
        }
      });
    }
  }, [cancelFollow, resetPausedOutput, retirePosition, retireSelection, retireWheel, startInitialPosition, measuredOutput, currentViewportView, rebaseContactAfterLayout,
    saveReadingHandoff, restoreReading]);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const olderRead = useRef<ReturnType<typeof observeOlderMessages> | null>(null);
  const hasOlder = useSessionStore((s) =>
    activeSessionId ? Boolean(s.hasMoreMessages[activeSessionId]) : false
  );
  useLayoutEffect(() => useSessionStore.subscribe((state, before) => {
    const sid = activeSessionId;
    // Hiding chat retires viewport work but keeps the same conversation opening.
    const sameOpening = state.accountGeneration === before.accountGeneration &&
      (state.activeSessionId === before.activeSessionId || state.activeSessionId === sid && staging.current?.committing);
    const messageWindow = sid ? state.messageWindows[sid] : undefined;
    const windowChanged = messageWindow !== (sid ? before.messageWindows[sid] : undefined);
    const wasChat = before.mainView === "chat";
    const view = currentView.current;
    const viewport = scroller.current;
    if (sameOpening && wasChat === (state.mainView === "chat") && !windowChanged) {
      const messagesChanged = sid && state.messages[sid] !== before.messages[sid];
      // The old DOM can still certify movement during publication, including
      // for cards that subscribe directly to the store.
      if (view && viewport) settleContactBeforeLayout(viewport, view);
      const outputChanged = sid && (messagesChanged || state.streamingText[sid] !== before.streamingText[sid] ||
        state.streamingItems[sid] !== before.streamingItems[sid] || state.subagents[sid] !== before.subagents[sid]);
      if (!sid || !outputChanged || !view || !viewport || !currentViewportView(view) ||
        (following.current && !selectionPaused(viewport))) return;
      const transfers = matchTranscriptTransfers(sid, state, before);
      if (pausedOutput.current) recordTranscriptOutput(pausedOutput.current, sid, state, before, projectedMessages.current, transfers);
      if (messagesChanged && !position.current && !navigationContact.current && !document.hidden && viewport.clientHeight) {
        const bounds = viewport.getBoundingClientRect();
        const oldRows = before.messages[sid];
        const rows = state.messages[sid];
        const readingRow = visibleReadingRow(viewport);
        const selected = transcriptSelection(viewport);
        // Match each actual preview to the saved row taking its place. The
        // selected live item takes priority over other visible rows.
        for (const preview of viewport.querySelectorAll<HTMLElement>("[data-live-output]")) {
          const body = preview.querySelector<HTMLElement>(".msg-streaming-text");
          if (!body) continue;
          const selection = selected ? captureTransferredSelection(body, selected, bounds.top) : undefined;
          const box = preview.getBoundingClientRect();
          if (!selection && (readingRow || selected || box.bottom <= bounds.top || box.top >= bounds.bottom)) continue;
          const transfer = transfers.get(preview.dataset.liveOutput!);
          if (!transfer || body.textContent !== transfer.displayed) continue;
          const saved = transfer.message;
          const savedSelection = selection && { ...selection, projected: projectMarkdownOutput(saved.content!, {
            ...selection.source, end: Math.min(selection.source.end, transfer.previous.length),
          }, "selection") };
          position.current = { kind: "history", view, viewport, frame: null, key: chatMessageKey(saved),
            offset: transcriptTextTop(preview) - bounds.top, textOffset: true, selected: savedSelection, physicalTop: viewport.scrollTop,
            alignOnMount: true };
          // The reader can be below the estimated range for the new row.
          setTransferOverscan(Math.max(0, bounds.top - box.top));
          break;
        }
        // A continuous reconnect can fill a sequence gap before a mounted
        // reader without changing the window or invoking a prepend callback.
        if (!position.current && readingRow && oldRows && rows && state.oldestSeq[sid] === before.oldestSeq[sid]) {
          const seq = renderedMessages.get(readingRow)?.seq;
          const index = seq === undefined ? -1 : oldRows.findIndex((row) => row.seq === seq);
          const nextIndex = seq === undefined ? -1 : rows.findIndex((row) => row.seq === seq);
          if (index >= 0 && nextIndex > index) {
            position.current = { kind: "history", view, viewport, frame: null, key: readingRow.dataset.messageKey!,
              offset: readingRow.getBoundingClientRect().top - bounds.top, physicalTop: viewport.scrollTop, alignOnMount: true };
          }
        }
      }
      return;
    }
    // Retire synchronously at the store boundary, before React can batch a
    // departure and return into identical selected values.
    pausePreparation(initialPresentation.current);
    if (!sameOpening || windowChanged) {
      setPagingReady(false);
      setOlderStalled(false);
    }
    if (!sameOpening) {
      focusReading.current = null;
      retireRecovery();
      readingHandoff.current = null;
      following.current = true;
      setReadingBoundary(false);
      setScrolledIntoHistory(false);
      initialPresentation.current = newPresentation();
      publishPresentation(initialPresentation.current, "preparing");
      navigationContact.current = null;
    } else if (wasChat && view && viewport &&
      (!following.current || navigationContact.current?.source === "scrollbar")) {
      saveReadingHandoff(view, viewport);
    }
    if (sameOpening && windowChanged && following.current && !initialPresentation.current.timedOut &&
      initialPresentation.current.phase !== "preparing" && initialPresentation.current.phase !== "incomplete") {
      publishPresentation(initialPresentation.current, "preparing");
    }
    retireSelection(viewport);
    resetPausedOutput(sameOpening && !following.current);
    currentView.current = null;
    cancelFollow(); retirePosition(); retireWheel();
    scrollNavigation.current = null;
    if (navigationContact.current) navigationContact.current.invalid = true;
    olderRead.current?.cancel(); olderRead.current = null;
    setViewportView({ sessionId: sid, account: state.accountGeneration, window: messageWindow });
  }), [activeSessionId, cancelFollow, currentViewportView, retirePosition, publishPresentation, retireRecovery, retireWheel, saveReadingHandoff,
    resetPausedOutput, retireSelection, selectionPaused, settleContactBeforeLayout]);
  useLayoutEffect(() => {
    currentView.current = viewportView;
    cancelFollow();
    retirePosition();
    setLoadingOlder(false);
    setAtTop(false);
    const viewport = scroller.current;
    if (viewport) lastScroll.current = sampleScroll(viewport);
    const phase = initialPresentation.current.phase;
    if (viewport && phase !== "incomplete") {
      if (!following.current) restoreReading(viewportView, viewport);
      else if (phase === "preparing" || phase === "revealing") startInitialPosition(viewport);
      measuredOutput();
    }
    return () => {
      cancelFollow(viewportView);
      if (position.current?.view === viewportView) retirePosition(position.current);
      if (currentView.current === viewportView) {
        pausePreparation(initialPresentation.current);
        retireWheel();
        currentView.current = null;
        readingHandoff.current = null;
        scrollNavigation.current = null; navigationContact.current = null;
        olderRead.current?.cancel(); olderRead.current = null;
      }
    };
  }, [viewportView, cancelFollow, retirePosition, retireWheel, startInitialPosition, restoreReading, measuredOutput]);
  const loadOlder = useCallback(() => {
    const sid = activeSessionId;
    const view = currentView.current;
    if (!sid || !view || view.sessionId !== sid || !currentViewportView(view) || olderRead.current?.current()) return;
    pauseFollowing();
    setAtTop(false);
    setLoadingOlder(true);
    let mergedPosition: ViewportPosition | null = null;
    const read = observeOlderMessages(sid, () => {
      if (!currentViewportView(view)) return;
      setAtTop(false);
      const element = scroller.current;
      if (!element?.clientHeight) return;
      // The incoming page may commit before an existing scroll is reported.
      if (!settleContactBeforeLayout(element, view) && !following.current) scrollNavigation.current = null;
      if (following.current && !selectionPaused(element)) return;
      const anchor = captureReadingAnchor(element);
      retirePosition();
      const top = element.scrollTop;
      mergedPosition = anchor ? { kind: "history", view, viewport: element, ...anchor, frame: null, physicalTop: top,
        expectedTop: navigationContact.current?.source === "scrollbar" ? top : undefined } : null;
      position.current = mergedPosition;
    });
    olderRead.current = read;
    void read.done.then((result) => {
      // Store publication and this view's request status have different lives.
      // An obsolete connection must release its own spinner, while an old view
      // can never settle a replacement view's request.
      if (olderRead.current !== read || !currentViewportView(view)) return;
      olderRead.current = null;
      setLoadingOlder(false);
      if (!read.current() || result.state === "obsolete") {
        retirePosition(mergedPosition);
        setOlderStalled(true);
        return;
      }
      if (result.state === "failed" || result.state === "in_flight" || result.state === "merged" && result.added === 0) {
        retirePosition(mergedPosition);
        setOlderStalled(true);
      }
    });
  }, [activeSessionId, pauseFollowing, retirePosition, currentViewportView, selectionPaused, settleContactBeforeLayout]);

  useLayoutEffect(() => {
    committed.current = { messages, subagentSnapshots, activeRequestId, pendingQuestions };
    projectedMessages.current = chatMessages;
    positionMeasuredOutput();
    measurePausedOutput();
  }, [messages, subagentSnapshots, activeRequestId, pendingQuestions, chatMessages, loadingOlder, transcriptBaseline,
    positionMeasuredOutput, measurePausedOutput]);

  useLayoutEffect(() => {
    const request = focusReading.current;
    if (!request) return;
    if (request.owner !== initialPresentation.current) { focusReading.current = null; return; }
    if (presentation.owner !== request.owner || presentation.phase !== "reading") return;
    focusReading.current = null;
    // Removing the overlay normally leaves focus on body. A later explicit
    // focus choice (especially the composer) owns itself and must be preserved.
    if (document.activeElement === request.source || !request.source.isConnected && document.activeElement === document.body) {
      scroller.current?.focus({ preventScroll: true });
    }
  }, [presentation]);

  useLayoutEffect(() => {
    if (transcriptDamaged || !transcriptBaseline && transcriptReadError) {
      failPreparation(initialPresentation.current, transcriptDamaged ? "Conversation history is damaged." : transcriptReadError!);
    } else if (transcriptBaseline) {
      const owner = initialPresentation.current;
      if (owner.phase === "incomplete") {
        // A successful reread repairs data availability, not positioning proof.
        owner.error = undefined;
        publishPresentation(owner, "reading");
      }
      if (owner.timedOut) measuredOutput();
    }
  }, [transcriptBaseline, transcriptReadError, transcriptDamaged, presentation.owner, failPreparation, measuredOutput, publishPresentation]);

  useLayoutEffect(() => {
    const owner = presentation.owner;
    if (owner !== initialPresentation.current || presentation.phase !== "revealing" || owner.phase !== "revealing") return;
    const operation = position.current;
    if (operation?.kind !== "initial" || !currentPosition(operation)) return;
    const viewport = operation.viewport;
    const sessionId = operation.view.sessionId;
    const positioned = !!sessionId && !document.hidden && viewport.clientHeight > 0 &&
      measureInitialTail(viewport, projectedMessages.current.at(-1), sessionId) === "readable";
    if (!positioned) {
      publishPresentation(owner, "preparing");
      positionMeasuredOutput();
      return;
    }
    if (preparation) {
      positionMeasuredOutput();
      return;
    }
    pausePreparation(owner);
    publishPresentation(owner, "ready");
    setAtTop(atLoadedStart(viewport, projectedMessages.current[0]));
    setPagingReady(viewport.scrollTop > 4);
    positionMeasuredOutput();
  }, [preparation, presentation, chatMessages, currentPosition, publishPresentation, positionMeasuredOutput]);

  // Physical scroll events supply current top geometry. An estimated initial
  // range or a delayed library top report cannot arm history by itself.
  useEffect(() => {
    const viewport = scroller.current;
    const view = currentView.current;
    if (atTop && pagingReady && hasOlder && !loadingOlder && !olderStalled && !position.current &&
        view && viewport && viewport.clientHeight > 0 && !pendingReadingHandoff(readingHandoff.current, view, viewport) &&
        atLoadedStart(viewport, projectedMessages.current[0])) {
      loadOlder();
    }
  }, [atTop, pagingReady, hasOlder, loadingOlder, olderStalled, loadOlder]);

  // The body per message shape. Wrapped by `renderMessage` below, which is
  // where the hover-revealed timestamp is attached — one place, so every shape
  // gets it rather than each branch of MessageBubble growing its own.
  const renderMessageBody = useCallback(
    (_index: number, msg: Message) => {
      if (msg.type === "question_request") {
        const pending = pendingQuestions.find(
          (q) => matchesPendingQuestion(msg, q)
        );
        if (pending && activeSessionId) {
          return (
            <QuestionPrompt
              question={pending}
              sessionId={activeSessionId}
              agentLabel={agentLabel}
              onSnooze={snoozeQuestion ? (id, target) => snoozeQuestion(activeSessionId, id, target) : undefined}
              onSubmit={(id, answers, target) =>
                answerQuestion(activeSessionId, id, answers, target)
              }
            />
          );
        }
        // Already answered or no live state — render a compact summary so the
        // chat history shows what was asked.
        const questions =
          (msg.tool_input?.questions as PendingQuestion["questions"]) || [];
        return (
          <div className="msg msg-question msg-question-done rounded-lg border-[0.7px] border-dashed border-border bg-muted/30 overflow-hidden opacity-75">
            <div className="question-header flex items-center gap-2 px-3 py-2 text-sm text-muted-foreground">
              <span aria-hidden className="text-xs">?</span>
              <strong>{agentLabel} asked</strong>
              {msg.tool_input?.interaction_policy === "human" && msg.tool_input?.wait_mode === "blocking" &&
                <span>Human blocking question · no automatic question deadline</span>}
              {msg.tool_input?.interaction_policy === "human" && msg.tool_input?.wait_mode === "optional" &&
                <span>Human optional question</span>}
              {typeof msg.tool_input?.deadline === "string" && <span>Original question deadline: {msg.tool_input.deadline}</span>}
            </div>
            <div className="question-body px-3 pb-3 space-y-1">
              {questions.map((q, i) => (
                <div className="question-item" key={i}>
                  <div className="question-text text-sm text-foreground leading-relaxed">
                    {q.question}
                  </div>
                </div>
              ))}
            </div>
          </div>
        );
      }
      return (
        <MessageBubble
          message={msg}
          sessionId={activeSessionId ?? ""}
          agentName={activeAgent?.name}
          agentAvatar={activeAgent?.avatar}
          onFork={onFork}
          onRestoreInput={onRestoreInput}
          restoreInputDisabled={restoreInputDisabled}
        />
      );
    },
    [
      activeSessionId,
      answerQuestion,
      snoozeQuestion,
      pendingQuestions,
      onFork,
      onRestoreInput,
      restoreInputDisabled,
      activeAgent?.name,
      activeAgent?.avatar,
      agentLabel,
    ]
  );

  const renderMessage = useCallback(
    (index: number, msg: Message) => {
      const body = msg.type === "question_request" && !pendingQuestions.some((question) => matchesPendingQuestion(msg, question))
        ? <MessageDetails sessionId={activeSessionId!} message={msg}>{(full) => full === msg
          ? renderMessageBody(index, msg)
          : <SavedMessageRecord message={full} label="Full saved question record" />}</MessageDetails>
        : renderMessageBody(index, msg);
      if (body === null) return body;
      return (
        <div className="msg-row group/msg relative" data-message-key={chatMessageKey(msg)} ref={(row) => {
          if (!row) return;
          renderedMessages.set(row, msg);
          alignCommittedReadingRow(row);
          if (msg === projectedMessages.current.at(-1) && position.current?.kind === "initial") positionMeasuredOutput();
        }}>
          {body}
          <MessageTime iso={msg.created_at} />
        </div>
      );
    },
    [renderMessageBody, pendingQuestions, activeSessionId, alignCommittedReadingRow, positionMeasuredOutput]
  );

  // While a turn runs: the text the model is producing right now, or the
  // loading dots when it hasn't produced any yet (it's thinking, or running a
  // tool). Rendered through MessageBubble so a partial answer looks exactly
  // like the finished one and doesn't jump when it's replaced — it carries no
  // `seq`, so the fork affordance stays off it.
  // What the top of a windowed transcript says. Silent when the whole thing is
  // loaded, so an ordinary short conversation looks exactly as it did.
  const olderHeader = useCallback(() => {
    if (!hasOlder) return null;
    return (
      <div className="chat-older-notice px-4 py-3 text-center text-xs text-muted-foreground">
        {loadingOlder ? (
          <span className="chat-older-loading">Loading earlier messages…</span>
        ) : (
          <button
            type="button"
            className="btn-load-older rounded-md px-2 py-1 hover:bg-accent hover:text-foreground"
            onClick={loadOlder}
          >
            Load earlier messages
          </button>
        )}
      </div>
    );
  }, [hasOlder, loadingOlder, loadOlder]);

  const footerContext = useMemo<ChatFooterContext>(() => ({
    sessionId: activeSessionId, running: isRunning, agentName: activeAgent?.name,
    agentAvatar: activeAgent?.avatar, onLayout: measuredOutput,
  }), [activeSessionId, isRunning, activeAgent?.name, activeAgent?.avatar, measuredOutput]);

  const showReadingNotice = readingBoundary || presentation.showTimeoutNotice;
  const showJumpToLatest = showReadingNotice || (scrolledIntoHistory || outputOutside) && transcriptBaseline && !transcriptDamaged;
  return <div className="contents">
      <div className={showReadingNotice ? "shrink-0 px-4 py-2 text-sm text-muted-foreground" : "relative h-0 shrink-0"}>
        <p role="status" aria-atomic="true" className={showReadingNotice ? undefined : "sr-only"}
          data-chat-timeout={presentation.timeoutHint || undefined}>
          {presentation.timeoutHint && "Automatic positioning did not finish. Loaded content is available."}
        </p>
        {presentation.timeoutHint && <>
          {presentation.offerTimeoutRecovery && <p>New output will return to the latest content unless you choose to read here.</p>}
          <Button size="sm" variant="ghost" aria-label="Dismiss positioning notice" onClick={() => {
            presentation.owner.timeoutHint = false;
            publishPresentation(presentation.owner, presentation.owner.phase);
          }}>Dismiss</Button>
        </>}
        {readingBoundary && <p role="status">The previous reading position is unavailable. Showing loaded content.</p>}
        {showJumpToLatest && <Button size="sm" variant={showReadingNotice ? "ghost" : "outline"}
          className={showReadingNotice ? undefined : "absolute left-4 top-2 z-10"}
          onClick={() => {
            retireSelection(scroller.current);
            scroller.current?.focus({ preventScroll: true });
            resumeVisibleFollowing();
          }}>Jump to latest</Button>}
      </div>
      {transcriptDamaged && activeSessionId && <TranscriptIntegrityNotice key={activeSessionId} sessionId={activeSessionId} />}

      <div className="relative flex flex-1 min-h-0" style={minimumViewportHeight ? { minHeight: minimumViewportHeight } : undefined}
        onClickCapture={pauseForReading}
        onWheelCapture={(event) => {
          settleWheel();
          const viewport = scroller.current;
          const view = currentView.current;
          if (!viewport || !view || !currentViewportView(view) || document.hidden ||
            pendingWheel.current || event.defaultPrevented || !event.deltaY || event.ctrlKey) return;
          // Composited wheel scrolling may move the DOM before React receives
          // its passive capture event. Keep the pre-scroll bound as evidence.
          const up = event.deltaY < 0;
          const from = up ? Math.max(viewport.scrollTop, lastScroll.current.top) : Math.min(viewport.scrollTop, lastScroll.current.top);
          if (!canScrollChat(viewport, event.target, up, from)) return;
          if (!event.cancelable) { navigate(up, from); return; }
          // The native event is still in dispatch. A descendant may cancel
          // it after capture, including after a microtask in a trusted event.
          const { top, maximum } = sampleScroll(viewport);
          const wheel: PendingWheel = { view, viewport, event: event.nativeEvent, from, maximum, atBottom: maximum - top <= 4,
            timer: window.setTimeout(() => settleWheel(wheel), 0) };
          pendingWheel.current = wheel;
          if (scrollNavigation.current?.contact !== navigationContact.current) scrollNavigation.current = null;
          suspendViewportMotion();
          preparationBudget();
        }}
        onKeyDown={(event) => {
          if (event.defaultPrevented || event.altKey || event.metaKey || event.ctrlKey && !["Home", "End"].includes(event.key)) return;
          const up = ["ArrowUp", "PageUp", "Home"].includes(event.key) || event.key === " " && event.shiftKey;
          const down = ["ArrowDown", "PageDown", "End"].includes(event.key) || event.key === " " && !event.shiftKey;
          if ((!up && !down) || !canScrollChat(scroller.current, event.target, up)) return;
          const target = event.target instanceof HTMLElement ? event.target : null;
          if (target?.isContentEditable || target?.closest("input, textarea, select, [role=textbox]")) return;
          if (event.key === " " && target?.closest("button, a[href], summary, [role=button]")) return;
          const viewport = scroller.current!;
          const view = currentView.current;
          if (!navigate(up, viewport.scrollTop)) return;
          if (event.key === "Home" && virtuoso.current) {
            // Native Home animates across estimated rows and can be interrupted
            // when Virtuoso measures them. Open the loaded start directly.
            event.preventDefault();
            virtuoso.current.scrollTo({ top: 0, behavior: "instant" });
            if (view && viewport === scroller.current) observeUpwardScroll(viewport, view, sampleScroll(viewport), "home");
          }
        }}
        onPointerDownCapture={(event) => {
          settleWheel();
          const viewport = scroller.current;
          const touchOrPen = event.pointerType === "touch" || event.pointerType === "pen";
          let scrollbar = false;
          if (viewport && !touchOrPen && event.button === 0 && event.target === viewport &&
            (canScrollChat(viewport, viewport, true) || canScrollChat(viewport, viewport, false))) {
            const left = viewport.getBoundingClientRect().left + viewport.clientLeft;
            scrollbar = event.clientX < left || event.clientX >= left + viewport.clientWidth;
          }
          const contact = navigationContact.current;
          if (scrollbar && (contact || event.isPrimary)) {
            const correction = position.current;
            if (correction?.kind === "history" && currentPosition(correction)) {
              // Snapshot the position before native movement. Never rebase
              // a correction that was already guarded by an earlier contact.
              correction.expectedTop ??= correction.viewport.scrollTop;
            }
            const view = currentView.current;
            const handoff = readingHandoff.current;
            if (handoff && viewport && view && currentViewportView(view) && pendingReadingHandoff(handoff, view, viewport)) {
              handoff.scrollbarPosition ??= { viewport, top: viewport.scrollTop };
            }
          }
          if (contact) {
            if (scrollbar && contact.source !== "scrollbar") {
              contact.source = "scrollbar";
              contact.id = event.pointerId;
              contact.y = event.clientY;
            }
            return;
          }
          scrollNavigation.current = null;
          if (event.isPrimary && (touchOrPen || scrollbar)) {
            startNavigationContact(scrollbar ? "scrollbar" : "pointer", event.pointerId, event.clientY, event.pointerType);
          }
        }}
        onPointerMoveCapture={(event) => {
          const pointer = navigationContact.current;
          if (!pointer || pointer.source === "touch" || pointer.id !== event.pointerId) return;
          if (pointer.source === "scrollbar") {
            if (event.clientY !== pointer.y) pauseForScrollbar(pointer);
            return;
          }
          if (event.clientY === pointer.y) return;
          const up = event.clientY > pointer.y;
          pointer.y = event.clientY;
          considerContactNavigation(pointer, event.target, up);
        }}
        onTouchStartCapture={(event) => {
          settleWheel();
          const viewport = scroller.current;
          if (!viewport || !event.touches.length) return;
          const touch = event.touches[0];
          if (!navigationContact.current) startNavigationContact("touch", touch.identifier, touch.clientY);
          const contact = navigationContact.current;
          if (!contact) return;
          if (!contact.touch) {
            // TouchEvent owns only touch pointers after takeover. A held
            // scrollbar or pen still owns its own release.
            if (contact.source === "pointer" && contact.pointers.get(contact.id) === "touch") {
              contact.source = "touch";
            } else if (contact.source !== "touch") contact.invalid = true;
            contact.touch = { id: touch.identifier, y: touch.clientY, multiple: false };
            for (const [id, type] of contact.pointers) {
              if (type === "touch") contact.pointers.delete(id);
            }
          }
          if (event.touches.length > 1) {
            contact.touch.multiple = true;
            contact.invalid = true;
          }
        }}
        onTouchMoveCapture={(event) => {
          const contact = navigationContact.current;
          if (!contact?.touch) return;
          if (event.touches.length !== 1) {
            contact.touch.multiple = true;
            invalidateNavigationContact(contact);
            return;
          }
          const touch = event.touches[0];
          const gesture = contact.touch;
          if (gesture.multiple || touch.identifier !== gesture.id || touch.clientY === gesture.y) return;
          const up = touch.clientY > gesture.y;
          gesture.y = touch.clientY;
          if (contact.source === "touch") considerContactNavigation(contact, event.target, up);
        }}
      >
      <HelperOutputLayout.Provider value={measurePausedOutput}>
      <Virtuoso
        // Apply the first row measurements within their layout cycle. A
        // deferred range update can otherwise move the tail after reveal.
        skipAnimationFrameInResizeObserver={presentation.phase === "preparing" || presentation.phase === "revealing"}
        style={{ visibility: presentation.phase === "preparing" || presentation.phase === "incomplete" ? "hidden" : undefined }}
        inert={presentation.phase === "preparing" || presentation.phase === "incomplete" ? true : undefined}
        aria-hidden={presentation.phase === "preparing" || presentation.phase === "incomplete" ? true : undefined}
        defaultItemHeight={DEFAULT_CHAT_ROW_HEIGHT}
        ref={virtuoso}
        scrollerRef={setScroller}
        key={`transcript:${activeSessionId}`}
        className="chat-messages flex-1 min-h-0"
        data={chatMessages}
        itemContent={renderMessage}
        computeItemKey={(_index, message) => chatMessageKey(message)}
        itemsRendered={onItemsRendered}
        onScrollEnd={(event) => {
          const view = currentView.current;
          if (navigationContact.current || event.currentTarget !== scroller.current || !view || !currentViewportView(view)) return;
          scrollNavigation.current = null;
          contentObserver.current?.disconnect();
        }}
        onScroll={(event) => {
          const viewport = event.currentTarget;
          const view = currentView.current;
          if (viewport !== scroller.current || !viewport.clientHeight || !view || !currentViewportView(view)) return;
          settleWheel();
          if (viewport !== scroller.current || !currentViewportView(view)) return;
          const sample = sampleScroll(viewport);
          const { top } = sample;
          const bottom = sample.maximum - top <= 4;
          observeReleasedContactScroll(viewport, view, sample);
          const correction = position.current;
          if (!navigationContact.current && correction && currentPosition(correction) &&
            (correction.kind === "history" && correction.expectedTop !== undefined && correction.expectedTop !== top ||
              correction.kind === "restore" && correction.handoff === readingHandoff.current &&
              movedFromScrollbarPosition(correction.handoff, viewport))) {
            // Give the same native scroll report to paging after yielding
            // the old correction, even if no later scroll event arrives.
            if (correction.kind === "restore") readingHandoff.current = null;
            retirePosition(correction);
          }
          observeUpwardScroll(viewport, view, sample);
          sampleHeldContact(viewport, view, sample, true);
          const navigation = scrollNavigation.current;
          if (!position.current && !navigationContact.current && navigation?.direction === "down" && !navigation.contact) {
            if (navigation.viewport !== viewport || performance.now() > navigation.until ||
              !currentViewportView(navigation.view) || top < lastScroll.current.top) {
              scrollNavigation.current = null;
            } else if (top > lastScroll.current.top) {
              navigation.until = performance.now() + 500;
              if (bottom && top > navigation.top) {
                resumeVisibleFollowing();
              }
            }
          }
          const handoff = readingHandoff.current;
          const restoreReport = restoredReadingAt(handoff, view, viewport);
          const held = navigationContact.current;
          const awaitingTouchRelease = held && held.source !== "scrollbar" && (!navigation || navigation.contact);
          if (!awaitingTouchRelease && !position.current && !restoreReport && !pendingReadingHandoff(handoff, view, viewport)) {
            reportPagingPosition(viewport);
            if (!following.current) {
              const anchor = captureReadingAnchor(viewport);
              readingHandoff.current = { view, anchor };
            }
          }
          lastScroll.current = sample;
        }}
        totalListHeightChanged={measuredOutput}
        followOutput={false}
        increaseViewportBy={{ top: 400 + transferOverscan, bottom: 400 }}
        components={{ Header: olderHeader, List: ChatRows, Footer: ChatFooter }}
        context={footerContext}
      />
      </HelperOutputLayout.Provider>

      {(presentation.phase === "preparing" || presentation.phase === "incomplete") && <div data-chat-initial-surface
        data-chat-preparation={!transcriptBaseline || presentation.phase === "incomplete" || undefined}
        tabIndex={0} aria-label="Conversation preparation"
        className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-4 text-sm text-muted-foreground">
        {(presentation.phase === "incomplete" || !transcriptBaseline) && <p role="status">
          {presentation.phase === "incomplete" ? "Conversation unavailable" : "Loading conversation…"}
        </p>}
        {presentation.error && <p>{presentation.error}</p>}
        <Button size="sm" variant="outline" className={transcriptBaseline && presentation.phase !== "incomplete" ? "sr-only focus:not-sr-only" : undefined}
          onClick={() => pauseFollowing()}>Browse loaded content</Button>
        {presentation.phase === "incomplete" && <Button size="sm" variant="outline" onClick={retryHistoryRead}>Retry loading</Button>}
      </div>}
      </div>

      <div aria-label="Questions outside the history window" className="max-h-[40vh] shrink-0 overflow-y-auto empty:hidden"
        onClickCapture={pauseForReading}>
      {pendingQuestions.filter((question) => !chatMessages.some((message) =>
        matchesPendingQuestion(message, question))).map((question) =>
        <div key={`${activeSessionId}:${question.question_id}:${question.attempt_id}`} className="px-4 py-2">
          <QuestionPrompt sessionId={activeSessionId!} question={question} agentLabel={agentLabel}
            onSnooze={snoozeQuestion ? (id, target) => snoozeQuestion(activeSessionId!, id, target) : undefined}
            onSubmit={(id, answers, target) => answerQuestion(activeSessionId!, id, answers, target)} />
        </div>)}
      </div>

    </div>;
}

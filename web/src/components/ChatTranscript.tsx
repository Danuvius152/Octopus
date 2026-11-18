import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { Virtuoso, type ListProps, type VirtuosoHandle } from "react-virtuoso";
import { hasEmptyTranscript, hasTranscriptBaseline, observeAcceptedText, useSessionStore, type Message, type PendingQuestion,
  type SessionInfo, type StreamingItem, type SubagentRun } from "../stores/sessionStore";
import { observeOlderMessages } from "../lib/transcript";
import { observeSession } from "../lib/hydrateSession";
import { matchesPendingQuestion } from "../lib/questionPresentation";
import { isNativeChild, nativeChatMessages, nativeItemKey } from "../lib/nativeOutput";
import type { QuestionCommandTarget, SubmissionReply } from "../lib/submission";
import { Button } from "./ui/button";
import { MessageBubble } from "./MessageBubble";
import { MessageDetails, SavedMessageRecord } from "./MessageDetails";
import { MessageTime } from "./MessageTime";
import { QuestionPrompt, type AnswerPayload } from "./QuestionPrompt";
import { TranscriptIntegrityNotice } from "./TranscriptIntegrityNotice";

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
}

function captureReadingAnchor(viewport: HTMLElement): ReadingAnchor | null {
  const bounds = viewport.getBoundingClientRect();
  const visible = Array.from(viewport.querySelectorAll<HTMLElement>("[data-message-key]")).find((element) => {
    const rectangle = element.getBoundingClientRect();
    return rectangle.bottom > bounds.top && rectangle.top < bounds.bottom;
  });
  return visible ? { key: visible.dataset.messageKey!, offset: visible.getBoundingClientRect().top - bounds.top } : null;
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

function latestContentInView(viewport: HTMLElement, last: Message | undefined, sessionId: string): boolean {
  const bounds = viewport.getBoundingClientRect();
  const footer = viewport.querySelector<HTMLElement>("[data-chat-footer]");
  const state = useSessionStore.getState();
  const session = state.sessions.find((row) => row.id === sessionId) ??
    state.archivedSessions.find((row) => row.id === sessionId);
  const live = footer && renderedFooters.get(footer);
  if (session?.status === "running") {
    if (!live?.running || live.sessionId !== sessionId ||
      live.text !== (state.streamingText[sessionId]?.content ?? "") ||
      live.items !== (state.streamingItems[sessionId] ?? EMPTY_STREAMING_ITEMS)) return false;
  } else if (live?.running) return false;
  const footerBox = footer?.parentElement?.getBoundingClientRect();
  if (viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop > 1) return false;
  const inView = (box: DOMRect) => box.height > 0 && box.bottom > bounds.top && box.bottom <= bounds.top + viewport.clientHeight + 1;
  if (footerBox?.height) {
    if (!inView(footerBox)) return false;
    if (!last) return true;
    // A full-screen Footer can be readable with every saved row virtualized.
    // The list still needs to have committed its space before we expose it.
    if (footerBox.top <= bounds.top) return !!viewport.querySelector("[data-chat-rows]")?.getBoundingClientRect().height;
  } else if (footer?.childElementCount) return false;
  if (!last) return true; // authoritative empty window
  const row = [...viewport.querySelectorAll<HTMLElement>("[data-message-key]")].at(-1);
  return !!row?.parentElement && renderedMessages.get(row) === last && inView(row.parentElement.getBoundingClientRect());
}

function positionAtPhysicalTail(viewport: HTMLElement, list: VirtuosoHandle | null): boolean {
  const bottom = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
  if (bottom - viewport.scrollTop <= 1) return true;
  list?.scrollTo({ top: bottom, behavior: "auto" });
  return false;
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
  ({ kind: "history"; physicalTop: number; expectedTop?: number; alignedHeight?: number } & ReadingAnchor));
function alignReadingAnchor(operation: Extract<ViewportPosition, { kind: "history" }>, element: HTMLElement) {
  const { viewport } = operation;
  const top = operation.textOffset ? transcriptTextTop(element) : element.getBoundingClientRect().top;
  const delta = top - viewport.getBoundingClientRect().top - operation.offset;
  if (Math.abs(delta) <= 1) return "aligned";
  operation.alignedHeight = undefined;
  const before = viewport.scrollTop;
  viewport.scrollTop += delta;
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
  top: number;
  invalid: boolean;
  pointers: Map<number, string>;
  touch: { id: number; y: number; multiple: boolean } | null;
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
  const initialPresentation = useRef<InitialPresentation>(newPresentation(!preparation && empty ? "ready" : "preparing"));
  const [presentation, setPresentation] = useState(() => ({ owner: initialPresentation.current, phase: initialPresentation.current.phase }));
  const publishPresentation = useCallback((owner: InitialPresentation, phase: InitialPresentation["phase"]) => {
    if (owner !== initialPresentation.current) return;
    owner.phase = phase;
    setPresentation({ owner, phase });
  }, []);
  const position = useRef<ViewportPosition | null>(null);
  const [transferOverscan, setTransferOverscan] = useState(0);
  const projectedMessages = useRef(chatMessages);
  // Input retires follow intent before queued layout work can overwrite it.
  // Position alone cannot distinguish native navigation from virtual layout.
  const following = useRef(true);
  const followFrame = useRef<(ViewportScope & { frame: number; time: number }) | null>(null);
  const reducedMotion = useRef<MediaQueryList | null>(null);
  const viewportObserver = useRef<ResizeObserver | null>(null);
  const lastScrollTop = useRef(0);
  // After contact release, native scroll evidence ends at scrollend or 500ms
  // without forward progress. An active contact owns its complete input stream;
  // neither its ordinary scroll notifications nor elapsed time restore follow.
  const downwardNavigation = useRef<(ViewportScope & { top: number; until: number; contact?: NavigationContact }) | null>(null);
  const navigationContact = useRef<NavigationContact | null>(null);
  const focusReading = useRef<{ owner: InitialPresentation; source: Element } | null>(null);
  const cancelFollow = useCallback((view?: ChatViewportView) => {
    const work = followFrame.current;
    if (!work || view && work.view !== view) return;
    cancelAnimationFrame(work.frame);
    followFrame.current = null;
  }, []);
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
  const alignTransferredRow = useCallback((element: HTMLDivElement | null) => {
    const operation = position.current;
    if (!element || operation?.kind !== "history" || !operation.textOffset ||
      element.dataset.messageKey !== operation.key || !currentPosition(operation) ||
      document.hidden || navigationContact.current) return;
    const { viewport, expectedTop } = operation;
    if (!viewport.clientHeight || expectedTop !== undefined && expectedTop !== viewport.scrollTop) return;
    alignReadingAnchor(operation, element);
  }, [currentPosition]);
  const followMeasuredOutput = useCallback(() => {
    const viewport = scroller.current;
    const view = currentView.current;
    if (!view?.sessionId || !viewport || followFrame.current) return;
    const sessionId = view.sessionId;
    const canAdvance = () => scroller.current === viewport && following.current && !navigationContact.current &&
      !position.current && !document.hidden && viewport.clientHeight > 0 && currentViewportView(view) &&
      hasTranscriptBaseline(useSessionStore.getState(), sessionId);
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
      const top = viewport.scrollTop;
      const bottom = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
      const gap = bottom - top;
      if (gap > 0 && dt > 0 && current()) {
        const next = reducedMotion.current?.matches ? bottom
          : top + Math.min(gap, Math.max(1, gap * (1 - Math.exp(-dt / 60))));
        virtuoso.current?.scrollTo({ top: bottom - next <= 2 ? bottom : next, behavior: "auto" });
      }
      if (!current()) { stop(); return; }
      lastScrollTop.current = viewport.scrollTop;
      // Equal/coarse readings cannot retire an unfinished target. Keep its
      // time origin until a positive interval permits the next actual write.
      if (dt > 0) work.time = time;
      // A write can synchronously expose a newer extent or be clamped by the
      // browser. Rounded size properties can leave a fractional bottom gap;
      // settle within tolerance using current geometry, not the old target.
      if (viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop > 2 && current()) {
        work.frame = requestAnimationFrame(advance);
      } else {
        initialPresentation.current.automaticReturn = false;
        stop();
      }
    };
    work.frame = requestAnimationFrame(advance);
  }, [currentViewportView]);
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
    retirePosition(); cancelFollow();
    downwardNavigation.current = null;
    readingHandoff.current = null;
    following.current = true;
    setReadingBoundary(false);
    owner.timeoutHint = false;
    // Following grants visible movement; it does not manufacture the retired
    // initial operation's measurement proof or open a new preparation budget.
    publishPresentation(owner, "reading");
    followMeasuredOutput();
  }, [cancelFollow, currentViewportView, followMeasuredOutput, publishPresentation, retirePosition, retireRecovery]);
  const consumeTimeoutRecovery = useCallback((owner = initialPresentation.current) => {
    if (owner !== initialPresentation.current || !owner.recovery?.pending || document.hidden ||
      !scroller.current?.clientHeight || navigationContact.current) return;
    resumeVisibleFollowing("accepted-output");
  }, [resumeVisibleFollowing]);
  useLayoutEffect(() => () => retireRecovery(), [retireRecovery]);
  const failPreparation = useCallback((owner: InitialPresentation, error: string) => {
    if (owner !== initialPresentation.current || owner.phase !== "preparing" && owner.phase !== "revealing") return;
    pausePreparation(owner);
    retirePosition(); cancelFollow();
    following.current = false;
    owner.error = error;
    publishPresentation(owner, "incomplete");
    staging.current?.fail(error);
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
    downwardNavigation.current = null;
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
      viewport?.clientHeight && !document.hidden && !navigationContact.current;
    if (!active) { pausePreparation(owner); return; }
    if (owner.since !== null) return;
    if (owner.remaining <= 0) { expirePreparation(owner); return; }
    owner.since = performance.now();
    owner.deadline = window.setTimeout(() => {
      if (owner !== initialPresentation.current) return;
      pausePreparation(owner);
      updateBudget();
    }, owner.remaining);
  }, [currentViewportView, expirePreparation]);
  const pauseFollowing = useCallback((source?: "handoff" | "details") => {
    retireRecovery();
    following.current = false;
    cancelFollow();
    retirePosition();
    downwardNavigation.current = null;
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
  }, [cancelFollow, retirePosition, currentViewportView, publishPresentation, retireRecovery]);
  const pauseForReading = useCallback((event: MouseEvent<HTMLElement>) => {
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest("button[aria-expanded], summary")) pauseFollowing("details");
  }, [pauseFollowing]);
  const navigate = useCallback((up: boolean, from: number, contact?: NavigationContact) => {
    const viewport = scroller.current;
    const view = currentView.current;
    if (!viewport?.clientHeight || document.hidden || !view || !currentViewportView(view)) return false;
    if (viewport.scrollTop > 0) setPagingReady(true);
    pauseFollowing();
    if (!up && !contact?.invalid) {
      if (!navigationContact.current && viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <= 4) {
        resumeVisibleFollowing();
      } else downwardNavigation.current = { view, viewport, top: from, until: performance.now() + 500, contact };
    }
    return true;
  }, [pauseFollowing, currentViewportView, resumeVisibleFollowing]);
  const invalidateNavigationContact = useCallback((contact: NavigationContact) => {
    contact.invalid = true;
    if (downwardNavigation.current?.contact === contact) downwardNavigation.current = null;
  }, []);
  const startNavigationContact = useCallback((source: NavigationContact["source"], id: number, y: number, pointerType?: string) => {
    const viewport = scroller.current;
    const view = currentView.current;
    if (!viewport?.clientHeight || document.hidden || !view || !currentViewportView(view)) return;
    cancelFollow();
    const operation = position.current;
    if (operation?.frame !== null && operation?.frame !== undefined) cancelAnimationFrame(operation.frame);
    if (operation) operation.frame = null;
    downwardNavigation.current = null;
    // Contact suspends writes. The current reading decision remains with the
    // opening, including any input that arrives during this contact.
    navigationContact.current = { source, id, y, top: viewport.scrollTop,
      invalid: false, pointers: new Map(pointerType === undefined ? [] : [[id, pointerType]]),
      touch: null, sessionId: view.sessionId, account: view.account };
    preparationBudget();
  }, [cancelFollow, currentViewportView, preparationBudget]);
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
  const restorePhysicalReading = useCallback((view: ChatViewportView, viewport: HTMLElement, top: number) => {
    viewport.scrollTop = top;
    // Keep the actual application write as the current reading sample. Its
    // delayed scroll notification must not become a new navigation report.
    readingHandoff.current = { view, anchor: null, physical: { viewport, top: viewport.scrollTop, restored: true } };
  }, []);
  const positionMeasuredOutput = useCallback(function schedulePosition() {
    preparationBudget();
    const operation = position.current;
    if (!operation || !currentPosition(operation) || !operation.viewport.clientHeight || document.hidden || navigationContact.current) return;
    // A live Footer can grow between the reveal commit and the first paint.
    // The initial operation still owns that layout and lands without animation.
    if (operation.kind === "initial" && initialPresentation.current.phase === "ready") {
      positionAtPhysicalTail(operation.viewport, virtuoso.current);
    }
    if (operation.frame !== null) return;
    const frame = requestAnimationFrame(() => {
      if (operation.frame !== frame) return;
      operation.frame = null;
      if (!currentPosition(operation) || !operation.viewport.clientHeight || document.hidden || navigationContact.current) return;
      const viewport = operation.viewport;
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
        followMeasuredOutput();
      };
      if (operation.kind === "initial") {
        if (!operation.view.sessionId || !hasTranscriptBaseline(useSessionStore.getState(), operation.view.sessionId)) return;
        if (!positionAtPhysicalTail(viewport, virtuoso.current)) {
          // The extent can change while Virtuoso installs the new range.
          schedulePosition();
        } else if (initialPresentation.current.phase === "ready") {
          finish();
        } else if (latestContentInView(viewport, rows.at(-1), operation.view.sessionId)) {
          const owner = initialPresentation.current;
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
        if (!currentPosition(operation) || navigationContact.current) return false;
        if (operation.expectedTop === undefined || operation.expectedTop === viewport.scrollTop) return true;
        finish();
        return false;
      };
      if (!canCorrect()) return;
      const index = rows.findIndex((message) => chatMessageKey(message) === operation.key);
      if (index < 0) {
        viewport.scrollTop = Math.max(0, Math.min(operation.physicalTop, viewport.scrollHeight - viewport.clientHeight));
        setReadingBoundary(true);
        finish(); return;
      }
      const element = Array.from(viewport.querySelectorAll<HTMLElement>("[data-message-key]"))
        .find((row) => row.dataset.messageKey === operation.key);
      if (element) {
        const result = alignReadingAnchor(operation, element);
        if (result === "moved" || result === "aligned" && operation.alignedHeight !== viewport.scrollHeight) {
          // A write can change the virtual range. Confirm the same anchor
          // after measurement, and end the operation after a physical clamp.
          if (result === "aligned") operation.alignedHeight = viewport.scrollHeight;
          schedulePosition();
          return;
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
            if (top >= 0 && top < viewport.scrollHeight && Math.abs(top - viewport.scrollTop) > 1 && currentPosition(operation)) {
              virtuoso.current?.scrollTo({ top, behavior: "auto" });
              if (operation.expectedTop !== undefined) operation.expectedTop = viewport.scrollTop;
              schedulePosition();
            }
          }
          return null;
        } });
      }
    });
    operation.frame = frame;
  }, [currentPosition, followMeasuredOutput, preparationBudget, publishPresentation, retirePosition, restorePhysicalReading]);
  const restoreReading = useCallback((view: ChatViewportView, viewport: HTMLElement) => {
    if (!currentViewportView(view) || !viewport.clientHeight || document.hidden || navigationContact.current) return;
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
      restorePhysicalReading(view, viewport, Math.min(physical.top, maxTop));
      if (handoff?.view.window !== view.window) setReadingBoundary(true);
    } else {
      restorePhysicalReading(view, viewport, 0);
      setReadingBoundary(true);
    }
  }, [currentViewportView, positionMeasuredOutput, reportPagingPosition, restorePhysicalReading, retirePosition]);
  const measuredOutput = useCallback(() => {
    preparationBudget();
    const viewport = scroller.current;
    if (!viewport?.clientHeight || document.hidden) {
      cancelFollow();
      const operation = position.current;
      if (operation?.frame !== null && operation?.frame !== undefined) cancelAnimationFrame(operation.frame);
      if (operation) operation.frame = null;
      return;
    }
    const view = currentView.current;
    if (!following.current && view && initialPresentation.current.phase !== "incomplete" &&
      pendingReadingHandoff(readingHandoff.current, view, viewport)) {
      restoreReading(view, viewport);
    }
    positionMeasuredOutput();
    consumeTimeoutRecovery();
    followMeasuredOutput();
  }, [cancelFollow, consumeTimeoutRecovery, positionMeasuredOutput, followMeasuredOutput, preparationBudget, restoreReading]);
  const releaseNavigationContact = useCallback((contact: NavigationContact, y: number) => {
    if (navigationContact.current !== contact) return;
    navigationContact.current = null;
    const viewport = scroller.current;
    const view = currentView.current;
    const canMeasure = viewport?.clientHeight && view && contact.sessionId === view.sessionId &&
      contact.account === view.account && currentViewportView(view);
    const owner = initialPresentation.current;
    const scrollbar = contact.source === "scrollbar";
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
    if (scrollbar && y !== contact.y) pauseFollowing();
    else if (scrollbar && !following.current && position.current?.kind === "history") retirePosition();
    if (following.current) readingHandoff.current = null;
    if (!canMeasure) return;
    const navigation = downwardNavigation.current;
    const directed = navigation?.viewport === viewport && navigation.view === view;
    // Native thumb drags may omit pointermove. Touch release instead requires
    // direction established by this contact, so inner scrolling or a tap cannot
    // turn an unrelated main-list displacement into a return to following.
    const returned = (!contact.invalid && scrollbar && y > contact.y && viewport.scrollTop > contact.top || directed && viewport.scrollTop >= navigation.top) &&
      viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <= 4;
    if (returned) resumeVisibleFollowing();
    else if (directed) {
      navigation.contact = undefined;
      navigation.until = performance.now() + 500;
    }
    if (directed) readingHandoff.current = null;
    measuredOutput();
  }, [currentViewportView, measuredOutput, pauseFollowing, resumeVisibleFollowing, retirePosition]);
  useEffect(() => {
    // The opening owns a held contact, even if its original viewport is
    // replaced or the release lands outside the list. Geometry stays scoped.
    const pointerDown = (event: PointerEvent) => {
      const contact = navigationContact.current;
      if (!contact || contact.touch && event.pointerType === "touch" || contact.pointers.has(event.pointerId)) return;
      contact.pointers.set(event.pointerId, event.pointerType);
      contact.invalid = true;
      downwardNavigation.current = null;
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
    downwardNavigation.current = null;
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
    cancelFollow();
    retirePosition();
    downwardNavigation.current = null;
    viewportObserver.current?.disconnect();
    viewportObserver.current = null;
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
    lastScrollTop.current = viewport?.scrollTop ?? 0;
    if (next) {
      // Virtuoso publishes this ref after layout effects. Attach to the
      // actual node, even when a replacement commits while chat is hidden.
      if (initialPresentation.current.phase !== "incomplete") {
        if (following.current) startInitialPosition(next);
        else if (view) restoreReading(view, next);
      }
      viewportObserver.current = new ResizeObserver(measuredOutput);
      viewportObserver.current.observe(next);
    }
  }, [cancelFollow, retirePosition, startInitialPosition, measuredOutput, currentViewportView, saveReadingHandoff, restoreReading]);
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
      // Match the saved row to the preview removed by the same publication.
      // Take its position while the live text and its viewport are still mounted.
      if (sid && state.messages[sid] !== before.messages[sid] &&
        !following.current && !position.current && !navigationContact.current && !document.hidden &&
        view && currentViewportView(view) && viewport?.clientHeight && !captureReadingAnchor(viewport)) {
        const bounds = viewport.getBoundingClientRect();
        const preview = [...viewport.querySelectorAll<HTMLElement>("[data-live-output]")].find((entry) => {
          const box = entry.getBoundingClientRect();
          return box.bottom > bounds.top && box.top < bounds.bottom;
        });
        if (preview) {
          const item = preview.dataset.liveOutput;
          const previous = item ? before.streamingItems[sid]?.[item] : before.streamingText[sid];
          const remaining = item ? state.streamingItems[sid]?.[item] : state.streamingText[sid];
          const saved = previous && !remaining && state.messages[sid]?.find((message) => message.role === "assistant" &&
            message.type === "text" && message.content === previous.content && nativeItemKey(message.runtime_metadata) === (item || undefined) &&
            !before.messages[sid]?.includes(message));
          if (saved && preview.querySelector(".msg-streaming-text")?.textContent === previous.content) {
            position.current = { kind: "history", view, viewport, frame: null, key: chatMessageKey(saved),
              offset: transcriptTextTop(preview) - bounds.top, textOffset: true, physicalTop: viewport.scrollTop };
            // The reader can be below Virtuoso's estimated range for the
            // new row. Render back to the preview's start until it measures.
            setTransferOverscan(Math.max(0, bounds.top - preview.getBoundingClientRect().top));
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
    currentView.current = null;
    cancelFollow(); retirePosition();
    downwardNavigation.current = null;
    if (navigationContact.current) navigationContact.current.invalid = true;
    olderRead.current?.cancel(); olderRead.current = null;
    setViewportView({ sessionId: sid, account: state.accountGeneration, window: messageWindow });
  }), [activeSessionId, cancelFollow, currentViewportView, retirePosition, publishPresentation, retireRecovery, saveReadingHandoff]);
  useLayoutEffect(() => {
    currentView.current = viewportView;
    cancelFollow();
    retirePosition();
    setLoadingOlder(false);
    setAtTop(false);
    const viewport = scroller.current;
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
        currentView.current = null;
        readingHandoff.current = null;
        downwardNavigation.current = null; navigationContact.current = null;
        olderRead.current?.cancel(); olderRead.current = null;
      }
    };
  }, [viewportView, cancelFollow, retirePosition, startInitialPosition, restoreReading, measuredOutput]);
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
      const anchor = captureReadingAnchor(element);
      retirePosition();
      downwardNavigation.current = null;
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
  }, [activeSessionId, pauseFollowing, retirePosition, currentViewportView]);

  useLayoutEffect(() => {
    committed.current = { messages, subagentSnapshots, activeRequestId, pendingQuestions };
    projectedMessages.current = chatMessages;
    positionMeasuredOutput();
  }, [messages, subagentSnapshots, activeRequestId, pendingQuestions, chatMessages, loadingOlder, transcriptBaseline, positionMeasuredOutput]);

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
      latestContentInView(viewport, projectedMessages.current.at(-1), sessionId);
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
          alignTransferredRow(row);
          if (msg === projectedMessages.current.at(-1) && position.current?.kind === "initial") positionMeasuredOutput();
        }}>
          {body}
          <MessageTime iso={msg.created_at} />
        </div>
      );
    },
    [renderMessageBody, pendingQuestions, activeSessionId, alignTransferredRow, positionMeasuredOutput]
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

  const showReadingNotice = readingBoundary || presentation.owner.timedOut && !following.current;
  return <div className="contents">
      <div className={showReadingNotice ? "shrink-0 px-4 py-2 text-sm text-muted-foreground" : "sr-only"}>
        <p role="status" aria-atomic="true" data-chat-timeout={presentation.owner.timeoutHint || undefined}>
          {presentation.owner.timeoutHint && "Automatic positioning did not finish. Loaded content is available."}
        </p>
        {presentation.owner.timeoutHint && <>
          {presentation.owner.recovery && <p>New output will return to the latest content unless you choose to read here.</p>}
          <Button size="sm" variant="ghost" aria-label="Dismiss positioning notice" onClick={() => {
            presentation.owner.timeoutHint = false;
            publishPresentation(presentation.owner, presentation.owner.phase);
          }}>Dismiss</Button>
        </>}
        {readingBoundary && <p role="status">The previous reading position is unavailable. Showing loaded content.</p>}
        {showReadingNotice && <Button size="sm" variant="ghost" onClick={() => resumeVisibleFollowing()}>Jump to latest</Button>}
      </div>
      {transcriptDamaged && activeSessionId && <TranscriptIntegrityNotice key={activeSessionId} sessionId={activeSessionId} />}

      <div className="relative flex flex-1 min-h-0" style={minimumViewportHeight ? { minHeight: minimumViewportHeight } : undefined}
        onClickCapture={pauseForReading}
        onWheelCapture={(event) => {
          const viewport = scroller.current;
          if (!viewport || !event.deltaY || event.ctrlKey) return;
          // Composited wheel scrolling may move the DOM before React receives
          // its passive capture event. Keep the pre-scroll bound as evidence.
          const up = event.deltaY < 0;
          const from = up ? Math.max(viewport.scrollTop, lastScrollTop.current) : Math.min(viewport.scrollTop, lastScrollTop.current);
          if (!canScrollChat(viewport, event.target, up, from)) return;
          navigate(up, from);
        }}
        onKeyDown={(event) => {
          if (event.defaultPrevented || event.altKey || event.metaKey || event.ctrlKey && !["Home", "End"].includes(event.key)) return;
          const up = ["ArrowUp", "PageUp", "Home"].includes(event.key) || event.key === " " && event.shiftKey;
          const down = ["ArrowDown", "PageDown", "End"].includes(event.key) || event.key === " " && !event.shiftKey;
          if ((!up && !down) || !canScrollChat(scroller.current, event.target, up)) return;
          const target = event.target instanceof HTMLElement ? event.target : null;
          if (target?.isContentEditable || target?.closest("input, textarea, select, [role=textbox]")) return;
          if (event.key === " " && target?.closest("button, a[href], summary, [role=button]")) return;
          if (!navigate(up, scroller.current!.scrollTop)) return;
          if (event.key === "Home" && virtuoso.current) {
            // Native Home animates across estimated rows and can be interrupted
            // when Virtuoso measures them. Open the loaded start directly.
            event.preventDefault();
            virtuoso.current.scrollTo({ top: 0, behavior: "instant" });
          }
        }}
        onPointerDownCapture={(event) => {
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
          downwardNavigation.current = null;
          if (event.isPrimary && (touchOrPen || scrollbar)) {
            startNavigationContact(scrollbar ? "scrollbar" : "pointer", event.pointerId, event.clientY, event.pointerType);
          }
        }}
        onPointerMoveCapture={(event) => {
          const pointer = navigationContact.current;
          if (!pointer || pointer.source === "touch" || pointer.id !== event.pointerId) return;
          if (pointer.source === "scrollbar") {
            if (event.clientY !== pointer.y) pauseFollowing();
            return;
          }
          if (event.clientY === pointer.y) return;
          const up = event.clientY > pointer.y;
          pointer.y = event.clientY;
          if (canScrollChat(scroller.current, event.target, up)) navigate(up, pointer.top, pointer);
        }}
        onTouchStartCapture={(event) => {
          downwardNavigation.current = null;
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
          // A held scrollbar retains control of downward movement during
          // mixed input. A touch-only gesture can still retire old geometry.
          if (!up && contact.source !== "touch") return;
          const viewport = scroller.current;
          if (!viewport) return;
          const from = up ? Math.max(viewport.scrollTop, lastScrollTop.current) : Math.min(viewport.scrollTop, lastScrollTop.current);
          if (canScrollChat(viewport, event.target, up, from)) navigate(up, contact.top, contact);
        }}
      >
      <Virtuoso
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
        itemsRendered={positionMeasuredOutput}
        onScrollEnd={() => { if (!navigationContact.current) downwardNavigation.current = null; }}
        onScroll={(event) => {
          const viewport = event.currentTarget;
          const view = currentView.current;
          if (viewport !== scroller.current || !viewport.clientHeight || !view || !currentViewportView(view)) return;
          const top = viewport.scrollTop;
          const bottom = viewport.scrollHeight - viewport.clientHeight - top <= 4;
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
          if (navigationContact.current && navigationContact.current.source !== "scrollbar") navigationContact.current.top = Math.min(navigationContact.current.top, top);
          if (!position.current && !navigationContact.current) {
            const navigation = downwardNavigation.current;
            if (navigation && (navigation.viewport !== viewport || performance.now() > navigation.until ||
              !currentViewportView(navigation.view) || top < lastScrollTop.current)) {
              downwardNavigation.current = null;
            } else if (navigation && top > lastScrollTop.current) {
              navigation.until = performance.now() + 500;
              if (bottom && top > navigation.top) {
                resumeVisibleFollowing();
              }
            }
          }
          const handoff = readingHandoff.current;
          const restoreReport = restoredReadingAt(handoff, view, viewport);
          if (!position.current && !restoreReport && !pendingReadingHandoff(handoff, view, viewport)) {
            reportPagingPosition(viewport);
            if (!following.current) {
              const anchor = captureReadingAnchor(viewport);
              readingHandoff.current = { view, anchor };
            }
          }
          lastScrollTop.current = top;
        }}
        totalListHeightChanged={measuredOutput}
        followOutput={false}
        increaseViewportBy={{ top: 400 + transferOverscan, bottom: 400 }}
        components={{ Header: olderHeader, List: ChatRows, Footer: ChatFooter }}
        context={footerContext}
      />

      {(presentation.phase === "preparing" || presentation.phase === "incomplete") && <div data-chat-initial-surface
        data-chat-preparation={!transcriptBaseline || presentation.phase === "incomplete" || undefined}
        tabIndex={0} aria-label="Conversation preparation"
        className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-4 text-sm text-muted-foreground">
        {(presentation.phase === "incomplete" || !transcriptBaseline) && <p role="status">
          {presentation.phase === "incomplete" ? "Conversation unavailable" : "Loading conversation…"}
        </p>}
        {presentation.owner.error && <p>{presentation.owner.error}</p>}
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

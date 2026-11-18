import { accountFetch as fetch } from "../lib/accountFetch";
import { schedules } from "../lib/scheduleList";
import { sessions as sessionList, type SessionObservation } from "../lib/sessionList";
import { ArchiveDeadline, postArchive } from "../lib/sessionArchive";
import { createResearch } from "../lib/research";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { useStore } from "zustand";
import { chatDraft, prefillChatDraft } from "../lib/chatDraft";
import {
  IconArrowUp,
  IconCpu,
  IconFile,
  IconMenu2,
  IconPaperclip,
  IconPlayerStop,
  IconX,
} from "@tabler/icons-react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import {
  hasTranscriptBaseline,
  observeAcceptedText,
  useSessionStore,
  type Message,
  type PendingQuestion,
  type SessionInfo,
  type StreamingItem,
  type SubagentRun,
} from "../stores/sessionStore";
import { ForkDialog } from "./ForkDialog";
import { ResearchCard } from "./ResearchCard";
import { RecoverableInputs } from "./RecoverableInputs";
import type { ClientSubmissionStatus, SessionArchiveResult } from "../api";
import { WikiJobCards } from "./WikiJobCards";
import { TranscriptIntegrityNotice } from "./TranscriptIntegrityNotice";
import { MessageBubble } from "./MessageBubble";
import { MessageDetails, SavedMessageRecord } from "./MessageDetails";
import { matchesPendingQuestion } from "../lib/questionPresentation";
import { DispatchQueue, type CancelCompact } from "./DispatchQueue";
import { ContextMeter } from "./ContextMeter";
import { CommandNotices } from "./CommandNotices";
import { SessionAuxiliaryNotice } from "./SessionAuxiliaryNotice";
import { MessageTime } from "./MessageTime";
import { ModelPickerDialog } from "./ModelPickerDialog";
import { isImeComposing } from "../lib/composerKeys";
import { isNativeChild, nativeChatMessages } from "../lib/nativeOutput";
import { caretWantsHistory } from "../lib/composerHistory";
import { useComposerHistory } from "../hooks/useComposerHistory";
import { OctopusLogo } from "./OctopusLogo";
import { CredentialPicker } from "./CredentialPicker";
import { PageHeader } from "./PageHeader";
import { QuestionPrompt, type AnswerPayload } from "./QuestionPrompt";
import { QuestionDraftRecovery } from "./QuestionDraftRecovery";
import { beginQuestionTransmission, finishQuestionTransmission } from "../lib/questionDrafts";
import {
  SlashCommandMenu,
  buildRememberPrompt,
  filterSlashCommands,
  type SlashCommand,
} from "./SlashCommandMenu";
import { Button } from "./ui/button";
import { useSessionFork } from "../hooks/useSessionFork";
import { observeOlderMessages } from "../lib/transcript";
import { selectSession } from "../lib/selectSession";
import { observeSession } from "../lib/hydrateSession";
import type { WikiMessageMode } from "../lib/wikiComposer";
import { classifySubmission, newSubmissionId, readSubmission, type QuestionCommandTarget, type SubmissionReply } from "../lib/submission";
import { MAX_ATTACHMENTS_PER_MESSAGE, useDraftAttachments, type PendingAttachment } from "../hooks/useDraftAttachments";

const EMPTY_MESSAGES: Message[] = [];
const EMPTY_STREAMING_ITEMS: Record<string, StreamingItem> = {};
const EMPTY_SUBAGENTS: Record<string, SubagentRun> = {};

/** True on a device whose primary input can't hover — a phone or tablet.
 *
 * Read at the moment of the keystroke rather than cached in state: a tablet
 * with a keyboard attached mid-session changes the answer, and this is cheap.
 */
function isTouchDevice(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(hover: none)").matches
  );
}

/** A nested output pane can consume navigation or block scroll chaining.
 * Downward input at the physical boundary still belongs to the main list. */
function canScrollChat(viewport: HTMLElement | null, target: EventTarget | null, up: boolean, from = viewport?.scrollTop ?? 0): boolean {
  if (!viewport?.clientHeight || up && from <= 0) return false;
  let element = target instanceof Element ? target : null;
  // The preparation surface overlays this same list while its layout stays
  // measurable. Navigation there has the same owner as navigation on the list.
  if (element?.closest("[data-chat-preparation]")?.parentElement?.querySelector(".chat-messages") === viewport) element = viewport;
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

function captureReadingAnchor(viewport: HTMLElement) {
  const bounds = viewport.getBoundingClientRect();
  const visible = Array.from(viewport.querySelectorAll<HTMLElement>("[data-message-key]")).find((element) => {
    const rectangle = element.getBoundingClientRect();
    return rectangle.bottom > bounds.top && rectangle.top < bounds.bottom;
  });
  return visible ? { key: visible.dataset.messageKey!, offset: visible.getBoundingClientRect().top - bounds.top } : null;
}

function ArchivedSuccessor({ sessionId }: { sessionId: string }) {
  const successorId = useSessionStore((state) => state.sessionSuccessors[sessionId]);
  const pending = useRef(false);
  const stop = useRef<() => void>(() => {});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => () => { stop.current(); stop.current = () => {}; }, []);

  const open = async () => {
    if (pending.current || !successorId) return;
    const tracked = sessionList.observe(sessionId);
    const start = useSessionStore.getState();
    let foreground = start.activeSessionId === sessionId && start.mainView === "chat";
    const unsubscribe = useSessionStore.subscribe((state) => {
      if (state.activeSessionId !== sessionId || state.mainView !== "chat") foreground = false;
    });
    const retire = () => { foreground = false; unsubscribe(); };
    stop.current = retire;
    pending.current = true; setBusy(true); setError("");
    try {
      const info = await sessionList.confirmSuccessor(sessionId, tracked.observation);
      if (!foreground || !sessionList.current(tracked.observation)) return;
      const state = useSessionStore.getState();
      if (!info || state.sessionSuccessors[sessionId] !== info.id || !state.archivedSessions.some((row) => row.id === sessionId)) {
        setError(state.sessionRead.error ?? "Successor is no longer available.");
        return;
      }
      await selectSession(info.id, info.agent_id);
    } finally {
      tracked.finish(); unsubscribe(); pending.current = false;
      if (stop.current === retire) { stop.current = () => {}; setBusy(false); }
    }
  };
  if (!successorId) return null;
  return <div className="shrink-0">
    <Button size="sm" variant="outline" disabled={busy} onClick={() => void open()}>Open successor</Button>
    {error && <p role="alert" className="mt-1 text-destructive">{error}</p>}
  </div>;
}

interface Props {
  sendMessage: (
    sessionId: string,
    content: string,
    attachmentIds?: string[],
    wikiMode?: WikiMessageMode,
    submissionId?: string,
    recoveryOfClientId?: string
  ) => void | Promise<SubmissionReply>;
  interrupt: (sessionId: string) => void | Promise<SubmissionReply>;
  cancelCompact?: CancelCompact;
  answerQuestion: (
    sessionId: string,
    questionId: string,
    answers: AnswerPayload[],
    target?: QuestionCommandTarget
  ) => void | Promise<SubmissionReply>;
  snoozeQuestion?: (sessionId: string, questionId: string, target: QuestionCommandTarget) => void | Promise<SubmissionReply>;
  connected: boolean;
  onToggleSidebar: () => void;
  // Open the all-agents Schedules overview (App owns the dialog). Used by the
  // bare `/schedule` command.
  onOpenSchedules: () => void;
}

const EMPTY_QUESTIONS: PendingQuestion[] = [];

// Keep the component type stable as its live contents change. Replacing the
// Footer component on every delta unmounts it and disrupts size observation.
function ChatFooter({ context }: { context?: { footer: ReactNode; onLayout: () => void } }) {
  const footer = context?.footer;
  const onLayout = context?.onLayout;
  useLayoutEffect(() => { onLayout?.(); }, [footer, onLayout]);
  return <div data-chat-footer>{footer}</div>;
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
  anchor: ReturnType<typeof captureReadingAnchor>;
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
} & ({ kind: "initial"; tail: TailMeasurement | null; checking: boolean;
  installation: TailInstallation | null } |
  { kind: "restore"; handoff: ReadingHandoff; limit: number } |
  { kind: "history"; key: string; offset: number; physicalTop: number; expectedTop?: number; alignedHeight?: number });
interface TailMeasurement {
  message: Message;
  element: HTMLElement;
  index: number;
  height: number;
  width: number;
  installed: boolean;
}
type InitialPosition = Extract<ViewportPosition, { kind: "initial" }>;
interface TailInstallation {
  element: HTMLElement;
  index: number;
  height: number;
  width: number;
  requested: boolean;
  delivered: boolean;
}
interface MeasurementBinding {
  token: symbol;
  operation: InitialPosition;
  demand: TailInstallation;
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
function newPresentation(): InitialPresentation {
  return { phase: "preparing", remaining: 2000, since: null, deadline: null };
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

export function ChatView({
  sendMessage,
  interrupt,
  cancelCompact,
  answerQuestion,
  snoozeQuestion,
  connected,
  onToggleSidebar,
  onOpenSchedules,
}: Props) {
  const accountGeneration = useSessionStore((s) => s.accountGeneration);
  const draftOwner = useSessionStore((s) => s.identity?.user_id);
  const textDraft = chatDraft();
  const { text: input, saveError } = useStore(textDraft.store);
  const pendingFileRestore = useRef<{
    account: number; sessionId: string; requestId: string; version: number;
  } | null>(null);
  const setInput = useCallback((text: string) => {
    pendingFileRestore.current = null;
    textDraft.edit(text);
  }, [textDraft]);
  useEffect(() => () => textDraft.clearConsumed(), [textDraft]);
  const mounted = useRef(true);
  const archiveObservers = useRef(new Set<() => void>());
  useEffect(() => {
    mounted.current = true;
    const observers = archiveObservers.current;
    return () => {
      mounted.current = false;
      pendingFileRestore.current = null;
      for (const stop of observers) stop();
      observers.clear();
    };
  }, []);
  const [restoredInput, setRestoredInput] = useState<{ sessionId: string; clientId: string } | null>(null);
  const [restoredQuestion, setRestoredQuestion] = useState<{ sessionId: string; key: string } | null>(null);
  const [requiredAttachmentCount, setRequiredAttachmentCount] = useState(0);
  const submittedDrafts = useRef(new Map<string, { sessionId: string; text: string; attachments: PendingAttachment[] }>());
  const submissionCommand = useRef<{ account: number; sessionId: string | null } | null>(null);
  const [submissionError, setCommandError] = useState<{
    command: NonNullable<typeof submissionCommand.current>; message: string;
  } | null>(null);
  // Completed command feedback belongs to one operation and one continuous view.
  // Uncertain receipts and recoverable drafts retain their separate lifetimes.
  const setSubmissionError = useCallback((message: string, command?: NonNullable<typeof submissionCommand.current>) => {
    const state = useSessionStore.getState();
    const owner = command ?? { account: state.accountGeneration, sessionId: state.activeSessionId };
    if (!mounted.current || command && submissionCommand.current !== command ||
        owner.account !== state.accountGeneration || owner.sessionId !== state.activeSessionId) return owner;
    submissionCommand.current = owner;
    setCommandError(message ? { command: owner, message } : null);
    return owner;
  }, []);
  const [archiveFeedback, setArchiveFeedback] = useState<{
    intent: { account: number; sessionId: string }; version: number;
    observation: SessionObservation; message: string; activity: string | null;
    confirm?: () => Promise<void>; checking?: boolean;
  } | null>(null);
  const [rejectedDrafts, setRejectedDrafts] = useState<{
    id: string; sessionId: string; text: string; attachments: PendingAttachment[];
    reason: string;
    receipt?: SubmissionReply;
    recoveryOfClientId?: string;
    uncertain?: { id: string };
    readOnly?: boolean;
    confirming?: boolean;
    compactDraft?: { accountGeneration: number; revision: number; version: number };
  }[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);
  // The opening owns its target even while a list confirmation replaces rows.
  // No seq opens the /rewind picker; seq opens the per-message confirm step.
  const forkDialogSequence = useRef(0);
  const [forkDialog, setForkDialog] = useState<{
    instance: number; sessionId: string; parentName: string; account: number; seq?: number;
  } | null>(null);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  // Slash-command autocomplete: which item is highlighted, and whether the
  // user dismissed the menu with Esc (re-opens as soon as they type again).
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashDismissed, setSlashDismissed] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const composerHistory = useComposerHistory(textDraft, textareaRef);
  const resetHistory = composerHistory.reset;
  // Counter for nested dragenter/dragleave events — without this the
  // overlay flickers as the cursor crosses child elements (textarea,
  // chips, etc.) because each crossing fires a leave on the parent.
  const dragCounter = useRef(0);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const transcriptBaseline = useSessionStore((s) => activeSessionId ? hasTranscriptBaseline(s, activeSessionId) : false);
  const transcriptReadError = useSessionStore((s) => activeSessionId ? s.transcriptReadErrors[activeSessionId] : undefined);
  const viewing = useRef<{ account: number; sessionId: string } | null>(null);
  useEffect(() => {
    const unsubscribe = useSessionStore.subscribe((state, before) => {
      if (state.accountGeneration !== before.accountGeneration || state.activeSessionId !== before.activeSessionId ||
          state.mainView !== before.mainView || state.viewer !== before.viewer) {
        viewing.current = null;
        submissionCommand.current = null;
        pendingFileRestore.current = null;
      }
    });
    return () => { viewing.current = null; unsubscribe(); };
  }, []);
  const composerRevision = useRef(0);
  const compactSending = useRef<AbortController | null>(null);
  useEffect(() => () => {
    compactSending.current?.abort();
    compactSending.current = null;
  }, [activeSessionId, accountGeneration]);
  useEffect(() => { composerRevision.current += 1; }, [input, activeSessionId, accountGeneration]);
  const contextSnapshot = useSessionStore((s) => activeSessionId ? s.contextStates[activeSessionId] : null);
  const contextState = contextSnapshot?.state;
  const researchCreations = useRef(new Set<AbortController>());
  useEffect(() => {
    const operations = researchCreations.current;
    return () => { operations.forEach((operation) => operation.abort()); operations.clear(); };
  }, [activeSessionId]);
  const { attachments: pendingAttachments, addFiles: addAttachmentFiles, removeAttachment, retryAttachment,
    restoreAttachments, clearAttachments, rejectedFiles, dismissRejectedFiles, unready,
    readyIds: readyAttachmentIds } = useDraftAttachments(activeSessionId);
  const addFiles = useCallback((files: File[]) => {
    pendingFileRestore.current = null;
    addAttachmentFiles(files);
  }, [addAttachmentFiles]);
  const retainSubmittedInput = useCallback((requestId: string, draft: { sessionId: string; text: string; attachments: PendingAttachment[] }) => {
    if (!mounted.current || !draft.attachments.length) return;
    const state = useSessionStore.getState();
    const result = state.messages[draft.sessionId]?.find((message) => message.outcome?.request_id === requestId);
    if (!result || result.outcome?.code === "attachment_unavailable") submittedDrafts.current.set(requestId, draft);
    const recovery = pendingFileRestore.current;
    if (!recovery || recovery.requestId !== requestId || recovery.sessionId !== draft.sessionId) return;
    pendingFileRestore.current = null;
    if (state.accountGeneration !== recovery.account || state.activeSessionId !== recovery.sessionId ||
        textDraft.store.getState().version !== recovery.version) return;
    // The user already restored this exact failure. Its late receipt may add
    // Files only while that composer remains unchanged and owns no new files.
    restoreAttachments(draft.attachments);
    submittedDrafts.current.delete(requestId);
    setRequiredAttachmentCount(0);
  }, [restoreAttachments, textDraft]);
  useEffect(() => {
    const discard = (sessionId?: string) => {
      for (const [id, draft] of submittedDrafts.current) {
        if (!sessionId || draft.sessionId === sessionId) submittedDrafts.current.delete(id);
      }
      setRejectedDrafts((drafts) => drafts.filter((draft) => sessionId && draft.sessionId !== sessionId));
      setRestoredInput((draft) => draft && sessionId && draft.sessionId !== sessionId ? draft : null);
      setRestoredQuestion((draft) => draft && sessionId && draft.sessionId !== sessionId ? draft : null);
      if (!sessionId || sessionId === activeSessionId) {
        clearAttachments(); setRequiredAttachmentCount(0); setSubmissionError(""); resetHistory();
      }
    };
    const deleted = sessionList.onDeleted(discard);
    const account = useSessionStore.subscribe((state, before) => {
      if (state.accountGeneration !== before.accountGeneration) discard();
    });
    return () => { deleted(); account(); };
  }, [activeSessionId, clearAttachments, resetHistory, setSubmissionError]);
  // Request/File settlement outlives the foreground session. It consumes
  // account messages without subscribing the current display to all of them.
  useLayoutEffect(() => useSessionStore.subscribe((state, before) => {
    if (state.messages === before.messages) return;
    for (const [requestId, draft] of submittedDrafts.current) {
      const result = state.messages[draft.sessionId]?.find((message) => message.outcome?.request_id === requestId);
      if (result && result.outcome?.code !== "attachment_unavailable") submittedDrafts.current.delete(requestId);
    }
  }), []);
  const messages = useSessionStore((s) => activeSessionId ? s.messages[activeSessionId] ?? EMPTY_MESSAGES : EMPTY_MESSAGES);
  const streamingText = useSessionStore((s) => activeSessionId ? s.streamingText[activeSessionId]?.content ?? "" : "");
  const streamingItems = useSessionStore((s) => activeSessionId ? s.streamingItems[activeSessionId] ?? EMPTY_STREAMING_ITEMS : EMPTY_STREAMING_ITEMS);
  const subagentSnapshots = useSessionStore((s) => activeSessionId ? s.subagents[activeSessionId] ?? EMPTY_SUBAGENTS : EMPTY_SUBAGENTS);
  const nativeStreaming = useMemo(() => Object.entries(streamingItems)
    .filter(([, item]) => !isNativeChild(item.runtime_metadata)), [streamingItems]);
  const activeSession = useSessionStore((s) => s.sessions.find(session => session.id === activeSessionId)
    ?? s.archivedSessions.find(session => session.id === activeSessionId));
  const activeAgent = useSessionStore((s) => s.agents.find(agent => agent.id === activeSession?.agent_id));
  // Display name for assistant attribution throughout the chat. Harness-
  // neutral fallback when the session has no owning agent.
  const agentLabel = activeAgent?.name || "Assistant";
  const isArchived = !!activeSession?.archived;
  const transcriptDamaged = activeSession?.transcript_integrity === "duplicate_seq";
  const restoreSavedInput = useCallback((saved: ClientSubmissionStatus, files: File[]) => {
    if (!activeSessionId || saved.session_id !== activeSessionId || input.length || pendingAttachments.length || isArchived || transcriptDamaged || !saved.input || saved.input.kind !== "message") return;
    setInput(saved.input.content);
    addFiles(files);
    setRequiredAttachmentCount(saved.input.attachment_ids.length);
    resetHistory();
    setRestoredInput({ sessionId: activeSessionId, clientId: saved.client_request_id });
    textareaRef.current?.focus();
  }, [activeSessionId, input, pendingAttachments, isArchived, transcriptDamaged, addFiles, setInput, resetHistory]);
  const dispatchState = useSessionStore((s) => activeSessionId ? s.dispatchStates[activeSessionId] : undefined);
  const activeRequestId = dispatchState?.active_request_id;
  const chatMessages = useMemo(() => nativeChatMessages(messages,
    Object.values(subagentSnapshots), activeRequestId),
  [messages, subagentSnapshots, activeRequestId]);
  const dispatchPaused = dispatchState?.paused ?? false;
  const pendingQuestions = useSessionStore((s) => activeSessionId ? s.pendingQuestions[activeSessionId] ?? EMPTY_QUESTIONS : EMPTY_QUESTIONS);
  const { queueFork, cancelFork, reset } = useSessionFork();

  // A prepend can also move or regroup child cards. Preserve the actual
  // visible identity and offset, independently of total row counts.
  const virtuoso = useRef<VirtuosoHandle>(null);
  const scroller = useRef<HTMLElement | null>(null);
  // A local observation of the existing identities survives React batching:
  // leaving A retires its work even when B→A completes before the next commit.
  const [viewportView, setViewportView] = useState<ChatViewportView>(() => {
    const state = useSessionStore.getState();
    return { sessionId: state.activeSessionId, account: state.accountGeneration,
      window: state.activeSessionId ? state.messageWindows[state.activeSessionId] : undefined };
  });
  const currentView = useRef<ChatViewportView | null>(null);
  // The opening owns reading intent; a window/node replacement only hands off
  // its optional position. A native scrollbar contact can outlive the commit.
  const readingHandoff = useRef<ReadingHandoff | null>(null);
  const [readingBoundary, setReadingBoundary] = useState(false);
  const initialPresentation = useRef<InitialPresentation>(newPresentation());
  const [presentation, setPresentation] = useState(() => ({ owner: initialPresentation.current, phase: initialPresentation.current.phase }));
  const publishPresentation = useCallback((owner: InitialPresentation, phase: InitialPresentation["phase"]) => {
    if (owner !== initialPresentation.current) return;
    owner.phase = phase;
    setPresentation({ owner, phase });
  }, []);
  const position = useRef<ViewportPosition | null>(null);
  // Keep just the current node/window's last real tail measurement. A reader
  // can request latest without causing another resize of an unchanged row.
  const measuredTail = useRef<(ViewportScope & TailMeasurement) | null>(null);
  const committedRows = useRef(new WeakMap<HTMLElement, {
    view: ChatViewportView; message: Message; index: number;
  }>());
  // React publishes only callback identity; the original position owns the
  // request and releases its DOM/operation references when it retires.
  const measurementRequest = useRef<MeasurementBinding | null>(null);
  const [measurementVersion, setMeasurementVersion] = useState<symbol>();
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
    if (measurementRequest.current?.operation === operation) measurementRequest.current = null;
    if (position.current === operation) position.current = null;
  }, []);
  const currentViewportView = useCallback((view: ChatViewportView) => {
    const state = useSessionStore.getState();
    return currentView.current === view && state.mainView === "chat" && state.accountGeneration === view.account &&
      state.activeSessionId === view.sessionId &&
      (view.sessionId ? state.messageWindows[view.sessionId] : undefined) === view.window;
  }, []);
  const currentPosition = useCallback((operation: ViewportPosition) => {
    return position.current === operation && scroller.current === operation.viewport &&
      currentViewportView(operation.view);
  }, [currentViewportView]);
  const readTail = useCallback((operation: InitialPosition) => {
    const viewport = operation.viewport;
    const rows = projectedMessages.current;
    const message = rows.at(-1);
    const index = rows.length - 1;
    let previous = operation.tail;
    if (previous && (previous.message !== message || previous.index !== index || previous.width !== viewport.clientWidth)) previous = null;
    const element = viewport.querySelector<HTMLElement>(`[data-index="${index}"]`);
    const committed = element ? committedRows.current.get(element) : undefined;
    if (message && element && committed?.view === operation.view && committed.message === message && committed.index === index &&
      element.closest(".chat-messages") === viewport) {
      const rectangle = element.getBoundingClientRect();
      if (rectangle.width > 0 && rectangle.height > 0) {
        if (!previous || previous.element !== element || previous.height !== rectangle.height) {
          previous = { message, element, index, height: rectangle.height, width: viewport.clientWidth, installed: false };
        }
      } else previous = null;
    } else if (element || previous?.element.isConnected) {
      // An unmounted tail keeps its acquired proof while visiting a tall Footer.
      // A mounted node with a different commit cannot stand in for current text.
      previous = null;
    }
    operation.tail = previous;
    if (previous) measuredTail.current = { view: operation.view, viewport, ...previous };
    return previous;
  }, []);
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
  }, [cancelFollow, retirePosition, publishPresentation]);
  const expirePreparation = useCallback((owner: InitialPresentation) => {
    const view = currentView.current;
    const viewport = scroller.current;
    if (owner !== initialPresentation.current || !view?.sessionId || !currentViewportView(view) ||
      !hasTranscriptBaseline(useSessionStore.getState(), view.sessionId)) return;
    pausePreparation(owner);
    retirePosition(); cancelFollow();
    following.current = false;
    downwardNavigation.current = null;
    readingHandoff.current = viewport ? { view, anchor: captureReadingAnchor(viewport),
      physical: { viewport, top: viewport.scrollTop } } : null;
    const focused = document.activeElement;
    focusReading.current = focused && viewport?.parentElement?.querySelector("[data-chat-preparation]")?.contains(focused)
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
    if (!viewport?.clientHeight || document.hidden || !view || !currentViewportView(view)) return;
    if (viewport.scrollTop > 0) setPagingReady(true);
    pauseFollowing();
    if (!up && !contact?.invalid) {
      if (!navigationContact.current && viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <= 4) {
        resumeVisibleFollowing();
      } else downwardNavigation.current = { view, viewport, top: from, until: performance.now() + 500, contact };
    }
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
    if (!operation || !currentPosition(operation) || !operation.viewport.clientHeight || document.hidden || navigationContact.current || operation.frame !== null) return;
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
        if (operation.kind === "initial") {
          setAtTop(atLoadedStart(viewport, rows[0]));
          setPagingReady(viewport.scrollTop > 4);
        }
        followMeasuredOutput();
      };
      if (operation.kind === "initial") {
        if (!operation.view.sessionId || !hasTranscriptBaseline(useSessionStore.getState(), operation.view.sessionId)) return;
        const write = (top: number) => {
          if (!currentPosition(operation) || Math.abs(top - viewport.scrollTop) <= 1) return;
          virtuoso.current?.scrollTo({ top, behavior: "auto" });
          // Every actual application write owns its next verification. The
          // final token, resize or browser scroll notification may never arrive.
          schedulePosition();
        };
        const tail = rows.at(-1);
        const measured = readTail(operation);
        if (tail && !measured) {
          // A tall Footer can put the historical tail outside overscan. Probe
          // the row first; it need not remain mounted at the physical bottom.
          virtuoso.current?.scrollIntoView({ index: rows.length - 1, calculateViewLocation: ({ itemTop, viewportTop }) => {
            const top = Math.max(0, Math.min(viewport.scrollHeight - viewport.clientHeight,
              viewport.scrollTop + itemTop - viewportTop));
            write(top);
            return null;
          } });
          return;
        }
        const positioned = () => {
          if (!currentPosition(operation)) return;
          const gap = viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop;
          if (gap > 1) { write(Math.max(0, viewport.scrollHeight - viewport.clientHeight)); return; }
          const owner = initialPresentation.current;
          if (owner.phase === "preparing") publishPresentation(owner, "revealing");
          else if (owner.phase !== "revealing") finish();
        };
        if (measured) {
          if (!operation.checking) {
            operation.checking = true;
            virtuoso.current?.getState((state) => {
              operation.checking = false;
              if (!currentPosition(operation)) return;
              // A detail may have recommitted equivalent content while this
              // read was pending. Verify the latest commit, not its old object.
              const latest = readTail(operation);
              if (!latest) { schedulePosition(); return; }
              latest.installed = state.ranges.some((range) => range.startIndex <= latest.index && range.endIndex >= latest.index &&
                range.size === latest.height);
              if (latest.installed) {
                operation.installation = null;
                positioned();
                return;
              }
              let demand = operation.installation;
              const sameGeometry = demand && demand.element === latest.element && demand.index === latest.index &&
                demand.height === latest.height && demand.width === latest.width;
              if (!demand || !sameGeometry && (!demand.requested || demand.delivered)) {
                demand = { element: latest.element, index: latest.index, height: latest.height, width: latest.width,
                  requested: false, delivered: false };
                operation.installation = demand;
                // Give an already queued natural measurement its one check
                // before requesting a new public observation.
                schedulePosition();
              } else if (!demand.requested) {
                demand.requested = true;
                const binding = { token: Symbol(), operation, demand };
                measurementRequest.current = binding;
                setMeasurementVersion(binding.token);
              } else if (!sameGeometry && !demand.delivered) {
                Object.assign(demand, { element: latest.element, index: latest.index, height: latest.height, width: latest.width });
              }
            });
          }
          return;
        }
        positioned();
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
        const delta = element.getBoundingClientRect().top - viewport.getBoundingClientRect().top - operation.offset;
        if (Math.abs(delta) > 1 && currentPosition(operation)) {
          operation.alignedHeight = undefined;
          const before = viewport.scrollTop;
          viewport.scrollTop += delta;
          if (operation.expectedTop !== undefined) operation.expectedTop = viewport.scrollTop;
          // This write can change the virtual range and its measured extent.
          // Verify the same anchor after that layout instead of retiring on
          // the pre-write geometry. A physical clamp still ends the operation.
          if (viewport.scrollTop !== before) { schedulePosition(); return; }
        } else if (operation.alignedHeight !== viewport.scrollHeight) {
          // Moving to the anchor can change the rendered range. Confirm the
          // aligned position after that range's measurement affects layout.
          operation.alignedHeight = viewport.scrollHeight;
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
  }, [currentPosition, followMeasuredOutput, preparationBudget, publishPresentation, readTail, retirePosition, restorePhysicalReading]);
  const measureItem = useCallback((element: HTMLElement, field: "offsetHeight" | "offsetWidth") => {
    const rectangle = element.getBoundingClientRect();
    const size = field === "offsetHeight" ? rectangle.height : rectangle.width;
    const binding = measurementRequest.current;
    const operation = binding?.operation;
    if (field === "offsetHeight" && binding && binding.token === measurementVersion && operation && currentPosition(operation) &&
      (!operation.installation || operation.installation === binding.demand)) {
      const latest = readTail(operation);
      if (latest?.element === element) {
        binding.demand.delivered = true;
        // Virtuoso 4.18.6 rebinds its list RO when itemSize changes. The real
        // size is installed after return; even equal measurements owe a check.
        positionMeasuredOutput();
      }
    }
    return size;
  }, [measurementVersion, currentPosition, readTail, positionMeasuredOutput]);
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
    if (owner.phase !== "preparing") publishPresentation(owner, "preparing");
    const view = currentView.current;
    if (!view?.sessionId || !currentViewportView(view)) return;
    const measured = measuredTail.current;
    position.current = { kind: "initial", view, viewport, frame: null,
      tail: measured?.view === view && measured.viewport === viewport ? measured : null, checking: false,
      installation: null };
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
    const anchor = current ? { key: current.key, offset: current.offset }
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
    measuredTail.current = null;
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
    const sid = state.activeSessionId;
    // Hiding chat retires viewport work but keeps the same conversation opening.
    const sameOpening = state.accountGeneration === before.accountGeneration && sid === before.activeSessionId;
    const messageWindow = sid ? state.messageWindows[sid] : undefined;
    const windowChanged = messageWindow !== (sid ? before.messageWindows[sid] : undefined);
    const wasChat = before.mainView === "chat";
    if (sameOpening && wasChat === (state.mainView === "chat") && !windowChanged) return;
    // Retire synchronously at the store boundary, before React can batch a
    // departure and return into identical selected values.
    pausePreparation(initialPresentation.current);
    if (!sameOpening || windowChanged) {
      setPagingReady(false);
      setOlderStalled(false);
    }
    const view = currentView.current;
    const viewport = scroller.current;
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
    measuredTail.current = null;
    cancelFollow(); retirePosition();
    downwardNavigation.current = null;
    if (navigationContact.current) navigationContact.current.invalid = true;
    olderRead.current?.cancel(); olderRead.current = null;
    setViewportView({ sessionId: sid, account: state.accountGeneration, window: messageWindow });
  }), [cancelFollow, retirePosition, publishPresentation, retireRecovery, saveReadingHandoff]);
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
        measuredTail.current = null;
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
    projectedMessages.current = chatMessages;
    positionMeasuredOutput();
  }, [chatMessages, loadingOlder, transcriptBaseline, positionMeasuredOutput]);

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
    const tail = projectedMessages.current.at(-1);
    const footer = viewport.querySelector<HTMLElement>("[data-chat-footer]")?.parentElement;
    const positioned = !document.hidden && viewport.clientHeight > 0 &&
      (!tail || operation.tail?.installed && operation.tail.message === tail && operation.tail.width === viewport.clientWidth) &&
      viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <= 1 &&
      (!footer || footer.getBoundingClientRect().bottom - viewport.getBoundingClientRect().bottom <= 1);
    if (!positioned) {
      publishPresentation(owner, "preparing");
      positionMeasuredOutput();
      return;
    }
    pausePreparation(owner);
    retirePosition(operation);
    publishPresentation(owner, "ready");
    setAtTop(atLoadedStart(viewport, projectedMessages.current[0]));
    setPagingReady(viewport.scrollTop > 4);
    followMeasuredOutput();
  }, [presentation, chatMessages, currentPosition, publishPresentation, positionMeasuredOutput, retirePosition, followMeasuredOutput]);

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

  // Prefilled chat input on fork open (session-rewind.md §6.1). When the
  // active session carries a non-null fork_prefilled_prompt (the rewound user
  // message text, set in fork_metadata until the first turn), populate the
  // input once, after account restoration. Saved text and newer edits take
  // precedence; the backend clears fork_metadata after the first turn.
  useEffect(() => {
    prefillChatDraft(textDraft, activeSessionId, activeSession?.fork_prefilled_prompt);
  }, [activeSessionId, activeSession?.fork_prefilled_prompt, draftOwner, textDraft]);

  const isRunning = activeSession?.status === "running";
  // Whether a message typed right now goes INTO the running turn or queues
  // behind it. A harness capability the server reports per session
  // (inline-steering.md §12) — the button must not promise one and do the
  // other. Attachments always queue, so the label follows that too.
  const canSteer =
    !!activeSession?.can_steer && pendingAttachments.length === 0 &&
    (dispatchState?.active_operation !== "compact" &&
      !dispatchState?.items.some((item) => item.operation === "compact"));

  const restoreRejectedInput = useCallback((message: Message) => {
    if (!activeSessionId || input.length > 0 || pendingAttachments.length > 0) return;
    const recovery = message.tool_input?.rejected_input as { content?: unknown; attachment_ids?: unknown } | undefined;
    if (typeof recovery?.content !== "string" || !Array.isArray(recovery.attachment_ids)) return;
    const requestId = message.outcome?.request_id;
    const draft = requestId ? submittedDrafts.current.get(requestId) : undefined;
    setInput(recovery.content);
    resetHistory();
    if (draft) {
      restoreAttachments(draft.attachments);
      submittedDrafts.current.delete(requestId!);
      setRequiredAttachmentCount(0);
    } else {
      // After a reload the browser no longer has File objects. Keep the
      // complete-input requirement visible until files are selected again.
      setRequiredAttachmentCount(recovery.attachment_ids.length);
      if (requestId && recovery.attachment_ids.length) pendingFileRestore.current = {
        account: useSessionStore.getState().accountGeneration, sessionId: activeSessionId,
        requestId, version: textDraft.store.getState().version,
      };
    }
    textareaRef.current?.focus();
  }, [activeSessionId, input.length, pendingAttachments.length, restoreAttachments, setInput, resetHistory, textDraft]);

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
          onFork={(seq) => { if (activeSessionId) setForkDialog({ sessionId: activeSessionId,
            instance: ++forkDialogSequence.current, parentName: activeSession?.name || "Session", account: accountGeneration, seq }); }}
          onRestoreInput={restoreRejectedInput}
          restoreInputDisabled={input.length > 0 || pendingAttachments.length > 0}
        />
      );
    },
    [
      activeSessionId,
      activeSession?.name,
      accountGeneration,
      answerQuestion,
      snoozeQuestion,
      pendingQuestions,
      restoreRejectedInput,
      input.length,
      pendingAttachments.length,
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
          const element = row?.parentElement;
          if (!element) return;
          const committed = { view: viewportView, message: msg, index };
          committedRows.current.set(element, committed);
          return () => {
            if (committedRows.current.get(element) === committed) committedRows.current.delete(element);
          };
        }}>
          {body}
          <MessageTime iso={msg.created_at} />
        </div>
      );
    },
    [renderMessageBody, pendingQuestions, activeSessionId, viewportView]
  );

  // ---- attachments: upload, paste, drop, picker, chips ------------------

  const handlePaste = useCallback(
    (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
      // Don't preventDefault on text pastes — only intercept when files
      // are present (clipboard images, copied files).
      const files = Array.from(e.clipboardData?.files ?? []);
      if (files.length === 0) return;
      e.preventDefault();
      addFiles(files);
    },
    [addFiles]
  );

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      dragCounter.current = 0;
      setIsDragOver(false);
      const files = Array.from(e.dataTransfer?.files ?? []);
      if (files.length > 0) addFiles(files);
    },
    [addFiles]
  );

  const handleDragEnter = useCallback((e: React.DragEvent) => {
    // Only react to drags that actually carry files, otherwise we'd
    // flash the overlay when the user is just rearranging text.
    if (!e.dataTransfer?.types?.includes("Files")) return;
    e.preventDefault();
    dragCounter.current += 1;
    setIsDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    if (!e.dataTransfer?.types?.includes("Files")) return;
    e.preventDefault();
    dragCounter.current -= 1;
    if (dragCounter.current <= 0) {
      dragCounter.current = 0;
      setIsDragOver(false);
    }
  }, []);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    if (!e.dataTransfer?.types?.includes("Files")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  }, []);

  const hasUploadInFlight = unready || pendingAttachments.length < requiredAttachmentCount;

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

  const footer = useMemo(() => {
    if (!isRunning) return null;
    if (streamingText || nativeStreaming.length) {
      return (
        <div className="msg-streaming" data-testid="streaming-text">
          {streamingText && <MessageBubble
            message={{ role: "assistant", type: "text", content: streamingText }}
            sessionId={activeSessionId ?? ""}
            agentName={activeAgent?.name}
            agentAvatar={activeAgent?.avatar}
            plain
          />}
          {nativeStreaming.map(([key, item]) => <MessageBubble key={key}
            message={{ role: "assistant", type: "text", ...item }} sessionId={activeSessionId || ""}
            agentName={activeAgent?.name} agentAvatar={activeAgent?.avatar} plain />)}
        </div>
      );
    }
    return (
      <div className="msg msg-loading flex items-center gap-1.5 px-3 py-2">
        <span className="loading-dot inline-block size-2 rounded-full bg-muted-foreground/60 animate-pulse [animation-delay:-0.32s]" />
        <span className="loading-dot inline-block size-2 rounded-full bg-muted-foreground/60 animate-pulse [animation-delay:-0.16s]" />
        <span className="loading-dot inline-block size-2 rounded-full bg-muted-foreground/60 animate-pulse" />
      </div>
    );
  }, [
    isRunning,
    streamingText,
    nativeStreaming,
    activeSessionId,
    activeAgent?.name,
    activeAgent?.avatar,
  ]);

  // Slash-command autocomplete state, derived from the current input.
  const slashCommands = useMemo(() => filterSlashCommands(input), [input]);
  const showSlashMenu = slashCommands.length > 0 && !slashDismissed;

  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    // Editing ends the walk: the next ArrowUp starts from the newest again.
    resetHistory();
    // Any edit re-arms the menu and resets the highlight to the top match.
    setSlashIndex(0);
    setSlashDismissed(false);
  };

  // Complete a slash command into the composer: name + trailing space so the
  // caret lands in the args position and the menu hides (whitespace ends the
  // command-typing state). For no-arg commands the trailing space is trimmed
  // away by handleSend's matching.
  const completeSlash = (cmd: SlashCommand) => {
    setInput(`${cmd.name} `);
    setSlashIndex(0);
    setSlashDismissed(false);
    // Focus now so completion cannot move a later text selection.
    textareaRef.current?.focus();
  };

  const submitTurn = (content: string) => {
    if (!activeSessionId || hasUploadInFlight) return;
    let draftId: string;
    try {
      draftId = newSubmissionId();
    } catch (error) {
      setSubmissionError(error instanceof Error ? error.message : "Secure randomness is unavailable.");
      return;
    }
    const original = restoredQuestion?.sessionId === activeSessionId
      ? useSessionStore.getState().questionDrafts[restoredQuestion.key] : undefined;
    if (restoredQuestion && original) {
      if (!beginQuestionTransmission(restoredQuestion.key,
        { kind: "continuation", id: draftId, answers: original.answers, content,
          attachments: pendingAttachments.filter((attachment) => attachment.status === "ready"), state: "pending" })) {
        setSubmissionError("Check the original question receipt before sending this continuation. Your text was kept.");
        return;
      }
      const key = restoredQuestion.key;
      const submission = Promise.resolve().then<void | SubmissionReply>(() => sendMessage(activeSessionId, content,
        readyAttachmentIds.length > 0 ? readyAttachmentIds : undefined, undefined, draftId, original.transmission?.recoveryOfClientId));
      setInput(""); setRestoredQuestion(null); setRestoredInput(null); resetHistory();
      clearAttachments(); setRequiredAttachmentCount(0); setSubmissionError("");
      void Promise.resolve(submission).then((reply) => finishQuestionTransmission(key, draftId, reply, retainSubmittedInput))
        .catch(() => finishQuestionTransmission(key, draftId, { accepted: false, state: "unknown" }));
      return;
    }
    setRestoredQuestion(null);
    setSubmissionError("");
    const draft = {
      id: draftId, sessionId: activeSessionId, text: input,
      attachments: pendingAttachments.filter((attachment) => attachment.status === "ready"),
      ...(restoredInput?.sessionId === activeSessionId ? { recoveryOfClientId: restoredInput.clientId } : {}),
    };
    const submission = draft.recoveryOfClientId ? sendMessage(
      activeSessionId, content, readyAttachmentIds.length > 0 ? readyAttachmentIds : undefined,
      undefined, draft.id, draft.recoveryOfClientId,
    ) : sendMessage(
      activeSessionId,
      content,
      readyAttachmentIds.length > 0 ? readyAttachmentIds : undefined,
      undefined, draft.id
    );
    setInput("");
    setRestoredInput(null);
    resetHistory();
    clearAttachments();
    setRequiredAttachmentCount(0);
    if (submission) {
      submittedDrafts.current.set(draft.id, draft);
      void submission.then((reply) => {
        if (submittedDrafts.current.get(draft.id) !== draft) return;
        submittedDrafts.current.delete(draft.id);
        const outcome = classifySubmission(reply, "message");
        if (outcome !== "settled") setRejectedDrafts((drafts) => [...drafts, {
          ...draft, receipt: reply, reason: reply.message || (outcome === "readonly" ? "This input was withdrawn and cannot be restored."
            : outcome === "recoverable" ? "This input was withdrawn before execution. You can restore it and send it again."
            : outcome === "unknown" ? "Admission is still unconfirmed. Check the original receipt before sending again."
            : "Your message was not accepted."),
          readOnly: outcome === "readonly",
          ...(outcome === "recoverable" && reply.clientRequestId ? { recoveryOfClientId: reply.clientRequestId } : {}),
          ...(outcome === "unknown" ? { uncertain: { id: reply.clientRequestId ?? draft.id } } : {}),
        }]);
        else if (reply.requestId) retainSubmittedInput(reply.requestId, draft);
      }).catch(() => {
        if (submittedDrafts.current.get(draft.id) !== draft) return;
        submittedDrafts.current.delete(draft.id);
        setRejectedDrafts((drafts) => [...drafts, { ...draft, uncertain: { id: draft.id },
          reason: "Delivery was not confirmed. Check the original receipt before sending again." }]);
      });
    }
  };

  const settleCompactCommand = (draft: { sessionId: string; text: string; compactDraft?: { accountGeneration: number; revision: number; version: number } }, accepted: boolean) => {
    const state = useSessionStore.getState();
    if (mounted.current && draft.compactDraft && state.accountGeneration === draft.compactDraft.accountGeneration &&
        state.activeSessionId === draft.sessionId && composerRevision.current === draft.compactDraft.revision) {
      if (accepted) textDraft.clear(draft.compactDraft.version);
      else if (textDraft.store.getState().version === draft.compactDraft.version) textDraft.edit(draft.text);
    }
  };

  const submitCompact = async () => {
    if (!activeSessionId || compactSending.current) return;
    if (rejectedDrafts.some((draft) => draft.sessionId === activeSessionId && draft.compactDraft && draft.uncertain)) {
      setSubmissionError("Confirm the original compaction request before submitting it again.");
      return;
    }
    let id: string;
    try { id = newSubmissionId(); }
    catch (error) { setSubmissionError(error instanceof Error ? error.message : "Secure randomness is unavailable."); return; }
    const state = useSessionStore.getState();
    const draft = { id, sessionId: activeSessionId, text: input, attachments: [],
      compactDraft: { accountGeneration: state.accountGeneration, revision: composerRevision.current, version: textDraft.store.getState().version } };
    const url = `${window.location.origin}/api/sessions/${encodeURIComponent(draft.sessionId)}/submissions`;
    const headers = { Authorization: `Bearer ${state.token}`, "Content-Type": "application/json" };
    const current = () => mounted.current && useSessionStore.getState().accountGeneration === draft.compactDraft.accountGeneration;
    const observer = new AbortController();
    compactSending.current = observer;
    const command = setSubmissionError("");
    textDraft.consume(draft.compactDraft.version);
    try {
      const original = { sessionId: draft.sessionId, clientId: id, operation: "compact" as const };
      let reply: SubmissionReply;
      try {
        reply = await readSubmission(url, { method: "POST", headers, signal: observer.signal,
          body: JSON.stringify({ client_request_id: id, operation: { kind: "compact" } }) }, original);
      } catch {
        if (observer.signal.aborted || !current()) throw new Error("Compaction admission is unconfirmed. Check the original request before sending again.");
        // A lost receipt never authorizes a second native operation.
        reply = await readSubmission(`${url}/${encodeURIComponent(id)}`, { method: "GET", headers, signal: observer.signal }, original);
      }
      if (!current()) return;
      const outcome = classifySubmission(reply, "compact");
      if (outcome === "settled") settleCompactCommand(draft, true);
      else if (outcome === "unknown") setRejectedDrafts((drafts) => [...drafts, { ...draft, uncertain: { id },
        receipt: reply, reason: reply.message || "Compaction admission is unconfirmed." }]);
      else if (outcome === "readonly") setRejectedDrafts((drafts) => [...drafts, { ...draft, readOnly: true, receipt: reply,
        reason: reply.message || "Compaction was withdrawn before execution. No recoverable input was created." }]);
      else {
        settleCompactCommand(draft, false);
        setSubmissionError(reply.message || "Compaction was not accepted.", command);
      }
    } catch (error) {
      if (current()) setRejectedDrafts((drafts) => [...drafts, { ...draft, uncertain: { id },
        reason: error instanceof Error ? error.message : "Compaction admission is unconfirmed." }]);
    } finally { if (compactSending.current === observer) compactSending.current = null; }
  };

  const confirmSubmission = async (draft: typeof rejectedDrafts[number]) => {
    if (!draft.uncertain || draft.confirming) return;
    setRejectedDrafts((drafts) => drafts.map((item) => item.id === draft.id ? { ...item, confirming: true } : item));
    const original = draft.uncertain;
    try {
      const token = useSessionStore.getState().token;
      const operation = draft.compactDraft ? "compact" : "message";
      const reply = await readSubmission(`${window.location.origin}/api/sessions/${encodeURIComponent(draft.sessionId)}/submissions/${encodeURIComponent(original.id)}`, {
        method: "GET", headers: { Authorization: `Bearer ${token}` },
      }, { sessionId: draft.sessionId, clientId: original.id, operation });
      const outcome = classifySubmission(reply, operation);
      if (outcome === "settled" && reply.requestId && !draft.compactDraft) retainSubmittedInput(reply.requestId, draft);
      if (draft.compactDraft && outcome === "settled") {
        settleCompactCommand(draft, true);
        setRejectedDrafts((drafts) => drafts.filter((item) => item.id !== draft.id));
        return;
      }
      const withdrawn = outcome === "recoverable" || outcome === "readonly";
      const recoverable = outcome === "recoverable";
      if (draft.compactDraft && outcome === "rejected") settleCompactCommand(draft, false);
      setRejectedDrafts((drafts) => outcome === "settled" ? drafts.filter((item) => item.id !== draft.id) : drafts.map((item) => item.id === draft.id
        ? { ...item, confirming: false, receipt: reply, reason: reply.message || (recoverable
          ? "This input was withdrawn before execution. You can restore it and send it again."
          : withdrawn ? "This input is read-only and cannot be restored. The saved draft and original request ID were kept."
          : "Admission is still unconfirmed. Check the conversation before sending again."),
          readOnly: outcome === "readonly",
          recoveryOfClientId: recoverable ? original.id : undefined,
          uncertain: outcome === "unknown" ? original : undefined } : item));
    } catch (error) {
      setRejectedDrafts((drafts) => drafts.map((item) => item.id === draft.id ? { ...item, confirming: false,
        reason: `Admission is still unconfirmed. ${error instanceof Error ? error.message : "Try again."} The saved draft and original request ID were kept. Check the conversation before sending again.` } : item));
    }
  };

  const handleSend = async () => {
    if (composerHistory.isWaiting()) return;
    if (!activeSessionId) return;
    const trimmed = input.trim();
    if (/^\/compact(?:\s|$)/.test(trimmed)) {
      if (trimmed !== "/compact") { setSubmissionError("Usage: /compact (no arguments)."); return; }
      await submitCompact();
      return;
    }
    // Allow attachment-only turns — if the user attached files and
    // didn't type anything, we still want to send.
    if (!trimmed && readyAttachmentIds.length === 0) return;
    // Block while uploads are still in flight so we don't drop attachments
    // the user just added but the server hasn't finished receiving.
    if (hasUploadInFlight) return;

    // /reset command — escape hatch for wedged sessions. Calls the
    // server's force-reset endpoint, which cancels the active task,
    // releases the lock, kills the backend subprocess, and clears any
    // pending questions. Use when interrupt failed or the
    // session is stuck in "Running" forever.
    if (trimmed.toLowerCase() === "/reset") {
      setInput("");
      const command = setSubmissionError("");
      const result = await reset(activeSessionId);
      if (result) setSubmissionError(result.error ?? "", command);
      return;
    }

    // /archive command — intercept client-side. Hides the current
    // session and seamlessly swaps the active id to a fresh session
    // with the same name/working_dir/credential_id. The user sees an
    // empty chat under the same sidebar entry.
    if (trimmed.toLowerCase() === "/archive") {
      setInput("");
      const start = useSessionStore.getState();
      const intent = { account: start.accountGeneration, sessionId: activeSessionId };
      viewing.current = intent;
      const version = textDraft.store.getState().version;
      setArchiveFeedback(null);
      for (const retire of archiveObservers.current) retire();
      const tracked = sessionList.observe(activeSessionId, start, "archive");
      const { observation } = tracked;
      let foreground = start.activeSessionId === activeSessionId && start.mainView === "chat";
      let retired = false, retain = false, checking = false;
      let posting = true, hasPostResult = false;
      let knownSuccessor: string | null = null;
      let partialMessage: string | null = null;
      let sharedActivity: SessionArchiveResult | null = null;
      let archived = false;
      const post = new ArchiveDeadline();
      let confirmation: ArchiveDeadline | undefined;
      let unsubscribe = () => {};
      const stop = () => {
        retired = true; foreground = false;
        const accountEnded = !sessionList.accountCurrent(observation);
        if (accountEnded) { post.finish(); confirmation?.finish(); }
        // A bounded command still observes restoration and deletion while
        // producing a list fact after its initiating view has retired.
        if (accountEnded || !posting && !checking) {
          unsubscribe();
          tracked.finish();
        }
        archiveObservers.current.delete(stop);
      };
      const current = () => mounted.current && foreground && viewing.current === intent &&
        sessionList.accountCurrent(observation) && textDraft.current() && textDraft.store.getState().version === version;
      const keepObserving = () => current() &&
        (retain || !hasPostResult && sessionList.archiveActivityCurrent(activeSessionId, observation));
      const failureDetail = (result: SessionArchiveResult) =>
        `${result.failure_stage === "successor_creation" ? "Successor creation" :
          result.failure_stage === "successor_configuration" ? "Successor configuration" : "Schedule rebinding"} failed: ${result.message}`;
      const getActivity = () => hasPostResult ? null : sessionList.archiveActivity(activeSessionId, observation);
      const activityMessage = (activity: SessionArchiveResult | null) => activity ? `Session archived. ${failureDetail(activity)}` : null;
      const syncActivity = () => {
        const activity = getActivity();
        if (sharedActivity === activity) return;
        sharedActivity = activity;
        if (!current()) return;
        setArchiveFeedback((value) => value?.intent === intent
          ? { ...value, activity: activityMessage(activity) }
          : activity ? { intent, version, observation, message: "", activity: activityMessage(activity) } : value);
      };
      const feedback = (message: string, readAgain = false) => {
        retain = readAgain;
        sharedActivity = getActivity();
        if (current()) setArchiveFeedback({ intent, version, observation, message, activity: activityMessage(sharedActivity),
          confirm: readAgain ? () => confirm(true) : undefined, checking });
      };
      const rememberPartial = (result: SessionArchiveResult) => {
        archived = true; knownSuccessor = result.new_session_id;
        partialMessage = `Archive committed. ${failureDetail(result)}`;
        feedback(partialMessage);
      };
      const ownsArchiveEffect = () => sessionList.archiveObservationCurrent(activeSessionId, observation);
      const cancelOriginalFork = () => {
        const original = start.pendingForks[activeSessionId];
        if (ownsArchiveEffect() && original && useSessionStore.getState().pendingForks[activeSessionId] === original) cancelFork(activeSessionId);
      };
      const canCheckSuccessor = () => knownSuccessor !== null && sessionList.archiveReceiptCurrent(observation) &&
        !useSessionStore.getState().sessionSuccessors[activeSessionId];
      const confirm = async (manual: boolean) => {
        if (checking || !sessionList.accountCurrent(observation) || manual && (retired || !current())) return;
        if (manual) confirmation?.finish();
        confirmation = manual || !confirmation ? new ArchiveDeadline() : confirmation;
        checking = true;
        // A manual read may use a new scope without taking over the original
        // command's receipt, successor registration or navigation.
        const reading = manual && !ownsArchiveEffect()
          ? sessionList.observe(activeSessionId, useSessionStore.getState(), "archive") : tracked;
        feedback(partialMessage ?? (archived ? "Archive committed. Checking the session list." :
          "Archive result is unknown. Checking the session list; the command will not be sent again."), true);
        try {
          const result = await sessionList.confirmArchive(activeSessionId, reading.observation, confirmation,
            knownSuccessor ? { successorId: knownSuccessor, original: observation } : undefined);
          if (!sessionList.accountCurrent(observation)) return;
          const activity = getActivity();
          if (result?.archived && !activity) cancelOriginalFork();
          if (partialMessage) { feedback(partialMessage, canCheckSuccessor()); return; }
          if (!result?.archived) {
            const message = archived ? "Archive committed. Its successor could not be confirmed. Confirm the original archive to check again." :
              "Archive result is still unknown. Confirm the original archive to check again; the command will not be sent again.";
            feedback(message, true);
            return;
          }
          if (activity) {
            feedback("Original command result is unknown.");
            return;
          }
          archived = true;
          if (result.successor && !manual && current() && ownsArchiveEffect() &&
              useSessionStore.getState().sessionSuccessors[activeSessionId] === result.successor.id) {
            await selectSession(result.successor.id, result.successor.agent_id);
          } else {
            const message = "Archive confirmed. Viewing the archived conversation.";
            feedback(hasPostResult ? message : `${message} Original command result is unknown.`);
          }
        } finally {
          if (reading !== tracked) reading.finish();
          checking = false;
          confirmation.finish();
          if (current()) setArchiveFeedback((value) => value?.intent === intent ? { ...value, checking: false } : value);
          if (!keepObserving()) stop();
        }
      };
      const unsubscribeList = useSessionStore.subscribe((state) => {
        if (!sessionList.accountCurrent(observation)) { stop(); return; }
        if (state.activeSessionId !== activeSessionId || state.mainView !== "chat") foreground = false;
        syncActivity();
        if (!posting && !checking && !keepObserving()) stop();
      });
      const unsubscribeDraft = textDraft.store.subscribe((state) => { if (state.version !== version) stop(); });
      unsubscribe = () => { unsubscribeList(); unsubscribeDraft(); };
      archiveObservers.current.add(stop);
      try {
        const reply = await postArchive(activeSessionId, start.token, true, post);
        if (!sessionList.accountCurrent(observation)) return;
        hasPostResult = reply.state !== "unknown";
        if (hasPostResult) syncActivity();
        if (reply.state === "refused") {
          feedback(`Archive refused (HTTP ${reply.status})${reply.reason ? `: ${reply.reason}` : "."}`);
          return;
        }
        if (reply.state === "unknown") {
          await confirm(false);
          return;
        }
        archived = true;
        partialMessage = null;
        const fresh = reply.successor;
        if (reply.result) {
          rememberPartial(reply.result);
          if (ownsArchiveEffect() && sessionList.archive(activeSessionId, observation, undefined, reply.result.new_session_id)) cancelOriginalFork();
          if (canCheckSuccessor()) await confirm(false);
          return;
        }
        knownSuccessor = fresh?.id ?? null;
        if (fresh === null) {
          if (ownsArchiveEffect() && sessionList.archive(activeSessionId, observation, undefined, null)) cancelOriginalFork();
          return;
        }
        // The `session_archived` WS broadcast also updates the
        // sessions list for any other clients — and may have already
        // landed in this tab before the HTTP response. Dedupe by id
        // on both removal AND insertion so the two paths converge to
        // the same list regardless of arrival order.
        confirmation = new ArchiveDeadline();
        const accepted = await sessionList.created(fresh, observation, activeSessionId, confirmation);
        confirmation.finish();
        if (!accepted || !sessionList.current(observation)) {
          feedback("Archive committed. Confirm the original archive to check its successor.", true);
          return;
        }
        cancelOriginalFork();
        if (current()) await selectSession(fresh.id, fresh.agent_id);
      } finally {
        posting = false;
        if (!keepObserving()) stop();
      }
      return;
    }

    // /model — which model runs THIS session (per-session override; the
    // agent's own model is untouched). Bare opens the picker; with a name it
    // applies straight away, because typing the name is faster than a dialog
    // once you know what you want. Any name is accepted: the CLIs take names
    // this build cannot know (see RuntimeProfile.models).
    const lowerModel = trimmed.toLowerCase();
    if (lowerModel === "/model" || lowerModel.startsWith("/model ")) {
      setInput("");
      resetHistory();
      const wanted = trimmed.slice("/model".length).trim();
      if (!wanted) {
        setModelPickerOpen(true);
        return;
      }
      const store = useSessionStore.getState();
      const tracked = sessionList.observe(activeSessionId, store);
      const { observation } = tracked;
      const notice = (content: string, isError = false) => {
        if (!sessionList.current(observation)) return;
        store.addMessage(activeSessionId, {
          role: "system",
          type: "notice",
          content,
          is_error: isError,
        });
      };
      try {
        const res = await fetch(
          `${window.location.origin}/api/sessions/${activeSessionId}`,
          {
            method: "PATCH",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${store.token}`,
            },
            // "default" / "agent" clears the override rather than storing a
            // model literally called "default".
            body: JSON.stringify({
              model: ["default", "agent"].includes(wanted.toLowerCase())
                ? null
                : wanted,
            }),
          }
        );
        if (!res.ok) {
          notice(`Could not change the model (HTTP ${res.status}).`, true);
          return;
        }
        const updated = await res.json();
        sessionList.patch(updated.id, { model: updated.model }, observation);
        if (!sessionList.current(observation)) return;
        notice(
          updated.model
            ? `Model for this session: ${updated.model} — from the next turn.`
            : "Model for this session: the agent's default — from the next turn."
        );
      } catch {
        notice("Could not change the model — the server was unreachable.", true);
      } finally {
        tracked.finish();
      }
      return;
    }

    // /rewind — open the rewind picker (session-rewind.md §6.2). Message
    // selection + confirmation happen in the dialog. (The underlying mechanism
    // is still a "fork"/branch internally; only the command name is /rewind.)
    if (trimmed.toLowerCase() === "/rewind") {
      setInput("");
      setForkDialog({ instance: ++forkDialogSequence.current, sessionId: activeSessionId,
        parentName: activeSession?.name || "Session", account: accountGeneration });
      return;
    }

    // /fork [name] — duplicate the whole session onto an INDEPENDENT full copy
    // of its working dir (session-fork.md). A consistent copy + resume
    // transcript needs a SETTLED session, so we can't fork mid-turn. Rather
    // than refuse, we ALWAYS record the intent and let the single watcher fire
    // it: immediately if idle, or once the session goes idle + its queue drains
    // if busy. Recording the intent (never POSTing directly here) is what makes
    // a 409 race recoverable — the watcher owns retry. Optional trailing name
    // overrides the default "<parent> (fork)".
    const lowerFork = trimmed.toLowerCase();
    if (lowerFork === "/fork" || lowerFork.startsWith("/fork ")) {
      setInput("");
      queueFork(activeSessionId, trimmed.slice("/fork".length).trim() || null);
      return;
    }

    // /schedule command — natural-language scheduling. Bare `/schedule` opens
    // the overview; otherwise the whole line goes to the backend, which parses
    // it (explicit "30m …" instantly, else a one-shot AI parse using this
    // agent's Claude) into a recurrence + prompt and creates the schedule.
    // The browser timezone is sent so "9am" means the user's local 9am.
    // Confirmation / errors render as a local notice bubble.
    const lower = trimmed.toLowerCase();
    if (lower === "/schedule" || lower.startsWith("/schedule ")) {
      setInput("");
      const store = useSessionStore.getState();
      const sid = activeSessionId;
      const currentScheduleOperation = schedules.accountGuard();
      const addNotice = (content: string, isError = false) =>
        currentScheduleOperation() && useSessionStore.getState().addMessage(sid, { role: "system", type: "notice", content, is_error: isError });

      const args = trimmed.slice("/schedule".length).trim();
      if (!args) {
        onOpenSchedules();
        return;
      }
      // Echo the command and post a loading notice — the natural-language
      // parse is a one-shot model call and can take a second or two.
      store.addMessage(sid, { role: "user", type: "text", content: trimmed });
      addNotice(`📅 Scheduling “${args}”…`);

      const agentId = activeSession?.agent_id;
      if (!agentId) {
        addNotice("Couldn't schedule: this session has no owning agent.", true);
        return;
      }
      try {
        const created = await schedules.createFromText({ agent_id: agentId, text: args,
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, session_id: sid });
        addNotice(
          `📅 Scheduled "${created.name}" — ${created.recurrence_label}. Manage in Schedules.`
        );
      } catch (error) {
        addNotice(error instanceof Error ? error.message : "Couldn't create schedule — result unknown.", true);
      }
      return;
    }

    // /research command — kick off an Octopus-native deep-research job
    // (native-deep-research.md §7). Returns immediately with a job id; the
    // progress shows in a ResearchCard and the cited report arrives as a turn.
    if (lower === "/research" || lower.startsWith("/research ")) {
      setInput("");
      const store = useSessionStore.getState();
      const sid = activeSessionId;
      const addNotice = (content: string, isError = false) =>
        store.addMessage(sid, { role: "system", type: "notice", content, is_error: isError });
      const question = trimmed.slice("/research".length).trim();
      if (!question) {
        addNotice("Usage: /research <question>", true);
        return;
      }
      store.addMessage(sid, { role: "user", type: "text", content: trimmed });
      addNotice(`🔎 Starting deep research: “${question}”…`);
      const observation = new AbortController();
      researchCreations.current.add(observation);
      try { await createResearch(sid, question, () => mounted.current, observation.signal); }
      finally { researchCreations.current.delete(observation); }
      return;
    }

    // /remember command — persist a note to the agent's long-term memory.
    // Harness-agnostic: we send a memory-writing instruction as a normal
    // turn, and the session's agent (Claude or Codex) writes it using its
    // own file tools against the per-agent memory dir both harnesses share.
    if (lower === "/remember" || lower.startsWith("/remember ")) {
      const note = trimmed.slice("/remember".length).trim();
      if (!note) {
        useSessionStore.getState().addMessage(activeSessionId, {
          role: "system",
          type: "notice",
          content: "Usage: /remember <something to save to memory>",
          is_error: true,
        });
        setInput("");
        return;
      }
      submitTurn(buildRememberPrompt(note));
      return;
    }

    if (lower === "/showme" || lower.startsWith("/showme ")) {
      const store = useSessionStore.getState();
      if (store.activeSessionId !== activeSessionId || store.mainView !== "chat") return;
      const text = trimmed.slice("/showme".length).trim();
      if (!text) {
        useSessionStore.getState().addMessage(activeSessionId, {
          role: "system",
          type: "notice",
          content: "Usage: /showme <file reference>",
          is_error: true,
        });
        setInput("");
        return;
      }

      // Immediate UX feedback: clear the composer, echo the command into
      // the transcript (so it doesn't look like Enter was missed), and
      // post a loading notice (the resolver call can take a second or two
      // when the model has to interpret a fuzzy reference).
      setInput("");
      const intent = { account: store.accountGeneration, sessionId: activeSessionId };
      viewing.current = intent;
      const current = () => {
        const now = useSessionStore.getState();
        return viewing.current === intent && now.accountGeneration === intent.account &&
          now.activeSessionId === intent.sessionId && now.mainView === "chat";
      };
      const failure = (content: string) => {
        if (current()) useSessionStore.getState().addMessage(activeSessionId, { role: "system", type: "notice", content, is_error: true });
      };
      store.addMessage(activeSessionId, {
        role: "user",
        type: "text",
        content: trimmed,
      });
      store.addMessage(activeSessionId, {
        role: "system",
        type: "notice",
        content: `Looking for “${text}”…`,
      });

      try {
        const res = await fetch(
          `${window.location.origin}/api/sessions/${activeSessionId}/showme/resolve`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${store.token}`,
            },
            body: JSON.stringify({ text }),
          }
        );
        if (!current()) return;
        if (!res.ok) {
          let detail = `Couldn't resolve file reference (HTTP ${res.status}).`;
          try {
            const body = await res.json();
            if (typeof body?.detail === "string") detail = body.detail;
          } catch {
            // keep the generic message
          }
          failure(detail);
          return;
        }
        const data = await res.json();
        if (!current()) return;
        if (typeof data?.path === "string" && data.path) {
          useSessionStore.getState().openViewer(activeSessionId, data.path);
        } else {
          failure(typeof data?.message === "string" && data.message ? data.message : "Couldn't resolve that file reference.");
        }
      } catch {
        failure("Couldn't resolve file reference — network error.");
      } finally {
        if (viewing.current === intent) viewing.current = null;
      }
      return;
    }

    submitTurn(trimmed);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    // An IME is mid-composition — Chinese/Japanese/Korean input, where the
    // keystrokes build a candidate the user then commits. During that, Enter
    // *confirms the candidate*; the arrows *move through candidates*; none of
    // it is text navigation or a send. Safari on macOS is the case that bit a
    // user: committing a Chinese word with Enter fired this handler and sent
    // the half-typed message. The composing keydown is flagged either as
    // `isComposing` (the standard) or `keyCode === 229` (older WebKit), so we
    // bow out on both and let the IME own the keys until composition ends.
    if (isImeComposing(e.nativeEvent as KeyboardEvent)) return;
    // When the slash-command menu is open it captures navigation keys:
    // arrows move the highlight, Enter/Tab complete, Esc dismisses (without
    // bubbling to the global Esc-to-interrupt handler).
    if (showSlashMenu) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashIndex((i) => (i + 1) % slashCommands.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashIndex(
          (i) => (i - 1 + slashCommands.length) % slashCommands.length
        );
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        const cmd = slashCommands[Math.min(slashIndex, slashCommands.length - 1)];
        if (cmd) completeSlash(cmd);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        setSlashDismissed(true);
        return;
      }
    }
    // Shell-style history. Only when the slash menu is closed (it owns the
    // arrows above), without modifiers, and only while the caret is on the
    // first/last line — so a recalled multi-line message is still navigable
    // with the same keys.
    if (
      (e.key === "ArrowUp" || e.key === "ArrowDown") &&
      !e.shiftKey &&
      !e.metaKey &&
      !e.ctrlKey &&
      !e.altKey
    ) {
      const ta = e.currentTarget as HTMLTextAreaElement;
      const caret = ta.selectionStart ?? 0;
      const collapsed = (ta.selectionEnd ?? caret) === caret;
      const dir = e.key === "ArrowUp" ? "up" : "down";
      if (collapsed && caretWantsHistory(ta.value, caret, dir)) {
        if (composerHistory.recall(dir, ta.value)) {
          e.preventDefault();
          return;
        }
      }
    }

    if (e.key === "Enter" && !e.shiftKey && !isTouchDevice()) {
      // Touch keyboards have no Shift+Enter worth reaching for, and a return
      // key that sends makes a second paragraph impossible to type. On a
      // phone Enter is a newline and the send button — which is right there
      // under your thumb — sends (mobile.md §3).
      e.preventDefault();
      handleSend();
    }
  };

  // Global Esc handler — interrupt the current turn whether or not the
  // textarea is focused. Skip when typing in another input (e.g. Schedules
  // form) so we don't hijack their native Esc behavior.
  useEffect(() => {
    const onEsc = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented || isImeComposing(e)) return;
      const state = useSessionStore.getState();
      const target = state.sessions.find((session) => session.id === state.activeSessionId) ??
        state.archivedSessions.find((session) => session.id === state.activeSessionId);
      if (state.mainView !== "chat" || !target || target.status !== "running") return;
      const tag = (e.target as HTMLElement | null)?.tagName;
      const inChatInput =
        tag === "TEXTAREA" &&
        (e.target as HTMLElement).closest(".chat-input-bar") !== null;
      const inOtherField =
        (tag === "INPUT" || tag === "TEXTAREA") && !inChatInput;
      if (inOtherField) return;
      e.preventDefault();
      interrupt(target.id);
    };
    window.addEventListener("keydown", onEsc);
    return () => window.removeEventListener("keydown", onEsc);
  }, [interrupt]);

  const renderStatusBadge = (status: string | undefined) => {
    const base = "status-badge font-mono text-[11px]";
    if (status === "running") {
      return (
        <span className={`${base} status-running inline-flex items-center gap-1.5 text-primary`}>
          <span className="inline-block size-1.5 rounded-full bg-primary animate-pulse" />
          running
        </span>
      );
    }
    // Idle stays in the DOM as a test hook and a low-key cue.
    return <span className={`${base} status-idle text-gray-700`}>idle</span>;
  };

  // Delegation child sessions get a small banner under the header that
  // names the parent agent + session and offers a "Open parent" link.
  // The parent_session_id field is set by DelegationManager when the
  // child Session row is created (agent-collaboration.md §4.1).
  const parentSession = useSessionStore((s) => activeSession?.parent_session_id
    ? s.sessions.find(session => session.id === activeSession.parent_session_id)
      ?? s.archivedSessions.find(session => session.id === activeSession.parent_session_id) : undefined);
  const parentAgent = useSessionStore((s) => s.agents.find(agent => agent.id === parentSession?.agent_id));
  const openParent = () => {
    if (!parentSession) return;
    void selectSession(parentSession.id, parentSession.agent_id);
  };

  const header = (
    <PageHeader
      className="chat-header"
      onToggleSidebar={onToggleSidebar}
      icon={
        activeSession && activeAgent ? (
          <span className="tile tile-plain shrink-0" aria-hidden>
            {activeAgent.avatar || "🐙"}
          </span>
        ) : undefined
      }
      crumbs={
        activeSession
          ? [activeAgent?.name ?? "Agent", activeSession.name || "Session"]
          : ["Octopus"]
      }
      meta={activeSession ? renderStatusBadge(activeSession.status) : undefined}
      actions={
        <>
          {activeSession && (
            <Button
              className="btn-model-settings" variant="ghost" size="sm"
              title="Model, reasoning and speed settings" aria-label="Model, reasoning and speed settings"
              onClick={() => setModelPickerOpen(true)}
            >
              <IconCpu size={16} />
              <span className="hidden sm:inline">Model{activeSession.reasoning_effort || activeAgent?.reasoning_effort ? ` · ${activeSession.reasoning_effort || activeAgent?.reasoning_effort}` : " & reasoning"}</span>
              {activeSession.backend === "codex" && (activeSession.service_tier || activeAgent?.service_tier) === "fast" && <span className="font-mono text-xs">Fast</span>}
            </Button>
          )}
          {activeSession && <CredentialPicker session={activeSession} />}
          <span
            className={`conn-status ${connected ? "on" : "off"} pill ${
              connected ? "pill-success" : "pill-warn"
            }`}
            title={connected ? "Connected" : "Disconnected"}
          >
            <span className={`dot ${connected ? "" : "animate-pulse"}`} />
            {/* The word is the first thing to go on a phone; the dot and its
              * colour say the same thing in 6px (mobile.md §4). */}
            <span className="conn-label">
              {connected ? "Connected" : "Disconnected"}
            </span>
          </span>
        </>
      }
    />
  );

  // Fork sessions get a banner mirroring the delegation one
  // (session-rewind.md §6.4): "Forked from <parent> at message N".
  // The parent may be deleted (dangling ref) → "(deleted session)".
  const forkParentSession = useSessionStore((s) => activeSession?.forked_from_session_id
    ? s.sessions.find(session => session.id === activeSession.forked_from_session_id)
      ?? s.archivedSessions.find(session => session.id === activeSession.forked_from_session_id) : undefined);
  const openForkParent = () => {
    if (!forkParentSession) return;
    void selectSession(forkParentSession.id, forkParentSession.agent_id);
  };

  // The dialog confirmed the fork in SessionList; switch to it and load its
  // copied history. The composer adopts fork_prefilled_prompt (§6.1).
  const handleForked = async (fork: SessionInfo, observation: SessionObservation) => {
    if (!sessionList.current(observation)) return;
    await selectSession(fork.id, fork.agent_id);
  };

  const forkBanner =
    activeSession?.forked_from_session_id != null ? (
      <div
        className="chat-fork-banner flex items-center gap-2 px-4 py-1.5 shrink-0 border-b border-border bg-primary/5 text-xs text-muted-foreground"
        data-testid="fork-banner"
      >
        <span className="text-primary" aria-hidden>
          ⑂
        </span>
        <span>
          Forked from{" "}
          <span className="font-medium text-foreground">
            {forkParentSession?.name ?? "(deleted session)"}
          </span>
          {activeSession.fork_is_full_copy ? (
            <>{" "}— full copy of the working dir</>
          ) : (
            <>
              {" "}at message{" "}
              <strong>{(activeSession.fork_after_seq ?? -1) + 1}</strong>
            </>
          )}
        </span>
        {forkParentSession && (
          <button
            type="button"
            onClick={openForkParent}
            className="btn-open-fork-parent ml-auto text-primary hover:underline"
          >
            back to parent →
          </button>
        )}
      </div>
    ) : null;

  const delegationBanner =
    activeSession?.parent_session_id ? (
      <div
        className="chat-delegation-banner flex items-center gap-2 px-4 py-1.5 shrink-0 border-b border-border bg-primary/5 text-xs text-muted-foreground"
        data-testid="delegation-banner"
      >
        <span className="text-primary">↑</span>
        <span>
          Delegated from{" "}
          <span className="font-medium text-foreground">
            {parentAgent?.name || "another agent"}
          </span>
          {parentSession && (
            <>
              {" "}(session{" "}
              <code className="font-mono text-[10px]">
                {parentSession.id.slice(0, 8)}
              </code>
              )
            </>
          )}
        </span>
        {parentSession && (
          <button
            type="button"
            onClick={openParent}
            className="btn-open-parent ml-auto text-primary hover:underline"
          >
            Open parent →
          </button>
        )}
      </div>
    ) : null;

  const savedDrafts = <div className="shrink-0 max-h-72 overflow-auto px-6">
          {saveError && <p role="alert" className="mb-2 text-sm text-destructive">{saveError}</p>}
          {submissionError && submissionError.command === submissionCommand.current &&
            submissionError.command.account === accountGeneration && submissionError.command.sessionId === activeSessionId &&
            <p role="alert" className="mb-2 text-sm text-destructive">{submissionError.message}</p>}
          {archiveFeedback && (archiveFeedback.message || archiveFeedback.activity) && viewing.current === archiveFeedback.intent &&
            archiveFeedback.intent.account === accountGeneration && archiveFeedback.intent.sessionId === activeSessionId &&
            textDraft.store.getState().version === archiveFeedback.version && sessionList.accountCurrent(archiveFeedback.observation) &&
            <div role="alert" className="mb-2 text-sm text-destructive">
              {archiveFeedback.message && <p>{archiveFeedback.message}</p>}
              {archiveFeedback.activity && <p>{archiveFeedback.activity}</p>}
              {archiveFeedback.confirm && <Button size="sm" variant="outline" disabled={archiveFeedback.checking}
                onClick={() => void archiveFeedback.confirm?.()}>Confirm original archive</Button>}
            </div>}
          {activeSessionId && <QuestionDraftRecovery sessionId={activeSessionId}
            composerOccupied={input.length > 0 || pendingAttachments.length > 0 || isArchived}
            onAcceptedInput={retainSubmittedInput}
            onContinue={(key, content, attachments) => {
              setInput(content); setRestoredQuestion({ key, sessionId: activeSessionId });
              restoreAttachments(attachments);
              setRestoredInput(null); resetHistory(); textareaRef.current?.focus();
            }} />}
          {rejectedDrafts.map((draft) => (
            <div key={draft.id} role="alert" className="mb-2 rounded-md border border-destructive/40 p-3 text-sm">
              <p>{draft.reason}</p>
              {(!activeSessionId || isArchived) && <p>Open an active conversation to restore this draft.</p>}
              <details><summary>Saved draft</summary>
                <pre className="max-h-40 overflow-auto whitespace-pre-wrap">{draft.text}</pre>
                {draft.attachments.map((attachment) => <p key={attachment.uid}>
                  <button type="button" className="text-primary underline" onClick={() => {
                    const url = URL.createObjectURL(attachment.file);
                    const link = document.createElement("a");
                    link.href = url; link.download = attachment.file.name; link.click();
                    setTimeout(() => URL.revokeObjectURL(url), 0);
                  }}>Download {attachment.file.name}</button></p>)}
              </details>
              {draft.uncertain && <Button size="sm" variant="outline" className="mt-2"
                disabled={draft.confirming}
                onClick={() => void confirmSubmission(draft)}>{draft.confirming ? "Confirming…" : "Retry admission confirmation"}</Button>}
              <Button size="sm" variant="outline" className="mt-2"
                disabled={draft.readOnly || Boolean(draft.uncertain) || !activeSessionId || isArchived || input.length > 0 || pendingAttachments.length > 0}
                onClick={() => {
                  if (draft.readOnly || draft.uncertain || !activeSessionId || isArchived || input.length > 0 || pendingAttachments.length > 0) return;
                  setInput(draft.text);
                  setRestoredInput(draft.recoveryOfClientId && draft.sessionId === activeSessionId
                    ? { sessionId: activeSessionId, clientId: draft.recoveryOfClientId } : null);
                  restoreAttachments(draft.attachments);
                  setRequiredAttachmentCount(0);
                  resetHistory();
                  setRejectedDrafts((drafts) => drafts.filter((item) => item.id !== draft.id));
                  textareaRef.current?.focus();
                }}>Restore draft</Button>
              <Button size="sm" variant="ghost" className="ml-2 mt-2"
                onClick={() => setRejectedDrafts((drafts) => drafts.filter((item) => item.id !== draft.id))}>Discard saved draft</Button>
              {(input.length > 0 || pendingAttachments.length > 0) && <p className="mt-1 text-xs text-muted-foreground">Clear the current draft to restore this one.</p>}
            </div>
          ))}
  </div>;

  if (!activeSessionId) {
    return (
      <div className="chat-view flex-1 flex flex-col min-h-0">
        {header}
        {delegationBanner}
        {savedDrafts}
        <div className="chat-empty flex flex-1 flex-col items-center justify-center gap-2 px-8 text-center text-gray-700">
          {/* The one place the mark gets room to be itself — nothing else is
            * competing for attention on an empty canvas. */}
          <OctopusLogo size={44} className="mb-3" />
          {/* Centred *and* bounded: as a flex item this line sizes to its own
            * content, so on a phone it grew wider than the screen and lost a
            * word off each edge (mobile.md §7). */}
          <p className="max-w-[26rem] text-[13.5px] leading-relaxed">
            Pick a session from the sidebar, or start a new one with{" "}
            <span className="font-mono text-primary">+</span> on an agent.
          </p>
          {/* On a phone the sidebar is behind the menu, so the empty state
            * offers the way in rather than pointing at something off-screen. */}
          <button
            type="button"
            className="btn-empty-browse mt-2 inline-flex items-center gap-1.5 rounded-lg border-[0.7px] border-gray-400 bg-card px-3 py-2 text-[13px] font-medium text-gray-900 transition-colors active:bg-gray-100 md:hidden"
            onClick={onToggleSidebar}
          >
            <IconMenu2 size={15} />
            Browse sessions
          </button>
        </div>
      </div>
    );
  }

  const showReadingNotice = readingBoundary || presentation.owner.timedOut && !following.current;
  return (
    <div
      className="chat-view flex-1 flex flex-col min-h-0 relative"
      onDragEnter={isArchived || transcriptDamaged ? undefined : handleDragEnter}
      onDragLeave={isArchived || transcriptDamaged ? undefined : handleDragLeave}
      onDragOver={isArchived || transcriptDamaged ? undefined : handleDragOver}
      onDrop={isArchived || transcriptDamaged ? undefined : handleDrop}
    >
      {header}
      {forkBanner}
      {delegationBanner}
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

      {activeSession && (
        <ModelPickerDialog
          session={activeSession}
          open={modelPickerOpen}
          onOpenChange={setModelPickerOpen}
          onApplied={(updated) =>
            activeSessionId &&
            useSessionStore.getState().addMessage(activeSessionId, {
              role: "system",
              type: "notice",
              content: `Model for this session: ${updated.model || "the agent's default"}; reasoning effort: ${updated.reasoning_effort || "the agent's default"}${updated.backend === "codex" ? `; speed: ${(updated.service_tier || activeAgent?.service_tier) === "fast" ? "Fast" : "Normal"}` : ""} — from the next turn.`,
            })
          }
        />
      )}

      {forkDialog && forkDialog.account === accountGeneration && (
        <ForkDialog
          key={forkDialog.instance}
          sessionId={forkDialog.sessionId}
          parentName={forkDialog.parentName}
          initialSeq={forkDialog.seq}
          onClose={() => setForkDialog((current) => current === forkDialog ? null : current)}
          onForked={handleForked}
        />
      )}

      {isDragOver && !isArchived && (
        <div className="chat-drop-overlay absolute inset-0 z-20 flex items-center justify-center bg-primary/10 border-2 border-dashed border-primary/60 pointer-events-none">
          <div className="rounded-lg bg-background/90 px-6 py-4 text-center shadow-lg">
            <IconPaperclip
              size={24}
              className="mx-auto mb-1 text-primary"
              aria-hidden
            />
            <div className="text-sm font-medium text-foreground">
              Drop files to attach
            </div>
            <div className="text-xs text-muted-foreground mt-0.5">
              Up to {MAX_ATTACHMENTS_PER_MESSAGE} files, 25 MB each
            </div>
          </div>
        </div>
      )}

      <div className="relative flex flex-1 min-h-0"
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
          navigate(up, scroller.current!.scrollTop);
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
        itemSize={measureItem}
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
        increaseViewportBy={{ top: 400, bottom: 400 }}
        components={{ Header: olderHeader, Footer: ChatFooter }}
        context={{ footer, onLayout: measuredOutput }}
      />

      {(presentation.phase === "preparing" || presentation.phase === "incomplete") && <div data-chat-preparation
        tabIndex={0} aria-label="Conversation preparation"
        className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-4 text-sm text-muted-foreground">
        <p role="status">{presentation.phase === "incomplete" ? "Conversation unavailable" : transcriptBaseline ? "Positioning latest output…" : "Loading conversation…"}</p>
        {presentation.owner.error && <p>{presentation.owner.error}</p>}
        <Button size="sm" variant="outline" onClick={() => pauseFollowing()}>Browse loaded content</Button>
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

      {activeSessionId && (
        <div className="research-card-wrap shrink-0 px-4 empty:hidden">
          <WikiJobCards key={activeSessionId} sessionId={activeSessionId} />
          <ResearchCard sessionId={activeSessionId} />
        </div>
      )}

      {activeSessionId && <DispatchQueue key={activeSessionId} sessionId={activeSessionId} archived={isArchived}
        readOnly={isArchived || transcriptDamaged} onCancelCompact={cancelCompact} />}
      {activeSessionId && <CommandNotices sessionId={activeSessionId} />}
      {activeSessionId && <SessionAuxiliaryNotice sessionId={activeSessionId} />}

      {activeSessionId && <RecoverableInputs key={`recovery-${activeSessionId}`} sessionId={activeSessionId} connected={connected}
        disabled={isArchived || transcriptDamaged || input.length > 0 || pendingAttachments.length > 0}
        onRestore={restoreSavedInput} />}

      {savedDrafts}
      {isArchived || transcriptDamaged ? (
        <div className="chat-archived-banner px-4 py-3 border-t border-border bg-muted/40 text-sm text-muted-foreground flex items-center justify-between gap-3 shrink-0">
          {transcriptDamaged ? <span>History review is required before continuing this conversation.</span> : <span>
            This session is <strong className="text-foreground">archived</strong>{" "}
            — viewing read-only history. Unarchive from the sidebar to
            continue the conversation.
          </span>}
          {isArchived && activeSessionId && <ArchivedSuccessor key={`${accountGeneration}:${activeSessionId}`} sessionId={activeSessionId} />}
          <ContextMeter key={activeSessionId} state={contextState} historical={isArchived} pending={contextSnapshot?.pending} />
        </div>
      ) : (
        <div className="chat-input-bar shrink-0 bg-background px-6 pb-5 pt-2">

          {dispatchPaused && <p className="dispatch-pause-hint mb-2 text-xs text-muted-foreground">
            Your next message takes priority. Pending results stay paused until you resume processing.
          </p>}
          {/* `relative` anchors the slash-command menu, which floats just
              above the composer (bottom-full) like the Claude Code CLI. */}
          <div className="relative">
          {showSlashMenu && (
            <SlashCommandMenu
              commands={slashCommands}
              activeIndex={slashIndex}
              onSelect={completeSlash}
              onHoverIndex={setSlashIndex}
              className="absolute inset-x-0 bottom-full mb-2"
            />
          )}
          {/* Rounded card containing chips + textarea + bottom action row.
              Layout copied from VM0 (zero-composer) but tuned shorter
              for Octopus' chat panel: the textarea auto-grows with
              content (field-sizing-content) so the empty composer is a
              single comfortable line, not a hero-sized block. */}
          <div className="zero-composer overflow-hidden rounded-[14px] border border-gray-400 bg-card transition-colors focus-within:border-primary focus-within:ring-[3px] focus-within:ring-primary/10">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              data-testid="attachment-file-input"
              onChange={(e) => {
                const files = Array.from(e.target.files ?? []);
                addFiles(files);
                // Reset so picking the same file twice in a row still fires.
                e.target.value = "";
              }}
            />

            {rejectedFiles.length > 0 && <div role="alert" className="px-3 pt-2 text-sm text-destructive">
              <p>These files were not attached:</p>
              <ul>{rejectedFiles.map((file, index) => <li key={index}>{file.name}: {file.reason}</li>)}</ul>
              <button type="button" className="underline" onClick={dismissRejectedFiles}>Continue with attached files</button>
            </div>}
            {pendingAttachments.length < requiredAttachmentCount && <div role="alert" className="px-3 pt-2 text-sm text-destructive">
              Re-attach {requiredAttachmentCount} {requiredAttachmentCount === 1 ? "file" : "files"} to send the complete message.
              <button type="button" className="ml-2 underline" onClick={() => {
                setInput(""); clearAttachments(); setRequiredAttachmentCount(0); setRestoredInput(null);
              }}>Discard draft</button>
            </div>}
            {pendingAttachments.length > 0 && (
              <div
                className="chat-attachment-chips flex flex-wrap gap-2 px-3 pt-2"
                aria-label="Attachments"
              >
                {pendingAttachments.map((p) => (
                  <div
                    key={p.uid}
                    className={`attachment-pending inline-flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-xs ${
                      p.status === "error"
                        ? "border-destructive/60 bg-destructive/5 text-destructive"
                        : "border-border bg-muted/40 text-foreground"
                    }`}
                    title={
                      p.status === "error"
                        ? p.error || "upload failed"
                        : p.file.name
                    }
                  >
                    <IconFile
                      size={14}
                      className="text-muted-foreground shrink-0"
                    />
                    <span className="font-medium truncate max-w-[10rem]">
                      {p.file.name}
                    </span>
                    {p.status === "uploading" && (
                      <span className="text-muted-foreground">uploading…</span>
                    )}
                    {p.status === "error" && (
                      <button type="button" className="text-destructive underline"
                        aria-label={`Retry ${p.file.name}`} onClick={() => retryAttachment(p.uid)}>failed · retry</button>
                    )}
                    <button
                      type="button"
                      className="attachment-remove text-muted-foreground hover:text-foreground"
                      aria-label={`Remove ${p.file.name}`}
                      onClick={() => removeAttachment(p.uid)}
                    >
                      <IconX size={12} />
                    </button>
                  </div>
                ))}
              </div>
            )}

            {composerHistory.waiting && <p role="status" className="px-3 text-xs text-muted-foreground">Loading full history message…</p>}
            {composerHistory.error && <p role="alert" className="px-3 text-xs text-destructive">{composerHistory.error}</p>}
            <textarea
              ref={textareaRef}
              value={input}
              onChange={handleInputChange}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
              placeholder={
                isRunning
                  ? "Send to queue, or press Esc to interrupt…"
                  : "Send a message…"
              }
              rows={1}
              enterKeyHint={isTouchDevice() ? "enter" : "send"}
              // field-sizing-content makes the textarea grow with its
              // content (Tailwind v4 / native CSS), so the empty state is
              // one comfortable line and we don't need JS auto-grow.
              // max-h caps growth before it eats the chat.
              className="w-full resize-none field-sizing-content bg-transparent px-3 pt-2 pb-0 text-sm text-foreground placeholder:text-muted-foreground/50 border-0 outline-none focus:outline-none focus:ring-0 max-h-48"
            />

            <div className="composer-actions flex items-center justify-between gap-2 px-1.5 pb-1.5">
              <div className="flex items-center gap-1 text-muted-foreground">
                <button
                  type="button"
                  className="btn btn-attach inline-flex items-center justify-center size-8 rounded-lg transition-colors duration-200 hover:bg-accent hover:text-foreground disabled:opacity-40 disabled:cursor-not-allowed"
                  aria-label="Attach file"
                  title="Attach file"
                  disabled={
                    pendingAttachments.length >= MAX_ATTACHMENTS_PER_MESSAGE
                  }
                  onClick={() => fileInputRef.current?.click()}
                >
                  <IconPaperclip size={16} stroke={1.5} />
                </button>
                <ContextMeter key={activeSessionId} state={contextState} pending={contextSnapshot?.pending} />
              </div>
              <div className="flex items-center gap-1">
                {isRunning ? (
                  <Button
                    type="button"
                    variant="destructive"
                    size="sm"
                    className="btn btn-stop rounded-lg h-8 w-8 p-0 shrink-0"
                    onClick={() =>
                      activeSessionId && interrupt(activeSessionId)
                    }
                    aria-label="Stop"
                    title="Stop (Esc)"
                  >
                    <IconPlayerStop size={14} />
                  </Button>
                ) : null}
                <Button
                  type="button"
                  size="sm"
                  className="btn btn-send size-9 shrink-0 rounded-full p-0"
                  onClick={handleSend}
                  disabled={
                    composerHistory.waiting ||
                    (hasUploadInFlight && !/^\/compact(?:\s|$)/.test(input.trim())) ||
                    (!input.trim() && readyAttachmentIds.length === 0)
                  }
                  aria-label={
                    isRunning
                      ? canSteer
                        ? "Send to running turn"
                        : "Queue message"
                      : "Send message"
                  }
                  title={
                    isRunning
                      ? canSteer
                        ? "Send into the running turn — the agent gets it at its next step"
                        : "Queue (current turn is running)"
                      : "Send"
                  }
                >
                  <IconArrowUp size={16} />
                </Button>
              </div>
            </div>
          </div>
          </div>
        </div>
      )}
    </div>
  );
}

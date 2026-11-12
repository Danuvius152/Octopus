import { accountFetch as fetch } from "../lib/accountFetch";
import { schedules } from "../lib/scheduleList";
import { sessions as sessionList, type SessionObservation } from "../lib/sessionList";
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
  useSessionStore,
  type Message,
  type PendingQuestion,
  type SessionInfo,
} from "../stores/sessionStore";
import { ForkDialog } from "./ForkDialog";
import { ResearchCard } from "./ResearchCard";
import { RecoverableInputs } from "./RecoverableInputs";
import type { ClientSubmissionStatus } from "../api";
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
import { isAutoInjectedPrompt } from "../lib/injectedTurns";
import {
  IDLE as HISTORY_IDLE,
  caretWantsHistory,
  historyFromMessages,
  recallNext,
  recallPrev,
} from "../lib/composerHistory";
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
import type { WikiMessageMode } from "../lib/wikiComposer";
import { newSubmissionId, readSubmission, type QuestionCommandTarget, type SubmissionReply } from "../lib/submission";
import { MAX_ATTACHMENTS_PER_MESSAGE, useDraftAttachments, type PendingAttachment } from "../hooks/useDraftAttachments";

const EMPTY_MESSAGES: Message[] = [];

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

/** A nested output pane can consume navigation or block scroll chaining. */
function canScrollChatUp(viewport: HTMLElement | null, target: EventTarget | null): boolean {
  if (!viewport?.clientHeight || viewport.scrollTop <= 0) return false;
  let element = target instanceof Element ? target : null;
  while (element && element !== viewport) {
    const style = getComputedStyle(element);
    if (/^(auto|scroll)$/.test(style.overflowY) && element.scrollHeight > element.clientHeight
      && (element.scrollTop > 0 || style.overscrollBehaviorY !== "auto")) return false;
    element = element.parentElement;
  }
  return element === viewport;
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
function ChatFooter({ context }: { context?: { footer: ReactNode } }) {
  return context?.footer;
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
  const setInput = useCallback((text: string) => textDraft.edit(text), [textDraft]);
  useEffect(() => () => textDraft.clearConsumed(), [textDraft]);
  const mounted = useRef(true);
  const archiveObservers = useRef(new Set<() => void>());
  useEffect(() => {
    mounted.current = true;
    const observers = archiveObservers.current;
    return () => {
      mounted.current = false;
      for (const stop of observers) stop();
      observers.clear();
    };
  }, []);
  const [restoredInput, setRestoredInput] = useState<{ sessionId: string; clientId: string } | null>(null);
  const [restoredQuestion, setRestoredQuestion] = useState<{ sessionId: string; key: string } | null>(null);
  const [requiredAttachmentCount, setRequiredAttachmentCount] = useState(0);
  const submittedDrafts = useRef(new Map<string, { sessionId: string; text: string; attachments: PendingAttachment[] }>());
  const [submissionError, setSubmissionError] = useState("");
  const [rejectedDrafts, setRejectedDrafts] = useState<{
    id: string; sessionId: string; text: string; attachments: PendingAttachment[];
    reason: string;
    recoveryOfClientId?: string;
    uncertain?: { id: string };
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
  // Composer history (shell-style ArrowUp/ArrowDown).
  const [history, setHistory] = useState(HISTORY_IDLE);
  const [slashDismissed, setSlashDismissed] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  // Counter for nested dragenter/dragleave events — without this the
  // overlay flickers as the cursor crosses child elements (textarea,
  // chips, etc.) because each crossing fires a leave on the parent.
  const dragCounter = useRef(0);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
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
  const { attachments: pendingAttachments, addFiles, removeAttachment, retryAttachment,
    restoreAttachments, clearAttachments, rejectedFiles, dismissRejectedFiles, unready,
    readyIds: readyAttachmentIds } = useDraftAttachments(activeSessionId);
  useEffect(() => {
    const discard = (sessionId?: string) => {
      for (const [id, draft] of submittedDrafts.current) {
        if (!sessionId || draft.sessionId === sessionId) submittedDrafts.current.delete(id);
      }
      setRejectedDrafts((drafts) => drafts.filter((draft) => sessionId && draft.sessionId !== sessionId));
      setRestoredInput((draft) => draft && sessionId && draft.sessionId !== sessionId ? draft : null);
      setRestoredQuestion((draft) => draft && sessionId && draft.sessionId !== sessionId ? draft : null);
      if (!sessionId || sessionId === activeSessionId) {
        clearAttachments(); setRequiredAttachmentCount(0); setSubmissionError(""); setHistory(HISTORY_IDLE);
      }
    };
    const deleted = sessionList.onDeleted(discard);
    const account = useSessionStore.subscribe((state, before) => {
      if (state.accountGeneration !== before.accountGeneration) discard();
    });
    return () => { deleted(); account(); };
  }, [activeSessionId, clearAttachments]);
  const messagesMap = useSessionStore((s) => s.messages);
  const streamingMap = useSessionStore((s) => s.streamingText);
  const streamingItems = useSessionStore((s) => s.streamingItems);
  const messages = activeSessionId ? (messagesMap[activeSessionId] ?? EMPTY_MESSAGES) : EMPTY_MESSAGES;
  const streamingText = activeSessionId ? (streamingMap[activeSessionId] ?? "") : "";
  const subagentSnapshots = useSessionStore((s) => s.subagents);
  const nativeStreaming = useMemo(() => Object.entries(activeSessionId ? streamingItems[activeSessionId] || {} : {})
    .filter(([, item]) => !isNativeChild(item.runtime_metadata)), [activeSessionId, streamingItems]);
  const sessions = useSessionStore((s) => s.sessions);
  const archivedSessions = useSessionStore((s) => s.archivedSessions);
  const agents = useSessionStore((s) => s.agents);
  const activeSession = useMemo(
    () =>
      sessions.find((s) => s.id === activeSessionId) ??
      archivedSessions.find((s) => s.id === activeSessionId),
    [sessions, archivedSessions, activeSessionId]
  );
  const activeAgent = useMemo(
    () => agents.find((a) => a.id === activeSession?.agent_id),
    [agents, activeSession?.agent_id]
  );
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
    setHistory(HISTORY_IDLE);
    setRestoredInput({ sessionId: activeSessionId, clientId: saved.client_request_id });
    textareaRef.current?.focus();
  }, [activeSessionId, input, pendingAttachments, isArchived, transcriptDamaged, addFiles, setInput]);
  const dispatchStates = useSessionStore((s) => s.dispatchStates);
  const chatMessages = useMemo(() => nativeChatMessages(messages,
    Object.values(activeSessionId ? subagentSnapshots[activeSessionId] || {} : {}),
    activeSessionId ? dispatchStates[activeSessionId]?.active_request_id : undefined),
  [messages, activeSessionId, subagentSnapshots, dispatchStates]);
  const dispatchPaused = activeSessionId ? dispatchStates[activeSessionId]?.paused ?? false : false;
  const pendingQuestionsMap = useSessionStore((s) => s.pendingQuestions);
  const pendingQuestions = activeSessionId
    ? (pendingQuestionsMap[activeSessionId] ?? EMPTY_QUESTIONS)
    : EMPTY_QUESTIONS;
  const { queueFork, cancelFork, reset } = useSessionFork();

  // A prepend can also move or regroup child cards. Preserve the actual
  // visible identity and offset, independently of total row counts.
  const virtuoso = useRef<VirtuosoHandle>(null);
  const scroller = useRef<HTMLElement | null>(null);
  const pageAnchor = useRef<{ key: string; offset: number } | null>(null);
  // Input retires follow intent before queued layout work can overwrite it.
  // Position alone cannot distinguish native navigation from virtual layout.
  const following = useRef(true);
  const followFrame = useRef<number | null>(null);
  const viewportObserver = useRef<ResizeObserver | null>(null);
  const lastScrollTop = useRef(0);
  const navigationPointer = useRef<{ id: number; y: number; top: number; resume: boolean } | null>(null);
  const cancelFollow = useCallback(() => {
    if (followFrame.current !== null) cancelAnimationFrame(followFrame.current);
    followFrame.current = null;
  }, []);
  const pauseFollowing = useCallback(() => {
    following.current = false;
    cancelFollow();
  }, [cancelFollow]);
  const pauseForReading = useCallback((event: MouseEvent<HTMLElement>) => {
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest("button[aria-expanded], summary")) pauseFollowing();
  }, [pauseFollowing]);
  const followMeasuredOutput = useCallback(() => {
    const viewport = scroller.current;
    if (!following.current || pageAnchor.current || !viewport?.clientHeight || followFrame.current !== null) return;
    followFrame.current = requestAnimationFrame(() => {
      followFrame.current = null;
      if (scroller.current !== viewport || !following.current || pageAnchor.current || !viewport.clientHeight) return;
      // Use the actual post-measurement extent, including the live Footer.
      // One instant write avoids a smooth-scroll retry outliving user intent.
      if (viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop > 1) {
        virtuoso.current?.scrollTo({ top: viewport.scrollHeight, behavior: "auto" });
      }
      lastScrollTop.current = viewport.scrollTop;
    });
  }, []);
  const setScroller = useCallback((element: HTMLElement | Window | null) => {
    cancelFollow();
    viewportObserver.current?.disconnect();
    viewportObserver.current = null;
    navigationPointer.current = null;
    scroller.current = element instanceof HTMLElement ? element : null;
    const viewport = scroller.current;
    lastScrollTop.current = viewport?.scrollTop ?? 0;
    if (scroller.current) {
      // Virtuoso publishes this ref after layout effects. Attach to the
      // actual current node, including replacements and hidden-view returns.
      viewportObserver.current = new ResizeObserver(followMeasuredOutput);
      viewportObserver.current.observe(scroller.current);
    }
  }, [cancelFollow, followMeasuredOutput]);
  const [loadingOlder, setLoadingOlder] = useState(false);
  // Whether the conversation has been parked at its newest message yet. Until
  // it has, "at the top" means nothing: the list mounts at scroll 0 and
  // reports its top as reached before there is anything to scroll to.
  const [parked, setParked] = useState(false);
  // Whether the list is scrolled to its top, and whether the last automatic
  // load there came back empty (a failed fetch). The second stops a failure
  // from retrying in a tight loop; leaving the top clears it, so coming back
  // tries again — and the header's button always works.
  const [atTop, setAtTop] = useState(false);
  const [olderStalled, setOlderStalled] = useState(false);
  const olderRead = useRef<ReturnType<typeof observeOlderMessages> | null>(null);
  const messageWindow = useSessionStore((state) => activeSessionId ? state.messageWindows[activeSessionId] : undefined);
  const hasOlder = useSessionStore((s) =>
    activeSessionId ? Boolean(s.hasMoreMessages[activeSessionId]) : false
  );
  useLayoutEffect(() => {
    cancelFollow();
    following.current = true;
    navigationPointer.current = null;
    pageAnchor.current = null;
    setLoadingOlder(false);
    setParked(false);
    setAtTop(false);
    setOlderStalled(false);
    return () => { cancelFollow(); olderRead.current?.cancel(); olderRead.current = null; };
  }, [activeSessionId, messageWindow, cancelFollow]);
  const loadOlder = useCallback(() => {
    const sid = activeSessionId;
    if (!sid || olderRead.current?.current()) return;
    pauseFollowing();
    setLoadingOlder(true);
    const read = observeOlderMessages(sid, () => {
      const viewport = scroller.current?.getBoundingClientRect();
      if (!viewport) return;
      const visible = Array.from(scroller.current!.querySelectorAll<HTMLElement>("[data-message-key]")).find((element) => {
        const rectangle = element.getBoundingClientRect();
        return rectangle.bottom > viewport.top && rectangle.top < viewport.bottom;
      });
      pageAnchor.current = visible ? { key: visible.dataset.messageKey!, offset: visible.getBoundingClientRect().top - viewport.top } : null;
    });
    olderRead.current = read;
    void read.done.then((result) => {
      // Store publication and this view's request status have different lives.
      // An obsolete connection must release its own spinner, while an old view
      // can never settle a replacement view's request.
      if (olderRead.current !== read) return;
      olderRead.current = null;
      setLoadingOlder(false);
      if (!read.current() || result.state === "obsolete") {
        pageAnchor.current = null;
        setOlderStalled(true);
        return;
      }
      if (result.state === "failed" || result.state === "in_flight" || result.state === "merged" && result.added === 0) {
        pageAnchor.current = null;
        setOlderStalled(true);
      }
    });
  }, [activeSessionId, pauseFollowing]);

  useLayoutEffect(() => {
    const anchor = pageAnchor.current;
    if (!anchor) return;
    const index = chatMessages.findIndex((message) => chatMessageKey(message) === anchor.key);
    if (index < 0) { pageAnchor.current = null; return; }
    virtuoso.current?.scrollToIndex({ index, align: "start", offset: -anchor.offset });
    const frame = requestAnimationFrame(() => {
      const viewport = scroller.current;
      const element = Array.from(viewport?.querySelectorAll<HTMLElement>("[data-message-key]") || [])
        .find((row) => row.dataset.messageKey === anchor.key);
      if (viewport && element) viewport.scrollTop += element.getBoundingClientRect().top - viewport.getBoundingClientRect().top - anchor.offset;
      pageAnchor.current = null;
    });
    return () => cancelAnimationFrame(frame);
  }, [chatMessages, loadingOlder]);

  // Remember reaching the top even if Virtuoso's initial bottom report lands
  // afterward. The bottom report arms paging; the mount-time scroll 0 alone
  // must never fetch history before the reader has seen the latest message.
  useEffect(() => {
    if (atTop && parked && hasOlder && !loadingOlder && !olderStalled) {
      loadOlder();
    }
  }, [atTop, parked, hasOlder, loadingOlder, olderStalled, loadOlder]);

  // Mount the transcript with its first loaded window so Virtuoso owns the
  // initial positioning. Repeated imperative scrolls can finish after the
  // reader has already scrolled back and steal that position.
  const hasMessages = messages.length > 0;

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
    (!activeSessionId || (dispatchStates[activeSessionId]?.active_operation !== "compact" &&
      !dispatchStates[activeSessionId]?.items.some((item) => item.operation === "compact")));

  useEffect(() => {
    for (const [requestId, draft] of submittedDrafts.current) {
      const result = messagesMap[draft.sessionId]?.find((message) => message.outcome?.request_id === requestId);
      if (result && result.outcome?.code !== "attachment_unavailable") submittedDrafts.current.delete(requestId);
    }
  }, [messagesMap]);

  const restoreRejectedInput = useCallback((message: Message) => {
    if (!activeSessionId || input.length > 0 || pendingAttachments.length > 0) return;
    const recovery = message.tool_input?.rejected_input as { content?: unknown; attachment_ids?: unknown } | undefined;
    if (typeof recovery?.content !== "string" || !Array.isArray(recovery.attachment_ids)) return;
    const requestId = message.outcome?.request_id;
    const draft = requestId ? submittedDrafts.current.get(requestId) : undefined;
    setInput(recovery.content);
    setHistory(HISTORY_IDLE);
    if (draft) {
      restoreAttachments(draft.attachments);
      submittedDrafts.current.delete(requestId!);
      setRequiredAttachmentCount(0);
    } else {
      // After a reload the browser no longer has File objects. Keep the
      // complete-input requirement visible until files are selected again.
      setRequiredAttachmentCount(recovery.attachment_ids.length);
    }
    textareaRef.current?.focus();
  }, [activeSessionId, input.length, pendingAttachments.length, restoreAttachments, setInput]);

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
        <div className="msg-row group/msg relative" data-message-key={chatMessageKey(msg)}>
          {body}
          <MessageTime iso={msg.created_at} />
        </div>
      );
    },
    [renderMessageBody, pendingQuestions, activeSessionId]
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
  // What ArrowUp walks back through. A user-role message the user never typed —
  // a bg-task result, a delegation reply, a scheduled fire — is machine text
  // wearing a user's hat, and recalling it into the box would be nonsense.
  const sentHistory = useMemo(
    () => historyFromMessages(messages, isAutoInjectedPrompt),
    [messages]
  );
  const showSlashMenu = slashCommands.length > 0 && !slashDismissed;

  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    // Editing ends the walk: the next ArrowUp starts from the newest again.
    setHistory(HISTORY_IDLE);
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
        readyAttachmentIds.length > 0 ? readyAttachmentIds : undefined, undefined, draftId));
      setInput(""); setRestoredQuestion(null); setRestoredInput(null); setHistory(HISTORY_IDLE);
      clearAttachments(); setRequiredAttachmentCount(0); setSubmissionError("");
      void Promise.resolve(submission).then((reply) => finishQuestionTransmission(key, draftId, reply))
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
      undefined, undefined, draft.recoveryOfClientId,
    ) : sendMessage(
      activeSessionId,
      content,
      readyAttachmentIds.length > 0 ? readyAttachmentIds : undefined,
      undefined
    );
    setInput("");
    setRestoredInput(null);
    setHistory(HISTORY_IDLE);
    clearAttachments();
    setRequiredAttachmentCount(0);
    if (submission) {
      submittedDrafts.current.set(draft.id, draft);
      void submission.then((reply) => {
        if (submittedDrafts.current.get(draft.id) !== draft) return;
        submittedDrafts.current.delete(draft.id);
        if (!reply.accepted || reply.execution?.state === "withdrawn" || reply.state === "unknown") setRejectedDrafts((drafts) => [...drafts, {
          ...draft, reason: reply.message || "Your message was not accepted.",
          ...(reply.execution?.state === "withdrawn" && reply.clientRequestId ? { recoveryOfClientId: reply.clientRequestId } : {}),
          ...(reply.state === "unknown" && reply.clientRequestId ? { uncertain: { id: reply.clientRequestId } } : {}),
        }]);
        else if (reply.requestId && draft.attachments.length) {
          const result = useSessionStore.getState().messages[draft.sessionId]?.find((message) => message.outcome?.request_id === reply.requestId);
          if (!result || result.outcome?.code === "attachment_unavailable") submittedDrafts.current.set(reply.requestId, draft);
        }
      }).catch(() => {
        if (submittedDrafts.current.get(draft.id) !== draft) return;
        submittedDrafts.current.delete(draft.id);
        setRejectedDrafts((drafts) => [...drafts, { ...draft, reason: "Delivery was not confirmed. Check the conversation before sending again." }]);
      });
    }
  };

  const clearCompactCommand = (draft: { sessionId: string; text: string; compactDraft?: { accountGeneration: number; revision: number; version: number } }) => {
    const state = useSessionStore.getState();
    if (mounted.current && draft.compactDraft && state.accountGeneration === draft.compactDraft.accountGeneration &&
        state.activeSessionId === draft.sessionId && composerRevision.current === draft.compactDraft.revision) {
      textDraft.clear(draft.compactDraft.version);
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
    setSubmissionError("");
    textDraft.consume(draft.compactDraft.version);
    try {
      let reply: { accepted: boolean; message?: string; state?: string };
      try {
        reply = await readSubmission(url, { method: "POST", headers, signal: observer.signal,
          body: JSON.stringify({ client_request_id: id, operation: { kind: "compact" } }) });
      } catch {
        if (observer.signal.aborted || !current()) throw new Error("Compaction admission is unconfirmed. Check the original request before sending again.");
        // A lost receipt never authorizes a second native operation.
        reply = await readSubmission(`${url}/${encodeURIComponent(id)}`, { method: "GET", headers, signal: observer.signal });
      }
      if (!current()) return;
      if (reply.accepted) clearCompactCommand(draft);
      else if (reply.state === "unknown") throw new Error(reply.message || "Compaction admission is unconfirmed.");
      else if (useSessionStore.getState().activeSessionId === draft.sessionId) setSubmissionError(reply.message || "Compaction was not accepted.");
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
      const reply = await readSubmission<ClientSubmissionStatus>(`${window.location.origin}/api/sessions/${encodeURIComponent(draft.sessionId)}/submissions/${encodeURIComponent(original.id)}`, {
        method: "GET", headers: { Authorization: `Bearer ${token}` },
      });
      if (draft.compactDraft && reply.accepted) {
        clearCompactCommand(draft);
        setRejectedDrafts((drafts) => drafts.filter((item) => item.id !== draft.id));
        return;
      }
      const unknown = reply.state === "unknown" || reply.execution.state === "unknown";
      const withdrawn = reply.execution.state === "withdrawn";
      const recoverable = withdrawn && !unknown && reply.execution.recoverable_input;
      setRejectedDrafts((drafts) => reply.accepted && !unknown && !withdrawn ? drafts.filter((item) => item.id !== draft.id) : drafts.map((item) => item.id === draft.id
        ? { ...item, confirming: false, reason: reply.message || (recoverable
          ? "This input was withdrawn before execution. You can restore it and send it again."
          : withdrawn ? "This input is read-only and cannot be restored. The saved draft and original request ID were kept."
          : "Admission is still unconfirmed. Check the conversation before sending again."),
          ...(recoverable ? { recoveryOfClientId: original.id } : {}),
          uncertain: recoverable ? undefined : original } : item));
    } catch (error) {
      setRejectedDrafts((drafts) => drafts.map((item) => item.id === draft.id ? { ...item, confirming: false,
        reason: `Admission is still unconfirmed. ${error instanceof Error ? error.message : "Try again."} The saved draft and original request ID were kept. Check the conversation before sending again.` } : item));
    }
  };

  const handleSend = async () => {
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
      setSubmissionError("");
      const result = await reset(activeSessionId);
      if (result) setSubmissionError(result.error ?? "");
      return;
    }

    // /archive command — intercept client-side. Hides the current
    // session and seamlessly swaps the active id to a fresh session
    // with the same name/working_dir/credential_id. The user sees an
    // empty chat under the same sidebar entry.
    if (trimmed.toLowerCase() === "/archive") {
      setInput("");
      const start = useSessionStore.getState();
      const observation = sessionList.capture(start);
      let foreground = start.activeSessionId === activeSessionId && start.mainView === "chat";
      const unsubscribe = useSessionStore.subscribe((state) => {
        if (state.activeSessionId !== activeSessionId || state.mainView !== "chat") foreground = false;
      });
      const stop = () => { foreground = false; unsubscribe(); };
      archiveObservers.current.add(stop);
      try {
        const token = useSessionStore.getState().token;
        const res = await fetch(
          `${window.location.origin}/api/sessions/${activeSessionId}/archive`,
          {
            method: "POST",
            headers: { Authorization: `Bearer ${token}` },
          }
        );
        if (!res.ok) return;
        const fresh = await res.json();
        // The `session_archived` WS broadcast also updates the
        // sessions list for any other clients — and may have already
        // landed in this tab before the HTTP response. Dedupe by id
        // on both removal AND insertion so the two paths converge to
        // the same list regardless of arrival order.
        const accepted = await sessionList.created(fresh, observation, activeSessionId);
        if (!accepted || !sessionList.current(observation)) return;
        // The archived session is going away — drop any deferred /fork for it.
        cancelFork(activeSessionId);
        if (foreground) await selectSession(fresh.id, fresh.agent_id);
      } catch {
        // A selected replacement exposes any snapshot acquisition failure.
      } finally {
        stop();
        archiveObservers.current.delete(stop);
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
      setHistory(HISTORY_IDLE);
      const wanted = trimmed.slice("/model".length).trim();
      if (!wanted) {
        setModelPickerOpen(true);
        return;
      }
      const store = useSessionStore.getState();
      const observation = sessionList.capture(store);
      const notice = (content: string, isError = false) =>
        store.addMessage(activeSessionId, {
          role: "system",
          type: "notice",
          content,
          is_error: isError,
        });
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
      const store = useSessionStore.getState();
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
        if (!res.ok) {
          let detail = `Couldn't resolve file reference (HTTP ${res.status}).`;
          try {
            const body = await res.json();
            if (typeof body?.detail === "string") detail = body.detail;
          } catch {
            // keep the generic message
          }
          useSessionStore.getState().addMessage(activeSessionId, {
            role: "system",
            type: "notice",
            content: detail,
            is_error: true,
          });
          return;
        }
        const data = await res.json();
        if (typeof data?.path === "string" && data.path) {
          useSessionStore.getState().openViewer(activeSessionId, data.path);
        } else {
          useSessionStore.getState().addMessage(activeSessionId, {
            role: "system",
            type: "notice",
            content:
              typeof data?.message === "string" && data.message
                ? data.message
                : "Couldn't resolve that file reference.",
            is_error: true,
          });
        }
      } catch {
        useSessionStore.getState().addMessage(activeSessionId, {
          role: "system",
          type: "notice",
          content: "Couldn't resolve file reference — network error.",
          is_error: true,
        });
      }
      return;
    }

    submitTurn(trimmed);
  };

  /** Put a recalled message in the box with the caret at its end, which is
   *  where a shell leaves it and where you want to keep typing. */
  const applyRecall = (text: string) => {
    setInput(text);
    requestAnimationFrame(() => {
      const ta = textareaRef.current;
      if (!ta) return;
      const end = ta.value.length;
      ta.setSelectionRange(end, end);
    });
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
        const recall =
          dir === "up"
            ? recallPrev(sentHistory, history, ta.value)
            : recallNext(sentHistory, history, ta.value);
        if (recall) {
          e.preventDefault();
          setHistory(recall.state);
          applyRecall(recall.text);
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
    if (!activeSessionId || !isRunning) return;
    const onEsc = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const tag = (e.target as HTMLElement | null)?.tagName;
      const inChatInput =
        tag === "TEXTAREA" &&
        (e.target as HTMLElement).closest(".chat-input-bar") !== null;
      const inOtherField =
        (tag === "INPUT" || tag === "TEXTAREA") && !inChatInput;
      if (inOtherField) return;
      e.preventDefault();
      interrupt(activeSessionId);
    };
    window.addEventListener("keydown", onEsc);
    return () => window.removeEventListener("keydown", onEsc);
  }, [activeSessionId, isRunning, interrupt]);

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
  const parentSession = useMemo(
    () =>
      activeSession?.parent_session_id
        ? sessions.find((s) => s.id === activeSession.parent_session_id) ??
          archivedSessions.find(
            (s) => s.id === activeSession.parent_session_id
          )
        : undefined,
    [activeSession?.parent_session_id, sessions, archivedSessions]
  );
  const parentAgent = useMemo(
    () => agents.find((a) => a.id === parentSession?.agent_id),
    [agents, parentSession?.agent_id]
  );
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
  const forkParentSession = useMemo(
    () =>
      activeSession?.forked_from_session_id
        ? sessions.find((s) => s.id === activeSession.forked_from_session_id) ??
          archivedSessions.find(
            (s) => s.id === activeSession.forked_from_session_id
          )
        : undefined,
    [activeSession?.forked_from_session_id, sessions, archivedSessions]
  );
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
          {submissionError && <p role="alert" className="mb-2 text-sm text-destructive">{submissionError}</p>}
          {activeSessionId && <QuestionDraftRecovery sessionId={activeSessionId}
            composerOccupied={input.length > 0 || pendingAttachments.length > 0 || isArchived}
            onContinue={(key, content, attachments) => {
              setInput(content); setRestoredQuestion({ key, sessionId: activeSessionId });
              restoreAttachments(attachments);
              setRestoredInput(null); setHistory(HISTORY_IDLE); textareaRef.current?.focus();
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
                disabled={Boolean(draft.uncertain) || !activeSessionId || isArchived || input.length > 0 || pendingAttachments.length > 0}
                onClick={() => {
                  if (draft.uncertain || !activeSessionId || isArchived || input.length > 0 || pendingAttachments.length > 0) return;
                  setInput(draft.text);
                  setRestoredInput(draft.recoveryOfClientId && draft.sessionId === activeSessionId
                    ? { sessionId: activeSessionId, clientId: draft.recoveryOfClientId } : null);
                  restoreAttachments(draft.attachments);
                  setRequiredAttachmentCount(0);
                  setHistory(HISTORY_IDLE);
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

      <Virtuoso
        ref={virtuoso}
        scrollerRef={setScroller}
        key={`${activeSessionId}:${hasMessages}`}
        className="chat-messages flex-1 min-h-0"
        data={chatMessages}
        itemContent={renderMessage}
        initialTopMostItemIndex={{ index: "LAST", align: "end" }}
        computeItemKey={(_index, message) => chatMessageKey(message)}
        atBottomStateChange={(bottom) => {
          if (bottom && hasMessages) setParked(true);
        }}
        onClickCapture={pauseForReading}
        atTopStateChange={(top) => {
          setAtTop(top);
          if (!top) setOlderStalled(false);
        }}
        onWheelCapture={(event) => {
          if (event.deltaY >= 0 || event.ctrlKey || !canScrollChatUp(scroller.current, event.target)) return;
          navigationPointer.current = null;
          pauseFollowing();
        }}
        onKeyDown={(event) => {
          if (event.defaultPrevented || event.altKey || event.metaKey || event.ctrlKey && event.key !== "Home") return;
          const up = ["ArrowUp", "PageUp", "Home"].includes(event.key) || event.key === " " && event.shiftKey;
          if (!up || !canScrollChatUp(scroller.current, event.target)) return;
          const target = event.target instanceof HTMLElement ? event.target : null;
          if (target?.isContentEditable || target?.closest("input, textarea, select, [role=textbox]")) return;
          if (event.key === " " && target?.closest("button, a[href], summary, [role=button]")) return;
          navigationPointer.current = null;
          pauseFollowing();
        }}
        onPointerDownCapture={(event) => {
          navigationPointer.current = null;
          const viewport = scroller.current;
          if (!viewport || !event.isPrimary) return;
          if (event.pointerType === "touch" || event.pointerType === "pen") {
            navigationPointer.current = { id: event.pointerId, y: event.clientY, top: viewport.scrollTop, resume: following.current };
          } else if (event.button === 0 && event.target === viewport && canScrollChatUp(viewport, viewport)) {
            const left = viewport.getBoundingClientRect().left + viewport.clientLeft;
            if (event.clientX < left || event.clientX >= left + viewport.clientWidth) {
              navigationPointer.current = { id: event.pointerId, y: event.clientY, top: viewport.scrollTop, resume: following.current };
              pauseFollowing();
            }
          }
        }}
        onPointerMoveCapture={(event) => {
          const pointer = navigationPointer.current;
          if (!pointer || pointer.id !== event.pointerId) return;
          if (event.pointerType === "mouse") {
            if (event.clientY !== pointer.y) pointer.resume = false;
            return;
          }
          const up = event.clientY > pointer.y;
          pointer.y = event.clientY;
          if (up && canScrollChatUp(scroller.current, event.target)) {
            pointer.resume = false;
            pauseFollowing();
          }
        }}
        onPointerUpCapture={(event) => {
          const pointer = navigationPointer.current;
          if (!pointer || pointer.id !== event.pointerId) return;
          navigationPointer.current = null;
          const viewport = scroller.current;
          // Native thumb drags may omit pointermove: release coordinates still
          // distinguish navigation from output growing during an unmoved press.
          const resume = pointer.resume && event.clientY === pointer.y;
          const returned = viewport && viewport.scrollTop > pointer.top &&
            viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <= 4;
          if (viewport?.clientHeight && (resume || returned)) {
            following.current = true;
            followMeasuredOutput();
          }
        }}
        onPointerCancelCapture={() => { navigationPointer.current = null; }}
        onScroll={(event) => {
          const viewport = event.currentTarget;
          if (viewport !== scroller.current || !viewport.clientHeight) return;
          const top = viewport.scrollTop;
          const bottom = viewport.scrollHeight - viewport.clientHeight - top <= 4;
          if (navigationPointer.current) navigationPointer.current.top = Math.min(navigationPointer.current.top, top);
          if (!pageAnchor.current && !navigationPointer.current) {
            // Collapse can clamp the viewport to its new bottom without any
            // return navigation. Resume only on an actual downward arrival.
            if (bottom && top > lastScrollTop.current) following.current = true;
          }
          lastScrollTop.current = top;
        }}
        totalListHeightChanged={followMeasuredOutput}
        followOutput={false}
        increaseViewportBy={{ top: 400, bottom: 400 }}
        components={{ Header: olderHeader, Footer: ChatFooter }}
        context={{ footer }}
      />

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

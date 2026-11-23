import { accountFetch as fetch } from "../lib/accountFetch";
import { schedules } from "../lib/scheduleList";
import { sessions as sessionList, type SessionObservation } from "../lib/sessionList";
import { ArchiveDeadline, postArchive } from "../lib/sessionArchive";
import { createResearch } from "../lib/research";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { useStore } from "zustand";
import { chatDraft, prefillChatDraft } from "../lib/chatDraft";
import {
  IconFile,
  IconMenu2,
  IconPaperclip,
  IconX,
} from "@tabler/icons-react";
import {
  hasEmptyTranscript,
  hasTranscriptBaseline,
  useSessionStore,
  type BackendKind,
  type Message,
  type SessionInfo,
} from "../stores/sessionStore";
import { ForkDialog } from "./ForkDialog";
import { ResearchCard } from "./ResearchCard";
import { RecoverableInputs } from "./RecoverableInputs";
import type { ClientSubmissionStatus, SessionArchiveResult } from "../api";
import { WikiJobCards } from "./WikiJobCards";
import { ChatTranscript, type TranscriptPreparation } from "./ChatTranscript";
import { ChatComposer } from "./ChatComposer";
import { DispatchQueue, type CancelCompact } from "./DispatchQueue";
import { ContextMeter } from "./ContextMeter";
import { CommandNotices } from "./CommandNotices";
import { SessionAuxiliaryNotice } from "./SessionAuxiliaryNotice";
import { ModelPickerDialog } from "./ModelPickerDialog";
import { isImeComposing } from "../lib/composerKeys";
import { useComposerHistory } from "../hooks/useComposerHistory";
import { OctopusLogo } from "./OctopusLogo";
import { ChatSessionHeading } from "./ChatSessionHeading";
import type { RuntimeModelDefault } from "../hooks/useModelSettings";
import type { AnswerPayload } from "./QuestionPrompt";
import { QuestionDraftRecovery } from "./QuestionDraftRecovery";
import { beginQuestionTransmission, finishQuestionTransmission } from "../lib/questionDrafts";
import { buildRememberPrompt } from "./SlashCommandMenu";
import { Button } from "./ui/button";
import { useSessionFork } from "../hooks/useSessionFork";
import { selectSession, showSession } from "../lib/selectSession";
import { loadSessionSnapshots, observeSession } from "../lib/hydrateSession";
import type { WikiMessageMode } from "../lib/wikiComposer";
import { classifySubmission, newSubmissionId, readSubmission, type QuestionCommandTarget, type SubmissionReply } from "../lib/submission";
import { MAX_ATTACHMENTS_PER_MESSAGE, useDraftAttachments, type PendingAttachment } from "../hooks/useDraftAttachments";

interface SuccessorPresentation {
  sessionId: string;
  account: number;
  key: string;
  preparation: TranscriptPreparation;
}

interface ArchiveIntent {
  sessionId: string;
  account: number;
}

interface ArchiveObservationFeedback {
  reading: ReturnType<typeof sessionList.observe>;
  confirmation?: string;
  successorError?: string;
}

interface ArchiveLayout extends ArchiveIntent {
  controlsHeight: number;
  viewportHeight: number;
}

function retainsRejectedDraft(message: Message): boolean {
  return message.outcome?.code === "attachment_unavailable" || Boolean(message.tool_input?.rejected_input)
    || message.tool_input?.rejected_input_preview === true;
}

function SessionContextMeter({ sessionId, archived }: { sessionId: string; archived?: boolean }) {
  const snapshot = useSessionStore((state) => state.contextStates[sessionId]);
  return <ContextMeter state={snapshot?.state} pending={snapshot?.pending} historical={archived} />;
}

function ArchivedSuccessor({ sessionId }: { sessionId: string }) {
  const successorId = useSessionStore((state) => state.sessionSuccessors[sessionId]);
  const successorKnown = useSessionStore((state) => {
    const id = state.sessionSuccessors[sessionId];
    return !!id && (state.sessions.some((row) => row.id === id) || state.archivedSessions.some((row) => row.id === id));
  });
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
    <Button size="sm" variant="outline" disabled={busy} onClick={() => void open()}>
      {successorKnown ? "Open successor" : "Check successor"}
    </Button>
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
    recoveryOfClientId?: string,
    expectedBackend?: BackendKind,
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
  const inputOccupied = useStore(textDraft.store, (draft) => draft.text.length > 0);
  const saveError = useStore(textDraft.store, (draft) => draft.saveError);
  const pendingFileRestore = useRef<{
    account: number; sessionId: string; requestId: string; version: number;
  } | null>(null);
  const setInput = useCallback((text: string) => {
    pendingFileRestore.current = null;
    textDraft.edit(text);
  }, [textDraft]);
  useEffect(() => () => textDraft.clearConsumed(), [textDraft]);
  const mounted = useRef(true);
  const archiveObserver = useRef<{ stop: () => void; revokeHandoff: () => void } | null>(null);
  const revokeArchiveHandoff = useCallback(() => archiveObserver.current?.revokeHandoff(), []);
  const [preparingSuccessor, setPreparingSuccessor] = useState<SuccessorPresentation | null>(null);
  // Ordinary selection keeps the current composer mounted. A handoff adopts
  // the prepared pane's key so it keeps the exact transcript already laid out.
  const [foregroundKey, setForegroundKey] = useState("chat");
  const successorSerial = useRef(0);
  const [archiveLayout, setArchiveLayout] = useState<ArchiveLayout | null>(null);
  const conversationControls = useRef<HTMLDivElement | null>(null);
  const focusAfterArchive = useRef<HTMLElement | null>(null);
  // Concurrent receipts can outlive their openings. Only their own source's
  // current view needs to allow for early dispatch updates.
  const archiveWrites = useRef(new Set<ArchiveIntent>());
  const archiveInFlight = useCallback(({ sessionId, account }: ArchiveIntent) =>
    Array.from(archiveWrites.current).some((write) => write.sessionId === sessionId && write.account === account), []);
  const preserveArchiveLayout = useCallback(({ sessionId, account }: ArchiveIntent) => {
    const controls = conversationControls.current;
    const pane = controls?.parentElement;
    if (!controls || pane?.dataset.chatSession !== sessionId) return;
    const controlsHeight = controls.getBoundingClientRect().height;
    if (controlsHeight <= 0) return;
    const viewportHeight = pane.querySelector(".chat-messages")?.getBoundingClientRect().height ?? 0;
    setArchiveLayout((layout) => {
      if (layout?.sessionId !== sessionId || layout.account !== account) return { sessionId, account, controlsHeight, viewportHeight };
      const keptHeight = Math.max(layout.controlsHeight, controlsHeight);
      if (keptHeight === layout.controlsHeight && viewportHeight === layout.viewportHeight) return layout;
      return { ...layout, controlsHeight: keptHeight, viewportHeight };
    });
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      pendingFileRestore.current = null;
      archiveObserver.current?.stop();
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
    intent: ArchiveIntent; version: number;
    observation: SessionObservation; message: string; activity: string | null;
    observed?: ArchiveObservationFeedback;
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
  const [modelDefaults, setModelDefaults] = useState<{ account: number; values: RuntimeModelDefault[] }>();
  const rememberModelDefault = (observed: RuntimeModelDefault) => {
    setModelDefaults((current) => {
      const values = current?.account === accountGeneration ? current.values : [];
      return { account: accountGeneration, values: [
        ...values.filter((item) => item.backend !== observed.backend || item.credential !== observed.credential),
        observed,
      ] };
    });
  };
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const composerHistory = useComposerHistory(textDraft, textareaRef);
  const resetHistory = composerHistory.reset;
  // Counter for nested dragenter/dragleave events — without this the
  // overlay flickers as the cursor crosses child elements (textarea,
  // chips, etc.) because each crossing fires a leave on the parent.
  const dragCounter = useRef(0);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const mainView = useSessionStore((s) => s.mainView);
  const composerRevision = useRef(0);
  const viewing = useRef<{ account: number; sessionId: string } | null>(null);
  useEffect(() => {
    const unsubscribe = useSessionStore.subscribe((state, before) => {
      const sameConversation = state.activeSessionId === before.activeSessionId && state.accountGeneration === before.accountGeneration;
      if (!sameConversation) {
        composerRevision.current += 1;
        setArchiveLayout(null);
      } else if (state.activeSessionId && state.archivedSessions !== before.archivedSessions) {
        const sessionId = state.activeSessionId;
        const wasArchived = before.archivedSessions.some((row) => row.id === sessionId);
        if (wasArchived && state.sessions.some((row) => row.id === sessionId)) {
          setArchiveLayout(null);
        } else if (!wasArchived && state.mainView === "chat" && state.archivedSessions.some((row) => row.id === sessionId)) {
          preserveArchiveLayout({ sessionId, account: state.accountGeneration });
          const active = document.activeElement;
          if (active instanceof HTMLElement && conversationControls.current?.contains(active)) {
            focusAfterArchive.current = active;
          }
        }
      }
      if (!sameConversation || state.mainView !== before.mainView || state.viewer !== before.viewer) {
        focusAfterArchive.current = null;
        viewing.current = null;
        submissionCommand.current = null;
        pendingFileRestore.current = null;
      }
    });
    return () => { viewing.current = null; unsubscribe(); };
  }, [preserveArchiveLayout]);
  const compactSending = useRef<AbortController | null>(null);
  useEffect(() => () => {
    compactSending.current?.abort();
    compactSending.current = null;
  }, [activeSessionId, accountGeneration]);
  useEffect(() => textDraft.store.subscribe((draft, before) => {
    if (draft.text !== before.text) composerRevision.current += 1;
  }), [textDraft]);
  const researchCreations = useRef(new Set<AbortController>());
  useEffect(() => {
    const operations = researchCreations.current;
    return () => { operations.forEach((operation) => operation.abort()); operations.clear(); };
  }, [activeSessionId]);
  const { attachments: pendingAttachments, addFiles: addAttachmentFiles, removeAttachment, retryAttachment,
    restoreAttachments, clearAttachments, rejectedFiles, dismissRejectedFiles, unready,
    readyIds: readyAttachmentIds } = useDraftAttachments(activeSessionId, revokeArchiveHandoff);
  const addFiles = useCallback((files: File[]) => {
    pendingFileRestore.current = null;
    addAttachmentFiles(files);
  }, [addAttachmentFiles]);
  const retainSubmittedInput = useCallback((requestId: string, draft: { sessionId: string; text: string; attachments: PendingAttachment[] }) => {
    if (!mounted.current || !draft.attachments.length) return;
    const state = useSessionStore.getState();
    const result = state.messages[draft.sessionId]?.find((message) => message.outcome?.request_id === requestId);
    if (!result || retainsRejectedDraft(result)) submittedDrafts.current.set(requestId, draft);
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
      if (result && !retainsRejectedDraft(result)) submittedDrafts.current.delete(requestId);
    }
  }), []);
  const activeSession = useSessionStore((s) => s.sessions.find(session => session.id === activeSessionId)
    ?? s.archivedSessions.find(session => session.id === activeSessionId));
  const staged = preparingSuccessor?.account === accountGeneration ? preparingSuccessor : null;
  const stagedSession = useSessionStore((state) => staged ? state.sessions.find((session) => session.id === staged.sessionId) : undefined);
  const isArchived = !!activeSession?.archived;
  const transcriptDamaged = activeSession?.transcript_integrity === "duplicate_seq";
  useLayoutEffect(() => {
    if (!activeSessionId || mainView !== "chat") return;
    if (!isArchived) {
      if (archiveLayout?.sessionId === activeSessionId && archiveLayout.account === accountGeneration) return;
      const owner = { sessionId: activeSessionId, account: accountGeneration };
      if (archiveInFlight(owner)) preserveArchiveLayout(owner);
      return;
    }
    const source = focusAfterArchive.current;
    focusAfterArchive.current = null;
    const pane = conversationControls.current?.parentElement;
    if (source && !source.isConnected && document.activeElement === document.body && pane?.dataset.chatSession === activeSessionId) {
      pane.querySelector<HTMLElement>(".chat-messages:not([inert]), [data-chat-initial-surface]")?.focus({ preventScroll: true });
    }
  }, [accountGeneration, activeSessionId, archiveInFlight, archiveLayout, isArchived, mainView, preserveArchiveLayout]);
  const restoreSavedInput = useCallback((saved: ClientSubmissionStatus, files: File[]) => {
    if (!activeSessionId || saved.session_id !== activeSessionId || textDraft.store.getState().text || pendingAttachments.length || isArchived || transcriptDamaged || !saved.input || saved.input.kind !== "message") return;
    setInput(saved.input.content);
    addFiles(files);
    setRequiredAttachmentCount(saved.input.attachment_ids.length);
    resetHistory();
    setRestoredInput({ sessionId: activeSessionId, clientId: saved.client_request_id });
    textareaRef.current?.focus();
  }, [activeSessionId, pendingAttachments, isArchived, transcriptDamaged, addFiles, setInput, resetHistory, textDraft]);
  const dispatchState = useSessionStore((s) => activeSessionId ? s.dispatchStates[activeSessionId] : undefined);
  const { queueFork, cancelFork, reset } = useSessionFork();

  // Prefilled chat input on fork open (session-rewind.md §6.1). When the
  // active session carries a non-null fork_prefilled_prompt (the rewound user
  // message text, set in fork_metadata until the first turn), populate the
  // input once, after account restoration. Saved text and newer edits take
  // precedence; the backend clears fork_metadata after the first turn.
  useEffect(() => {
    prefillChatDraft(textDraft, activeSessionId, activeSession?.fork_prefilled_prompt);
  }, [activeSessionId, activeSession?.fork_prefilled_prompt, draftOwner, textDraft]);

  // Whether a message typed right now goes INTO the running turn or queues
  // behind it. A harness capability the server reports per session
  // (inline-steering.md §12) — the button must not promise one and do the
  // other. Attachments always queue, so the label follows that too.
  const canSteer =
    !!activeSession?.can_steer && pendingAttachments.length === 0 &&
    (dispatchState?.active_operation !== "compact" &&
      !dispatchState?.items.some((item) => item.operation === "compact"));

  const restoreRejectedInput = useCallback((message: Message) => {
    if (!activeSessionId || textDraft.store.getState().text || pendingAttachments.length > 0) return;
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
  }, [activeSessionId, pendingAttachments.length, restoreAttachments, setInput, resetHistory, textDraft]);

  const openMessageFork = useCallback((seq: number) => {
    if (!activeSessionId) return;
    setForkDialog({ sessionId: activeSessionId, instance: ++forkDialogSequence.current,
      parentName: activeSession?.name || "Session", account: accountGeneration, seq });
  }, [activeSessionId, activeSession?.name, accountGeneration]);

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

  const submitTurn = (content: string, input: string) => {
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
        readyAttachmentIds.length > 0 ? readyAttachmentIds : undefined, undefined, draftId,
        original.transmission?.recoveryOfClientId, activeSession?.backend));
      setInput(""); setRestoredQuestion(null); setRestoredInput(null); resetHistory();
      clearAttachments(); setRequiredAttachmentCount(0); setSubmissionError("");
      void Promise.resolve(submission).then((reply) => {
        if (reply?.code === "engine_changed" && useSessionStore.getState().accountGeneration === accountGeneration) {
          void loadSessionSnapshots(activeSessionId, { fresh: true }).catch(() => {});
        }
        return finishQuestionTransmission(key, draftId, reply, retainSubmittedInput);
      })
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
    const submission = sendMessage(
      activeSessionId, content,
      readyAttachmentIds.length > 0 ? readyAttachmentIds : undefined,
      undefined, draft.id, draft.recoveryOfClientId, activeSession?.backend,
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
        if (reply.code === "engine_changed" && useSessionStore.getState().accountGeneration === accountGeneration) {
          void loadSessionSnapshots(draft.sessionId, { fresh: true }).catch(() => {});
        }
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

  const submitCompact = async (input: string) => {
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
          body: JSON.stringify({ client_request_id: id, expected_backend: activeSession?.backend,
            operation: { kind: "compact" } }) }, original);
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
        if (reply.code === "engine_changed") void loadSessionSnapshots(draft.sessionId, { fresh: true }).catch(() => {});
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
    const foreground = useSessionStore.getState();
    if (!activeSessionId || foreground.activeSessionId !== activeSessionId || foreground.mainView !== "chat" ||
      foreground.archivedSessions.some((session) => session.id === activeSessionId)) return;
    const input = textDraft.store.getState().text;
    const trimmed = input.trim();
    if (/^\/compact(?:\s|$)/.test(trimmed)) {
      if (trimmed !== "/compact") { setSubmissionError("Usage: /compact (no arguments)."); return; }
      await submitCompact(input);
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

    // The source remains readable until the confirmed direct successor is ready.
    if (trimmed.toLowerCase() === "/archive") {
      setInput("");
      const start = useSessionStore.getState();
      const intent = { account: start.accountGeneration, sessionId: activeSessionId };
      let handoffAllowed = !pendingAttachments.length && !rejectedFiles.length && !requiredAttachmentCount;
      // Dispatch can clear its controls before archive membership arrives.
      archiveWrites.current.add(intent);
      preserveArchiveLayout(intent);
      viewing.current = intent;
      const version = textDraft.store.getState().version;
      setArchiveFeedback(null);
      archiveObserver.current?.stop();
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
      let retirePreparation: (() => void) | undefined;
      let targetStillCurrent: (() => boolean) | undefined;
      let observedFeedback: ArchiveObservationFeedback | undefined;
      let unsubscribe = () => {};
      const updateObservedFeedback = (next?: ArchiveObservationFeedback) => {
        const previousRead = observedFeedback?.reading;
        if (previousRead && previousRead !== tracked && previousRead !== next?.reading) previousRead.finish();
        observedFeedback = next;
      };
      const revokeHandoff = () => {
        handoffAllowed = false;
        retirePreparation?.();
      };
      const stop = () => {
        retired = true; foreground = false;
        revokeHandoff();
        updateObservedFeedback();
        const accountEnded = !sessionList.accountCurrent(observation);
        if (accountEnded) { post.finish(); confirmation?.finish(); }
        // A bounded command still observes restoration and deletion while
        // producing a list fact after its initiating view has retired.
        if (accountEnded || !posting && !checking) {
          unsubscribe();
          tracked.finish();
        }
        if (archiveObserver.current === ownership) archiveObserver.current = null;
      };
      const ownership = { stop, revokeHandoff };
      const current = () => mounted.current && foreground && viewing.current === intent &&
        sessionList.accountCurrent(observation) && textDraft.current() && textDraft.store.getState().version === version;
      const keepObserving = () => current() &&
        (retain || observedFeedback !== undefined || !hasPostResult && sessionList.archiveActivityCurrent(activeSessionId, observation));
      const failureDetail = (result: SessionArchiveResult) =>
        `${result.failure_stage === "successor_creation" ? "Successor creation" :
          "Schedule rebinding"} failed: ${result.message}`;
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
      const feedback = (message: string, readAgain = false, observed?: ArchiveObservationFeedback) => {
        retain = readAgain;
        sharedActivity = getActivity();
        if (!current()) return;
        updateObservedFeedback(observed);
        setArchiveFeedback({ intent, version, observation, message, activity: activityMessage(sharedActivity), observed,
          confirm: readAgain ? () => confirm(true) : undefined, checking });
      };
      const finishChecking = () => {
        checking = false;
        if (current()) setArchiveFeedback((value) => value?.intent === intent ? { ...value, checking: false } : value);
      };
      const rememberPartial = (result: SessionArchiveResult) => {
        archived = true; knownSuccessor = result.new_session_id;
        partialMessage = `Archive committed. ${failureDetail(result)}`;
        feedback(partialMessage);
      };
      const ownsArchiveEffect = () => sessionList.archiveObservationCurrent(activeSessionId, observation);
      const confirmedArchiveCurrent = (reading: typeof tracked) => sessionList.archiveActivityCurrent(activeSessionId, reading.observation) &&
        useSessionStore.getState().archivedSessions.some((row) => row.id === activeSessionId);
      const syncObservedFeedback = () => {
        const shown = observedFeedback;
        if (!shown) return;
        if (!confirmedArchiveCurrent(shown.reading)) updateObservedFeedback();
        else if (shown.successorError && !ownsArchiveEffect()) {
          updateObservedFeedback(shown.confirmation ? { ...shown, successorError: undefined } : undefined);
        }
        const observed = observedFeedback;
        if (shown !== observed && current()) setArchiveFeedback((value) => value?.intent === intent ? { ...value, observed } : value);
      };
      const prepareSuccessor = (id: string) => new Promise<string | undefined>((done) => {
        const target = () => {
          if (!handoffAllowed || !current() || !ownsArchiveEffect()) return undefined;
          const state = useSessionStore.getState();
          if (state.sessionSuccessors[activeSessionId] !== id || !state.archivedSessions.some((row) => row.id === activeSessionId)) return undefined;
          return state.sessions.find((row) => row.id === id && !row.archived);
        };
        if (!target()) { done(undefined); return; }
        const key = `successor:${++successorSerial.current}`;
        const reading = observeSession(id, "complete", { fresh: true });
        const finish = (promoted: boolean, error?: string) => {
          if (retirePreparation !== retire) return;
          retirePreparation = undefined;
          targetStillCurrent = undefined;
          if (!promoted) {
            reading.cancel();
            if (mounted.current) setPreparingSuccessor((pane) => pane?.key === key ? null : pane);
          }
          done(error);
        };
        const retire = () => finish(false);
        retirePreparation = retire;
        targetStillCurrent = () => Boolean(target());
        const fail = (message: string) => finish(false, message);
        const promote = (prepared: boolean) => {
          const successor = target();
          if (!successor || !hasTranscriptBaseline(useSessionStore.getState(), id) || successor.transcript_integrity === "duplicate_seq") {
            finish(false);
            return;
          }
          preparation.committing = true;
          finish(true);
          flushSync(() => {
            if (prepared) {
              setForegroundKey(key);
              setPreparingSuccessor(null);
            }
            showSession(id, successor.agent_id);
          });
        };
        const preparation: TranscriptPreparation = {
          current: () => retirePreparation === retire && Boolean(target()),
          committing: false,
          commit: () => promote(true),
          fail,
        };
        void reading.done.then((detail) => {
          const successor = target();
          if (retirePreparation !== retire || !successor) { finish(false); return; }
          const state = useSessionStore.getState();
          if (!detail || !hasTranscriptBaseline(state, id) || successor.transcript_integrity === "duplicate_seq") {
            fail("The successor's conversation could not be loaded. You can open it from the archived conversation.");
            return;
          }
          if (hasEmptyTranscript(state, successor)) {
            promote(false);
          } else setPreparingSuccessor({ sessionId: id, account: intent.account, key, preparation });
        }, (error: unknown) => fail(error instanceof Error ? error.message : "The successor's conversation could not be loaded."));
      });
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
        if (manual) setArchiveFeedback((value) => value?.intent === intent ? { ...value, checking: true } : value);
        else feedback(partialMessage ?? "");
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
          const successorError = result.successor && !manual ? await prepareSuccessor(result.successor.id) : undefined;
          // A confirmed archive can outlive the connection used to prepare its successor.
          if (!confirmedArchiveCurrent(reading)) return;
          feedback(hasPostResult ? "Archive committed." : "Original command result is unknown.", false, {
            reading,
            confirmation: hasPostResult ? "Viewing the archived conversation." : "Archive confirmed. Viewing the archived conversation.",
            successorError: successorError && ownsArchiveEffect() ? successorError : undefined,
          });
        } finally {
          // A displayed confirmation keeps its observation until the feedback retires.
          if (reading !== tracked && reading !== observedFeedback?.reading) reading.finish();
          confirmation.finish();
          finishChecking();
          if (!keepObserving()) stop();
        }
      };
      const unsubscribeList = useSessionStore.subscribe((state) => {
        if (!sessionList.accountCurrent(observation)) { stop(); return; }
        if (state.activeSessionId !== activeSessionId || state.mainView !== "chat") foreground = false;
        if (targetStillCurrent && !targetStillCurrent()) retirePreparation?.();
        syncActivity();
        syncObservedFeedback();
        if (!posting && !checking && !keepObserving()) stop();
      });
      const unsubscribeDraft = textDraft.store.subscribe((state) => { if (state.version !== version) stop(); });
      unsubscribe = () => { unsubscribeList(); unsubscribeDraft(); };
      archiveObserver.current = ownership;
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
          if (ownsArchiveEffect() && sessionList.archive(activeSessionId, observation, reply.result.new_session_id)) cancelOriginalFork();
          if (canCheckSuccessor()) await confirm(false);
          return;
        }
        knownSuccessor = fresh?.id ?? null;
        if (fresh === null) {
          if (ownsArchiveEffect() && sessionList.archive(activeSessionId, observation, null)) cancelOriginalFork();
          return;
        }
        // The `session_archived` WS broadcast also updates the
        // sessions list for any other clients — and may have already
        // landed in this tab before the HTTP response. Dedupe by id
        // on both removal AND insertion so the two paths converge to
        // the same list regardless of arrival order.
        confirmation = new ArchiveDeadline();
        checking = true;
        feedback("");
        const accepted = await sessionList.created(fresh, observation, { archivedId: activeSessionId, deadline: confirmation });
        confirmation.finish();
        if (!accepted || !sessionList.current(observation)) {
          feedback("Archive committed. Confirm the original archive to check its successor.", true);
          return;
        }
        cancelOriginalFork();
        const successorError = await prepareSuccessor(fresh.id);
        const observed = successorError && ownsArchiveEffect() && confirmedArchiveCurrent(tracked)
          ? { reading: tracked, successorError } : undefined;
        feedback("Archive committed.", false, observed);
      } finally {
        if (checking) finishChecking();
        posting = false;
        archiveWrites.current.delete(intent);
        if (mounted.current && !archiveInFlight(intent) &&
            !useSessionStore.getState().archivedSessions.some((row) => row.id === activeSessionId)) {
          setArchiveLayout((layout) => layout?.sessionId === intent.sessionId && layout.account === intent.account ? null : layout);
        }
        if (!keepObserving()) stop();
      }
      return;
    }

    if (/^\/model(?:\s|$)/i.test(trimmed)) {
      if (!activeSession) return;
      setInput("");
      resetHistory();
      setModelPickerOpen(true);
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
      submitTurn(buildRememberPrompt(note), input);
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

    submitTurn(trimmed, input);
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

  // The dialog confirmed the fork in SessionList; switch to it and load its
  // copied history. The composer adopts fork_prefilled_prompt (§6.1).
  const handleForked = async (fork: SessionInfo, observation: SessionObservation) => {
    if (!sessionList.current(observation)) return;
    await selectSession(fork.id, fork.agent_id, { fresh: true });
  };

  const renderSavedDrafts = (sessionId: string | null, archived: boolean) => <div className="shrink-0 max-h-72 overflow-auto px-6">
          {saveError && <p role="alert" className="mb-2 text-sm text-destructive">{saveError}</p>}
          {submissionError && submissionError.command === submissionCommand.current &&
            submissionError.command.account === accountGeneration && submissionError.command.sessionId === sessionId &&
            <p role="alert" className="mb-2 text-sm text-destructive">{submissionError.message}</p>}
          {archiveFeedback && viewing.current === archiveFeedback.intent &&
            archiveFeedback.intent.account === accountGeneration && archiveFeedback.intent.sessionId === sessionId &&
            textDraft.store.getState().version === archiveFeedback.version && sessionList.accountCurrent(archiveFeedback.observation) &&
            <>
              {archiveFeedback.checking && !archiveFeedback.confirm &&
                <p role="status" className="mb-2 text-sm text-muted-foreground">Checking archive…</p>}
              {(archiveFeedback.message || archiveFeedback.activity || archiveFeedback.observed) && <div role="alert" className="mb-2 text-sm text-destructive">
                {archiveFeedback.message && <p>{archiveFeedback.message}</p>}
                {archiveFeedback.observed?.confirmation && <p>{archiveFeedback.observed.confirmation}</p>}
                {archiveFeedback.activity && <p>{archiveFeedback.activity}</p>}
                {archiveFeedback.observed?.successorError && <p>{archiveFeedback.observed.successorError}</p>}
                {archiveFeedback.confirm && <Button size="sm" variant="outline" disabled={archiveFeedback.checking}
                  onClick={() => void archiveFeedback.confirm?.()}>Confirm original archive</Button>}
              </div>}
            </>}
          {sessionId && <QuestionDraftRecovery sessionId={sessionId}
            composerOccupied={inputOccupied || pendingAttachments.length > 0 || archived}
            onAcceptedInput={retainSubmittedInput}
            onContinue={(key, content, attachments) => {
              setInput(content); setRestoredQuestion({ key, sessionId: sessionId });
              restoreAttachments(attachments);
              setRestoredInput(null); resetHistory(); textareaRef.current?.focus();
            }} />}
          {rejectedDrafts.map((draft) => (
            <div key={draft.id} role="alert" className="mb-2 rounded-md border border-destructive/40 p-3 text-sm">
              <p>{draft.reason}</p>
              {(!sessionId || archived) && <p>Open an active conversation to restore this draft.</p>}
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
                disabled={draft.readOnly || Boolean(draft.uncertain) || !sessionId || archived || inputOccupied || pendingAttachments.length > 0}
                onClick={() => {
                  if (draft.readOnly || draft.uncertain || !sessionId || archived || textDraft.store.getState().text || pendingAttachments.length > 0) return;
                  setInput(draft.text);
                  setRestoredInput(draft.recoveryOfClientId && draft.sessionId === sessionId
                    ? { sessionId: sessionId, clientId: draft.recoveryOfClientId } : null);
                  restoreAttachments(draft.attachments);
                  setRequiredAttachmentCount(0);
                  resetHistory();
                  setRejectedDrafts((drafts) => drafts.filter((item) => item.id !== draft.id));
                  textareaRef.current?.focus();
                }}>Restore draft</Button>
              <Button size="sm" variant="ghost" className="ml-2 mt-2"
                onClick={() => setRejectedDrafts((drafts) => drafts.filter((item) => item.id !== draft.id))}>Discard saved draft</Button>
              {(inputOccupied || pendingAttachments.length > 0) && <p className="mt-1 text-xs text-muted-foreground">Clear the current draft to restore this one.</p>}
            </div>
          ))}
  </div>;

  if (!activeSessionId) {
    return (
      <div className="chat-view flex-1 flex flex-col min-h-0">
        <ChatSessionHeading connected={connected} onToggleSidebar={onToggleSidebar} />
        {renderSavedDrafts(null, false)}
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

  const renderPane = (sessionId: string, session: SessionInfo | undefined, key: string, preparation?: TranscriptPreparation) => {
    const archived = !!session?.archived;
    const damaged = session?.transcript_integrity === "duplicate_seq";
    const editing = !preparation && !archived && !damaged;
    const occupied = !preparation && (inputOccupied || pendingAttachments.length > 0);
    const keptLayout = !preparation && archiveLayout?.account === accountGeneration && archiveLayout.sessionId === sessionId
      ? archiveLayout : null;
    // Let the footer yield on resize without enlarging an already short reading viewport on archive.
    const readingFloor = keptLayout?.viewportHeight ? `min(3rem, ${keptLayout.viewportHeight}px)` : "3rem";
    return (
      <div key={`${accountGeneration}:${key}`} data-chat-session={sessionId} inert={preparation ? true : undefined} aria-hidden={preparation ? true : undefined}
        className={`flex flex-col min-h-0 ${preparation ? "absolute inset-0 invisible opacity-0 pointer-events-none" : "relative flex-1"}`}>
        <ChatSessionHeading session={session} connected={connected} onToggleSidebar={onToggleSidebar}
          runtimeModelDefaults={modelDefaults?.account === accountGeneration ? modelDefaults.values : undefined}
          onOpenModel={preparation ? undefined : () => setModelPickerOpen(true)} />
        <ChatTranscript key={`transcript:${sessionId}`} sessionId={sessionId} session={session} preparation={preparation}
          minimumViewportHeight={archived ? readingFloor : undefined}
          answerQuestion={answerQuestion} snoozeQuestion={snoozeQuestion} onFork={openMessageFork}
          onRestoreInput={archived || damaged ? undefined : restoreRejectedInput} restoreInputDisabled={occupied} />

        <div ref={preparation ? undefined : conversationControls}
          className={archived ? "flex flex-col overflow-y-auto" : "flex flex-col"}
          style={keptLayout ? { flexBasis: keptLayout.controlsHeight } : undefined}>
          <div className="research-card-wrap shrink-0 px-4 empty:hidden">
            <WikiJobCards key={sessionId} sessionId={sessionId} />
            <ResearchCard sessionId={sessionId} foreground={mainView === "chat" && !preparation} />
          </div>
          <DispatchQueue key={`dispatch:${sessionId}`} sessionId={sessionId} archived={archived} readOnly={archived || damaged} onCancelCompact={cancelCompact} />
          <CommandNotices sessionId={sessionId} />
          <SessionAuxiliaryNotice sessionId={sessionId} />
          <RecoverableInputs key={`recovery:${sessionId}`} sessionId={sessionId} connected={connected}
            disabled={archived || damaged || occupied} onRestore={restoreSavedInput} />
          {renderSavedDrafts(sessionId, archived)}
          {archived || damaged ? (
            <div className="chat-archived-banner px-4 py-3 border-t border-border bg-muted/40 text-sm text-muted-foreground flex items-center justify-between gap-3 grow shrink-0">
              {damaged ? <span>History review is required before continuing this conversation.</span> : <span>
                This session is <strong className="text-foreground">archived</strong>{" "}
                — viewing read-only history. Unarchive from the sidebar to continue the conversation.
              </span>}
              {archived && <ArchivedSuccessor key={`successor:${sessionId}`} sessionId={sessionId} />}
              <SessionContextMeter key={`context:${sessionId}`} sessionId={sessionId} archived={archived} />
            </div>
          ) : (
            <ChatComposer sessionId={sessionId} editor={editing ? { draft: textDraft, textarea: textareaRef,
              history: composerHistory, edit: setInput, send: handleSend, stop: () => void interrupt(sessionId),
              attach: () => fileInputRef.current?.click(), paste: handlePaste,
              canAttach: pendingAttachments.length < MAX_ATTACHMENTS_PER_MESSAGE,
              uploadPending: hasUploadInFlight, hasAttachments: readyAttachmentIds.length > 0 } : undefined}
              contextMeter={<SessionContextMeter key={sessionId} sessionId={sessionId} />}
              running={session?.status === "running"} canSteer={preparation ? !!session?.can_steer : canSteer}>
            {editing && <>
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

            </>}
            </ChatComposer>
          )}
        </div>
      </div>
    );
  };

  return (
    <div
      className="chat-view flex-1 flex flex-col min-h-0 relative"
      onDragEnter={isArchived || transcriptDamaged ? undefined : handleDragEnter}
      onDragLeave={isArchived || transcriptDamaged ? undefined : handleDragLeave}
      onDragOver={isArchived || transcriptDamaged ? undefined : handleDragOver}
      onDrop={isArchived || transcriptDamaged ? undefined : handleDrop}
    >
      {activeSession && (
        <ModelPickerDialog
          session={activeSession}
          open={modelPickerOpen}
          onOpenChange={setModelPickerOpen}
          onModelDefault={rememberModelDefault}
          onApplied={(updated) =>
            activeSessionId &&
            useSessionStore.getState().addMessage(activeSessionId, {
              role: "system",
              type: "notice",
              content: updated.backend === activeSession.backend ? "Conversation settings apply from the next turn."
                : "Conversation engine updated.",
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

      {[
        renderPane(activeSessionId, activeSession, foregroundKey),
        staged && stagedSession ? renderPane(staged.sessionId, stagedSession, staged.key, staged.preparation) : null,
      ]}
    </div>
  );
}

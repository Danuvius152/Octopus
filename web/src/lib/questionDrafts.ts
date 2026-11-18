import { useSessionStore, type PendingQuestion } from "../stores/sessionStore";
import { accountFetch } from "./accountFetch";
import { classifySubmission, readSubmission, type SubmissionReply } from "./submission";
import type { PendingAttachment } from "../hooks/useDraftAttachments";

export interface AnswerPayload {
  selected?: string[];
  text?: string;
}

export interface QuestionDraft {
  sessionId: string;
  accountGeneration: number;
  question: PendingQuestion;
  answers: AnswerPayload[];
  transmission?: {
    kind: "answer" | "continuation";
    id: string;
    answers: AnswerPayload[];
    state: "pending" | "unknown" | "rejected" | "readonly";
    content?: string;
    attachments?: PendingAttachment[];
    message?: string;
    receipt?: SubmissionReply;
    recoveryOfClientId?: string;
  };
}

export type RetainContinuationInput = (requestId: string, input: {
  sessionId: string; text: string; attachments: PendingAttachment[];
}) => void;

export function questionDraftKey(account: number, sessionId: string, question: PendingQuestion): string {
  return JSON.stringify([account, sessionId, question.request_id ?? null, question.attempt_id ?? null, question.question_id]);
}

export function saveQuestionDraft(draft: QuestionDraft): void {
  useSessionStore.setState((state) => {
    if (state.accountGeneration !== draft.accountGeneration) return state;
    if (!state.pendingQuestions[draft.sessionId]?.some((question) =>
      questionDraftKey(state.accountGeneration, draft.sessionId, question) === questionDraftKey(draft.accountGeneration, draft.sessionId, draft.question))) return state;
    const key = questionDraftKey(draft.accountGeneration, draft.sessionId, draft.question);
    return { questionDrafts: { ...state.questionDrafts, [key]: draft } };
  });
}

export function beginQuestionTransmission(key: string, transmission: NonNullable<QuestionDraft["transmission"]>): boolean {
  let started = false;
  useSessionStore.setState((state) => {
    const draft = state.questionDrafts[key];
    if (!draft || draft.transmission && draft.transmission.state !== "rejected") return state;
    started = true;
    return { questionDrafts: { ...state.questionDrafts, [key]: { ...draft, transmission } } };
  });
  return started;
}

export function finishQuestionTransmission(key: string, id: string, reply?: SubmissionReply | void, retainInput?: RetainContinuationInput): void {
  useSessionStore.setState((state) => {
    const draft = state.questionDrafts[key];
    if (!draft || draft.transmission?.id !== id) return state;
    const drafts = { ...state.questionDrafts };
    const outcome = draft.transmission.kind === "continuation" ? classifySubmission(reply, "continuation")
      : reply?.accepted && reply.state !== "unknown" ? "settled" : !reply || reply.state === "unknown" ? "unknown" : "rejected";
    if (outcome === "settled") {
      // Hand Files to the message recovery consumer before the question's
      // transmission snapshot retires, for direct and confirmed receipts.
      if (draft.transmission.kind === "continuation" && reply?.requestId && draft.transmission.attachments?.length) {
        retainInput?.(reply.requestId, { sessionId: draft.sessionId,
          text: draft.transmission.content ?? questionDraftText(draft), attachments: draft.transmission.attachments });
      }
      // A receipt confirms only the exact submitted snapshot, not later edits.
      if (draft.answers === draft.transmission.answers) delete drafts[key];
      else drafts[key] = { ...draft, transmission: undefined };
    } else drafts[key] = { ...draft, transmission: { ...draft.transmission,
      state: outcome === "unknown" ? "unknown" : outcome === "readonly" ? "readonly" : "rejected",
      message: reply?.message || (outcome === "readonly" ? "This continuation was withdrawn and cannot be restored." : undefined),
      receipt: reply || undefined, recoveryOfClientId: outcome === "recoverable" ? id : undefined } };
    return { questionDrafts: drafts };
  });
}

export function observeQuestionResolution(key: string, raw: unknown): boolean {
  const state = useSessionStore.getState(), draft = state.questionDrafts[key];
  if (!draft || !raw || typeof raw !== "object") return false;
  const resolution = raw as Record<string, unknown>, question = draft.question;
  if (resolution.question_id !== question.question_id || resolution.request_id !== (question.request_id ?? null)
    || resolution.attempt_id !== (question.attempt_id ?? null)
    || !["answered", "expired", "closed"].includes(String(resolution.disposition))) return false;
  const transmission = draft.transmission;
  if (transmission?.kind === "answer" && transmission.state !== "rejected") {
    if (resolution.disposition === "answered" && resolution.client_command_id === transmission.id) {
      finishQuestionTransmission(key, transmission.id, { accepted: true });
    } else if (resolution.disposition !== "answered" || typeof resolution.client_command_id === "string") {
      finishQuestionTransmission(key, transmission.id, { accepted: false,
        message: "The original question closed without accepting this submitted answer." });
    }
    // A legacy answer without command correlation cannot prove who sent it.
  }
  useSessionStore.getState().removePendingQuestion(draft.sessionId, question.question_id);
  return true;
}

export async function confirmQuestionReceipt(key: string, retainInput?: RetainContinuationInput): Promise<void> {
  const state = useSessionStore.getState(), draft = state.questionDrafts[key];
  if (!draft) return;
  if (draft.transmission?.kind === "continuation") {
    const original = draft.transmission;
    const result = await readSubmission(`/api/sessions/${encodeURIComponent(draft.sessionId)}/submissions/${encodeURIComponent(original.id)}`,
      { method: "GET", headers: { Authorization: `Bearer ${state.token}` }, signal: AbortSignal.timeout(10_000) },
      { sessionId: draft.sessionId, clientId: original.id, operation: "message" });
    finishQuestionTransmission(key, original.id, result, retainInput);
    return;
  }
  const path = `/api/sessions/${encodeURIComponent(draft.sessionId)}/questions/${encodeURIComponent(draft.question.question_id)}/result`;
  const response = await accountFetch(path, { headers: { Authorization: `Bearer ${state.token}` }, signal: AbortSignal.timeout(10_000) });
  if (!response.ok) throw new Error(`Original receipt is still unknown (${response.status}).`);
  const result = await response.json();
  if (result.state !== "resolved" || !observeQuestionResolution(key, result.resolution)) {
    throw new Error("Original answer acceptance is still unconfirmed. No input was resent.");
  }
}

export function discardQuestionDraft(key: string): void {
  useSessionStore.setState((state) => {
    const drafts = { ...state.questionDrafts };
    delete drafts[key];
    return { questionDrafts: drafts };
  });
}

export function questionDraftText(draft: QuestionDraft): string {
  return "Continuing a closed question (new message):\n\n" + draft.question.questions.map((question, index) => {
    const answer = draft.answers[index];
    return `Q: ${question.question}\nA: ${answer?.text || answer?.selected?.join(", ") || "(unanswered)"}`;
  }).join("\n\n");
}

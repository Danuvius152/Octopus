import { useSessionStore, type PendingQuestion } from "../stores/sessionStore";
import { accountFetch } from "./accountFetch";
import type { SubmissionReply } from "./submission";
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
    state: "pending" | "unknown" | "rejected";
    content?: string;
    attachments?: PendingAttachment[];
    message?: string;
  };
}

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

export function finishQuestionTransmission(key: string, id: string, reply?: SubmissionReply | void): void {
  useSessionStore.setState((state) => {
    const draft = state.questionDrafts[key];
    if (!draft || draft.transmission?.id !== id) return state;
    const drafts = { ...state.questionDrafts };
    if (reply?.accepted && reply.state !== "unknown") {
      // A receipt confirms only the exact submitted snapshot, not later edits.
      if (draft.answers === draft.transmission.answers) delete drafts[key];
      else drafts[key] = { ...draft, transmission: undefined };
    } else drafts[key] = { ...draft, transmission: { ...draft.transmission,
      state: !reply || reply.state === "unknown" ? "unknown" : "rejected", message: reply?.message } };
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

export async function confirmQuestionReceipt(key: string): Promise<void> {
  const state = useSessionStore.getState(), draft = state.questionDrafts[key];
  if (!draft) return;
  if (draft.transmission?.kind === "continuation") {
    const original = draft.transmission;
    const response = await accountFetch(`/api/sessions/${encodeURIComponent(draft.sessionId)}/submissions/${encodeURIComponent(original.id)}`,
      { headers: { Authorization: `Bearer ${state.token}` }, signal: AbortSignal.timeout(10_000) });
    if (!response.ok) throw new Error(`Original message receipt is still unknown (${response.status}).`);
    const result = await response.json();
    if (result.session_id !== draft.sessionId || result.client_request_id !== original.id || result.accepted !== true
      || result.state === "unknown" || result.execution?.state === "unknown") {
      throw new Error("Original message admission is still unconfirmed. No input was resent.");
    }
    finishQuestionTransmission(key, original.id, { accepted: true });
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

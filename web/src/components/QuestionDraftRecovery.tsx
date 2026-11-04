import { useEffect, useState } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { confirmQuestionReceipt, discardQuestionDraft, observeQuestionResolution, questionDraftText } from "../lib/questionDrafts";
import { Button } from "./ui/button";
import type { PendingAttachment } from "../hooks/useDraftAttachments";

interface Props {
  sessionId: string;
  composerOccupied: boolean;
  onContinue: (key: string, content: string, attachments: PendingAttachment[]) => void;
}

export function QuestionDraftRecovery({ sessionId, composerOccupied, onContinue }: Props) {
  const [checking, setChecking] = useState<string | null>(null);
  const [error, setError] = useState("");
  const drafts = useSessionStore((state) => state.questionDrafts);
  const closed = useSessionStore((state) => state.closedQuestionIds[sessionId]);
  const messages = useSessionStore((state) => state.messages[sessionId]);
  const pending = useSessionStore((state) => state.pendingQuestions[sessionId]);
  useEffect(() => {
    for (const [key, draft] of Object.entries(drafts)) {
      if (draft.sessionId !== sessionId || draft.transmission?.kind !== "answer") continue;
      for (const message of messages ?? []) {
        if (message.tool_use_id === draft.question.question_id && message.tool_input?.question_resolution) {
          observeQuestionResolution(key, message.tool_input.question_resolution);
        }
      }
    }
  }, [drafts, sessionId, messages]);
  const confirm = async (key: string) => {
    setChecking(key); setError("");
    try { await confirmQuestionReceipt(key); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "Original receipt is still unknown."); }
    finally { setChecking(null); }
  };
  return Object.entries(drafts).filter(([, draft]) => draft.sessionId === sessionId &&
    (closed?.[draft.question.question_id] || draft.transmission?.state === "unknown" ||
      !pending?.some((question) => question.question_id === draft.question.question_id))).map(([key, draft]) => (
    <div key={key} role="status" className="mb-2 rounded-md border border-border p-3 text-sm">
      <p>{closed?.[draft.question.question_id] ? "This question closed." : "The original question status is unconfirmed."} Your saved input is kept here.</p>
      {draft.transmission?.state === "unknown" && <p>Original acceptance is unknown. Check its receipt before continuing; no input will be resent.</p>}
      {draft.transmission?.message && <p>{draft.transmission.message}</p>}
      {error && <p role="alert">{error}</p>}
      <details><summary>Saved answer draft</summary><pre className="whitespace-pre-wrap">{draft.transmission?.content ?? questionDraftText(draft)}</pre>
        {draft.transmission?.attachments?.map((attachment) => <p key={attachment.uid}>{attachment.file.name}</p>)}
      </details>
      <Button size="sm" variant="outline" disabled={composerOccupied || !closed?.[draft.question.question_id] ||
        draft.transmission?.state === "unknown" || draft.transmission?.state === "pending"}
        onClick={() => onContinue(key, draft.transmission?.content ?? questionDraftText(draft), draft.transmission?.attachments ?? [])}>Continue as new message</Button>
      <Button size="sm" variant="outline" disabled={checking === key}
        onClick={() => void confirm(key)}>{checking === key ? "Checking…" : "Check original receipt"}</Button>
      <Button size="sm" variant="ghost" onClick={() => discardQuestionDraft(key)}>Discard answer draft</Button>
      {composerOccupied && <p>Keep or clear your current message before previewing this answer.</p>}
    </div>
  ));
}

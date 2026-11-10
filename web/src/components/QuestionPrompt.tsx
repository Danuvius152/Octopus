import { useCallback, useEffect, useRef, useState } from "react";
import { IconHelpCircle } from "@tabler/icons-react";
import type { PendingQuestion } from "../stores/sessionStore";
import { newSubmissionId, type QuestionCommandTarget, type SubmissionReply } from "../lib/submission";
import { useSessionStore } from "../stores/sessionStore";
import { beginQuestionTransmission, finishQuestionTransmission, questionDraftKey, saveQuestionDraft, type AnswerPayload } from "../lib/questionDrafts";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { useMessageDetail } from "./MessageDetails";

export type { AnswerPayload } from "../lib/questionDrafts";

interface Props {
  question: PendingQuestion;
  sessionId: string;
  agentLabel?: string;
  onSubmit: (questionId: string, answers: AnswerPayload[], target: QuestionCommandTarget) => void | Promise<SubmissionReply>;
  onSnooze?: (questionId: string, target: QuestionCommandTarget) => void | Promise<SubmissionReply>;
}

export function QuestionPrompt({ question, sessionId, onSubmit, onSnooze, agentLabel = "Agent" }: Props) {
  const [error, setError] = useState("");
  const detail = useMessageDetail(sessionId, question.detail_ref, question.message_seq ?? undefined);
  const saved = detail.body;
  const savedItems = saved?.tool_input?.questions;
  const complete = saved?.type === "question_request" && saved.tool_use_id === question.question_id &&
    saved.tool_input?.request_id === (question.request_id ?? null) &&
    saved.tool_input?.attempt_id === (question.attempt_id ?? null) && Array.isArray(savedItems) &&
    savedItems.every((item) => item && typeof item === "object" && typeof item.question === "string" &&
      (item.options == null || Array.isArray(item.options) && item.options.every((option: unknown) =>
        option && typeof option === "object" && "label" in option && typeof option.label === "string")));
  const needsFull = question.body_state === "preview" && !complete;
  const questions: PendingQuestion["questions"] = complete ? savedItems : question.questions;
  // For each sub-question, hold either selected option labels or a free-text
  // "Other" answer. `selected` is an array even for single-select so the
  // submit serializer is uniform.
  const accountGeneration = useSessionStore((state) => state.accountGeneration);
  const key = questionDraftKey(accountGeneration, sessionId, question);
  const requestedSnooze = useRef<string | null>(null);
  const tabPressed = useRef(false);
  const [now, setNow] = useState(Date.now);
  const optional = question.interaction_policy === "human" && question.wait_mode === "optional";
  const deadline = question.deadline ? Date.parse(question.deadline) : NaN;
  useEffect(() => {
    if (!optional || !Number.isFinite(deadline) || question.snoozed) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [optional, deadline, question.snoozed]);
  const requestSnooze = useCallback(() => {
    if (!optional || question.snoozed || !onSnooze || requestedSnooze.current === key) return;
    requestedSnooze.current = key;
    const state = useSessionStore.getState();
    try {
      const target = { clientCommandId: newSubmissionId(), accountGeneration, generation: state.dispatchGeneration,
        requestId: question.request_id ?? null, attemptId: question.attempt_id ?? null };
      void Promise.resolve(onSnooze(question.question_id, target)).then((reply) => {
        if (!reply?.accepted) setError("Pausing automatic expiry was not confirmed. The displayed deadline still comes from the server.");
      }).catch(() => setError("Pausing automatic expiry was not confirmed."));
    } catch (failure) { setError(String(failure)); }
  }, [accountGeneration, key, onSnooze, optional, question]);
  useEffect(() => {
    const down = (event: KeyboardEvent) => { tabPressed.current = event.key === "Tab"; };
    const up = () => { tabPressed.current = false; };
    document.addEventListener("keydown", down, true);
    document.addEventListener("keyup", up, true);
    window.addEventListener("blur", up);
    return () => {
      document.removeEventListener("keydown", down, true);
      document.removeEventListener("keyup", up, true);
      window.removeEventListener("blur", up);
    };
  }, []);
  const draft = useSessionStore((state) => state.questionDrafts[key]);
  const answers = draft?.answers ?? questions.map(() => ({ selected: [] }));
  const setAnswers = (update: (previous: AnswerPayload[]) => AnswerPayload[]) => {
    saveQuestionDraft({ ...draft, sessionId, accountGeneration, question: { ...question, questions }, answers: update(answers) });
  };

  const setSelected = (i: number, labels: string[]) => {
    setAnswers((prev) => {
      const next = [...prev];
      next[i] = { selected: labels, text: undefined };
      return next;
    });
  };

  const setText = (i: number, text: string) => {
    setAnswers((prev) => {
      const next = [...prev];
      next[i] = { text, selected: undefined };
      return next;
    });
  };

  const toggleMulti = (i: number, label: string) => {
    const cur = answers[i]?.selected || [];
    const exists = cur.includes(label);
    setSelected(i, exists ? cur.filter((l) => l !== label) : [...cur, label]);
  };

  const canSubmit = !needsFull && questions.length > 0 && answers.every((a) => {
    if (a.text && a.text.trim()) return true;
    return (a.selected?.length || 0) > 0;
  });

  const handleSubmit = async () => {
    if (!canSubmit) return;
    const pending = useSessionStore.getState().pendingQuestions[sessionId]?.find((current) =>
      current.question_id === question.question_id && current.request_id === question.request_id && current.attempt_id === question.attempt_id);
    if (!pending) { setError("This question is no longer pending."); return; }
    let id: string;
    try { id = newSubmissionId(); }
    catch (failure) { setError(String(failure)); return; }
    if (!beginQuestionTransmission(key, { kind: "answer", id, answers, state: "pending" })) return;
    const state = useSessionStore.getState();
    try {
      finishQuestionTransmission(key, id, await onSubmit(question.question_id, answers, {
        clientCommandId: id, accountGeneration, generation: state.dispatchGeneration,
        requestId: question.request_id ?? null, attemptId: question.attempt_id ?? null,
      }));
    } catch {
      finishQuestionTransmission(key, id, { accepted: false, state: "unknown", message: "Answer acceptance was not confirmed." });
    }
  };

  const submitting = draft?.transmission?.state === "pending";
  const uncertain = draft?.transmission?.state === "unknown";

  return (
    <div className="msg msg-question rounded-lg border-[0.7px] border-primary/30 bg-card overflow-hidden"
      onPointerDownCapture={requestSnooze} onKeyDownCapture={requestSnooze}
      onFocusCapture={() => { if (tabPressed.current) requestSnooze(); }}>
      <div className="question-header flex items-center gap-2.5 px-3 py-2 bg-primary-50 text-sm text-foreground">
        <IconHelpCircle size={18} className="text-primary shrink-0" />
        <strong>{agentLabel} is asking</strong>
      </div>
      {question.interaction_policy === "human" && question.wait_mode === "blocking" &&
        <p className="px-3 pt-2 text-sm text-muted-foreground">Waiting for your answer · no automatic question deadline</p>}
      {question.wait_mode === "mcp" && <p className="px-3 pt-2 text-sm text-muted-foreground">
        {question.deadline ? `MCP compatibility deadline: ${question.deadline}` : "MCP compatibility question · automatic deadline disabled"}
      </p>}
      {optional && <p className="px-3 pt-2 text-sm text-muted-foreground">
        {question.snoozed ? "Automatic expiry paused · waiting for your answer" : Number.isFinite(deadline)
          ? now < deadline - 60_000 ? "Optional question · the countdown starts after the initial grace period"
            : `Automatic unanswered response in ${Math.max(0, Math.ceil((deadline - now) / 1000))}s`
          : "Optional question · automatic expiry is unknown"}
      </p>}
      <div className="question-body px-3 py-3 space-y-5">
        {needsFull && <div className="space-y-2">
          <p>Question preview · load the complete question and options before answering.</p>
          {detail.busy && <p role="status">Loading full question…</p>}
          {(detail.error || saved && !complete) && <p role="alert">{detail.error || "The saved question does not match this pending question."}</p>}
          <button type="button" className="underline" disabled={detail.busy} onClick={() => void detail.load()}>Load full question</button>
        </div>}
        {!needsFull && questions.map((q, i) => {
          const multi = !!q.multiSelect;
          const selected = answers[i]?.selected || [];
          const inputName = `q-${question.question_id}-${i}`;
          return (
            <div className="question-item space-y-3" key={i}>
              {q.header && (
                <div className="question-tag text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                  {q.header}
                </div>
              )}
              <div className="question-text text-sm font-medium text-foreground leading-relaxed">
                {q.question}
              </div>
              <div className="question-options flex flex-col gap-2">
                {(q.options ?? []).map((opt, j) => {
                  const isSelected = selected.includes(opt.label);
                  return (
                    <label
                      className={`question-option flex items-start gap-3 px-4 py-3 rounded-lg border-[0.7px] cursor-pointer transition-colors ${
                        isSelected
                          ? "selected border-primary bg-primary-50"
                          : "border-border hover:bg-accent"
                      }`}
                      key={j}
                    >
                      <input
                        type={multi ? "checkbox" : "radio"}
                        name={inputName}
                        checked={isSelected}
                        onChange={() => {
                          requestSnooze();
                          return multi
                            ? toggleMulti(i, opt.label)
                            : setSelected(i, [opt.label]);
                        }}
                        className="mt-0.5 accent-primary"
                      />
                      <div className="min-w-0 flex-1">
                        <div className="question-option-label text-sm text-foreground">
                          {opt.label}
                        </div>
                        {opt.description && (
                          <div className="question-option-desc text-xs text-muted-foreground mt-1 leading-relaxed">
                            {opt.description}
                          </div>
                        )}
                      </div>
                    </label>
                  );
                })}
              </div>
              <Input
                type="text"
                className="question-other text-sm"
                placeholder="Or type your own answer…"
                value={answers[i]?.text || ""}
                onChange={(e) => { requestSnooze(); setText(i, e.target.value); }}
              />
            </div>
          );
        })}
      </div>
      <div className="question-actions flex justify-end gap-2 px-3 py-2 border-t border-border">
        {error && <p role="alert">{error}</p>}
        <Button
          className="btn btn-approve"
          onClick={handleSubmit}
          disabled={!canSubmit || submitting || uncertain}
        >
          {submitting ? "Confirming…" : "Submit"}
        </Button>
      </div>
    </div>
  );
}

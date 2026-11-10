import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { QuestionPrompt } from "./QuestionPrompt";
import { useSessionStore, type PendingQuestion } from "../stores/sessionStore";

const form: PendingQuestion = { question_id: "optional", request_id: "request", attempt_id: "attempt",
  questions: [{ question: "Your decision?" }], interaction_policy: "human", wait_mode: "optional", snoozed: false,
  deadline: "2026-11-04T08:02:00Z" };

beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.setState({ pendingQuestions: { session: [form] } });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it("loads the original large question before enabling answers and keeps current snooze facts", async () => {
  useSessionStore.setState({ token: "question-token" });
  let finish!: (value: Response) => void;
  const fetcher = vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; }));
  vi.stubGlobal("fetch", fetcher);
  const onSubmit = vi.fn();
  const preview = { ...form, questions: [], message_seq: 3, body_state: "preview" as const,
    detail_ref: "/api/sessions/session/messages/3" };
  useSessionStore.setState({ pendingQuestions: { session: [preview] } });
  const view = render(<QuestionPrompt sessionId="session" question={preview} onSubmit={onSubmit} />);
  expect(screen.getByRole("button", { name: "Submit" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Submit" }));
  expect(onSubmit).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Load full question" }));
  view.rerender(<QuestionPrompt sessionId="session" question={{ ...preview, snoozed: true, deadline: null }} onSubmit={onSubmit} />);
  await act(async () => finish(Response.json({ role: "assistant", type: "question_request", seq: 3,
    tool_use_id: form.question_id, tool_input: { questions: [{ question: "Original full question?", options: [{ label: "Exact option" }] }],
      request_id: form.request_id, attempt_id: form.attempt_id, snoozed: false, deadline: form.deadline } })));
  await waitFor(() => expect(screen.getByText("Original full question?")).toBeVisible());
  expect(screen.getByText("Automatic expiry paused · waiting for your answer")).toBeVisible();
  fireEvent.click(screen.getByLabelText("Exact option"));
  fireEvent.click(screen.getByRole("button", { name: "Submit" }));
  await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(form.question_id,
    [{ selected: ["Exact option"], text: undefined }], expect.objectContaining({ requestId: form.request_id, attemptId: form.attempt_id })));
});

it("derives grace and countdown from the owner deadline even after remount", () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date("2026-11-04T08:00:00Z"));
  const props = { sessionId: "session", question: form, onSubmit: vi.fn() };
  const view = render(<QuestionPrompt {...props} />);
  expect(screen.getByText(/initial grace period/)).toBeVisible();
  act(() => vi.advanceTimersByTime(60_000));
  expect(screen.getByText("Automatic unanswered response in 60s")).toBeVisible();
  view.unmount();
  act(() => vi.advanceTimersByTime(30_000));
  render(<QuestionPrompt {...props} />);
  expect(screen.getByText("Automatic unanswered response in 30s")).toBeVisible();
  expect(props.onSubmit).not.toHaveBeenCalled();
});

it("keeps malformed full question options behind the preview and allows another read", async () => {
  useSessionStore.setState({ token: "question-token" });
  const preview = { ...form, questions: [], message_seq: 3, body_state: "preview" as const,
    detail_ref: "/api/sessions/session/messages/3" };
  const saved = { role: "assistant", type: "question_request", seq: 3, tool_use_id: form.question_id,
    tool_input: { request_id: form.request_id, attempt_id: form.attempt_id, questions: [{ question: "Original question", options: "broken" }] } };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(Response.json(saved))
    .mockResolvedValueOnce(Response.json({ ...saved, tool_input: { ...saved.tool_input,
      questions: [{ question: "Original question", options: [{ label: "Original option" }] }] } })));
  render(<QuestionPrompt sessionId="session" question={preview} onSubmit={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Load full question" }));
  await waitFor(() => expect(screen.getByRole("alert")).toBeVisible());
  expect(screen.getByRole("button", { name: "Submit" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Load full question" }));
  await waitFor(() => expect(screen.getByLabelText("Original option")).toBeVisible());
});

it("requires active interaction to request one owner snooze and keeps the deadline until owner confirmation", async () => {
  const onSnooze = vi.fn(async () => ({ accepted: true }));
  render(<><button>Outside</button><QuestionPrompt sessionId="session" question={form} onSubmit={vi.fn()} onSnooze={onSnooze} /></>);
  const input = screen.getByRole("textbox");
  act(() => input.focus());
  expect(onSnooze).not.toHaveBeenCalled();
  act(() => screen.getByRole("button", { name: "Outside" }).focus());
  fireEvent.keyDown(document.activeElement!, { key: "Tab" });
  await act(async () => input.focus());
  fireEvent.keyUp(input, { key: "Tab" });
  fireEvent.pointerDown(input);
  fireEvent.change(input, { target: { value: "My decision" } });
  fireEvent.keyDown(input, { key: "ArrowLeft" });
  expect(onSnooze).toHaveBeenCalledTimes(1);
  expect(onSnooze).toHaveBeenCalledWith("optional", expect.objectContaining({
    clientCommandId: expect.any(String), requestId: "request", attemptId: "attempt",
  }));
  expect(screen.queryByText("Automatic expiry paused · waiting for your answer")).not.toBeInTheDocument();
});

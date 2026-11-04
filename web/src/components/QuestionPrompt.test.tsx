import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
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
afterEach(() => { cleanup(); vi.useRealTimers(); });

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

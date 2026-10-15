import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ChatView } from "./ChatView";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import commandCasesText from "../../../tests/fixtures/wiki-command-cases.json?raw";

const commandCases: { message: string; mode: "read" | "maintain" | null }[] = JSON.parse(commandCasesText);

const session = {
  id: "wiki-session", name: "Wiki chat", working_dir: "/tmp",
  created_at: "2026-10-12", status: "idle", backend: "codex",
} as SessionInfo;

beforeEach(() => {
  useSessionStore.setState({
    activeSessionId: session.id, sessions: [session], agents: [],
    messages: {}, streamingText: {}, pendingQueue: {}, pendingQuestions: {},
    dispatchStates: {}, dispatchGeneration: null, retiredDispatchGenerations: [],
    pendingForks: {}, credentials: [], backendModels: {},
  });
});
afterEach(cleanup);

it("keeps correction separate from paused results and resumes only on the explicit button", async () => {
  const paused = {
    generation: "service-ui", revision: 1, paused: true, active_request_id: "correction",
    items: [{ request_id: "bg-1", source: "bg", summary: "background result", priority: false }],
  };
  useSessionStore.getState().applyDispatchState(session.id, paused);
  const resume = vi.fn(async () => new Response(JSON.stringify({ ...paused, revision: 2, paused: false })));
  vi.stubGlobal("fetch", resume);
  const { input, send, sendMessage } = setup();
  expect(screen.getByText("Paused · 1 pending")).toBeTruthy();
  expect(screen.getByText("Background task")).toBeTruthy();
  fireEvent.change(input, { target: { value: "correct the direction" } });
  fireEvent.click(send);
  expect(sendMessage).toHaveBeenCalledWith(session.id, "correct the direction", undefined, undefined);
  expect(useSessionStore.getState().dispatchStates[session.id].paused).toBe(true);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Resume processing" })));
  expect(useSessionStore.getState().dispatchStates[session.id].paused).toBe(false);
  expect(resume).toHaveBeenCalledWith(expect.stringContaining(`/api/sessions/${session.id}/dispatch/resume`),
    expect.objectContaining({ method: "POST" }));
  vi.unstubAllGlobals();
});

it("shows pause independently while a correction runs, including an empty pending queue", () => {
  useSessionStore.setState({ sessions: [{ ...session, status: "running" }] });
  useSessionStore.getState().applyDispatchState(session.id, {
    generation: "service-ui", revision: 1, paused: true, active_request_id: "correction", items: [],
  });
  const sendMessage = vi.fn();
  render(<ChatView sendMessage={sendMessage} interrupt={vi.fn()} approveTool={vi.fn()} denyTool={vi.fn()}
    answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />);
  expect(screen.getByText("running")).toBeTruthy();
  expect(screen.getByText("Paused · 0 pending")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Resume processing" })).toBeTruthy();
  expect(screen.getByText(/Your next message takes priority/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
});

it("keeps the queue paused and reports a failed resume", async () => {
  useSessionStore.getState().applyDispatchState(session.id, {
    generation: "service-ui", revision: 1, paused: true, active_request_id: null, items: [],
  });
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Turn quota exceeded" }), { status: 429 })));
  setup();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Resume processing" })));
  expect(screen.getByRole("alert")).toHaveTextContent("Turn quota exceeded");
  expect(useSessionStore.getState().dispatchStates[session.id].paused).toBe(true);
  expect(screen.getByRole("button", { name: "Resume processing" })).not.toBeDisabled();
  vi.unstubAllGlobals();
});

function setup() {
  const sendMessage = vi.fn();
  render(<ChatView sendMessage={sendMessage} interrupt={vi.fn()}
    approveTool={vi.fn()} denyTool={vi.fn()} answerQuestion={vi.fn()}
    connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />);
  return {
    sendMessage,
    input: screen.getByRole("textbox"),
    send: screen.getByRole("button", { name: "Send message" }),
  };
}

it("keeps a rejected Wiki draft and protects subsequent editing during restoration", async () => {
  let finish!: (reply: { accepted: boolean; message: string }) => void;
  const sendMessage = vi.fn(() => new Promise<{ accepted: boolean; message: string }>((resolve) => { finish = resolve; }));
  render(<ChatView sendMessage={sendMessage} interrupt={vi.fn()} approveTool={vi.fn()}
    denyTool={vi.fn()} answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />);
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "  /wiki maintain Library/a.md  " } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  fireEvent.change(input, { target: { value: "new draft" } });
  await act(async () => finish({ accepted: false, message: "Conversation is closed" }));
  expect(input).toHaveValue("new draft");
  expect(screen.getByRole("alert")).toHaveTextContent("Conversation is closed");
  expect(screen.getByRole("button", { name: "Restore draft" })).toBeDisabled();
  fireEvent.change(input, { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Restore draft" }));
  expect(input).toHaveValue("  /wiki maintain Library/a.md  ");
  expect(screen.queryByRole("button", { name: "Use local Wiki for this message" })).not.toBeInTheDocument();
  expect(sendMessage).toHaveBeenCalledTimes(1);
});

describe("shared Wiki without a composer switch", () => {
  it.each(["请结合共享知识解释这个问题", ...commandCases.map(({ message }) => message)])(
    "sends the original request without an implicit Wiki mode: %s", (message) => {
      const { input, send, sendMessage } = setup();
      expect(screen.queryByRole("button", { name: "Use local Wiki for this message" })).not.toBeInTheDocument();
      fireEvent.change(input, { target: { value: message } });
      fireEvent.click(send);
      expect(sendMessage).toHaveBeenLastCalledWith(session.id, message.trim(), undefined, undefined);
      expect(input).toHaveValue("");
    },
  );

  it.each(["普通查询", "/wiki 查询资料", "/wiki maintain Library/a.md"])(
    "keeps an unsent draft across conversation switches: %s", (message) => {
      const { input, send, sendMessage } = setup();
      fireEvent.change(input, { target: { value: message } });
      act(() => useSessionStore.setState({
        activeSessionId: "other-session",
        sessions: [session, { ...session, id: "other-session" }],
      }));
      expect(input).toHaveValue(message);
      fireEvent.click(send);
      expect(sendMessage).toHaveBeenLastCalledWith("other-session", message, undefined, undefined);
    },
  );

  it("does not send an empty request", () => {
    const { input, sendMessage } = setup();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(sendMessage).not.toHaveBeenCalled();
  });
});

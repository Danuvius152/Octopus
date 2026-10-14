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
    wiki: screen.getByRole("button", { name: "Use local Wiki for this message" }),
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
  const wiki = screen.getByRole("button", { name: "Use local Wiki for this message" });
  fireEvent.change(input, { target: { value: "  /wiki maintain Library/a.md  " } });
  fireEvent.click(wiki);
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  fireEvent.change(input, { target: { value: "new draft" } });
  await act(async () => finish({ accepted: false, message: "Conversation is closed" }));
  expect(input).toHaveValue("new draft");
  expect(screen.getByRole("alert")).toHaveTextContent("Conversation is closed");
  expect(screen.getByRole("button", { name: "Restore draft" })).toBeDisabled();
  fireEvent.change(input, { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Restore draft" }));
  expect(input).toHaveValue("  /wiki maintain Library/a.md  ");
  expect(wiki).toHaveAttribute("aria-pressed", "false");
  expect(sendMessage).toHaveBeenCalledTimes(1);
});

describe("Wiki composer selection", () => {
  it("gives visible selection feedback and can be turned off", () => {
    const { wiki } = setup();
    expect(wiki).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(wiki);
    expect(wiki).toHaveAttribute("aria-pressed", "true");
    expect(wiki).toHaveClass("bg-primary-50", "text-primary", "border-primary-200");
    expect(wiki).toHaveTextContent("Wiki · 已启用");
    fireEvent.click(wiki);
    expect(wiki).toHaveAttribute("aria-pressed", "false");
    expect(wiki).not.toHaveClass("bg-primary-50");
  });

  it("grants read access to one sent message and resets for the next", () => {
    const { wiki, input, send, sendMessage } = setup();
    fireEvent.click(wiki);
    fireEvent.change(input, { target: { value: "查找相关文章" } });
    fireEvent.click(send);
    expect(sendMessage).toHaveBeenLastCalledWith(session.id, "查找相关文章", undefined, "read");
    expect(wiki).toHaveAttribute("aria-pressed", "false");
    fireEvent.change(input, { target: { value: "继续普通聊天" } });
    fireEvent.click(send);
    expect(sendMessage).toHaveBeenLastCalledWith(session.id, "继续普通聊天", undefined, undefined);
  });

  it("keeps the selection when an empty message cannot be sent", () => {
    const { wiki, input, sendMessage } = setup();
    fireEvent.click(wiki);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(sendMessage).not.toHaveBeenCalled();
    expect(wiki).toHaveAttribute("aria-pressed", "true");
  });

  it("clears the selection when switching conversations", () => {
    const { wiki } = setup();
    fireEvent.click(wiki);
    act(() => useSessionStore.setState({
      activeSessionId: "other-session",
      sessions: [session, { ...session, id: "other-session" }],
    }));
    expect(wiki).toHaveAttribute("aria-pressed", "false");
  });

  it.each([
    ["/wiki 查询相关文章", false],
    ["/wiki 查询相关文章", true],
    ["/wiki maintain Library/a.md", false],
    ["/wiki maintain Library/a.md", true],
  ])("preserves cancellation for %s across conversation switches (return=%s)", (command, returnToOriginal) => {
    const { wiki, input, send, sendMessage } = setup();
    fireEvent.change(input, { target: { value: command } });
    fireEvent.click(wiki);
    act(() => useSessionStore.setState({
      activeSessionId: "other-session",
      sessions: [session, { ...session, id: "other-session" }],
    }));
    expect(input).toHaveValue(command);
    expect(wiki).toHaveAttribute("aria-pressed", "false");
    if (returnToOriginal) {
      act(() => useSessionStore.setState({ activeSessionId: session.id }));
      expect(input).toHaveValue(command);
      expect(wiki).toHaveAttribute("aria-pressed", "false");
    }
    fireEvent.click(send);
    expect(sendMessage).toHaveBeenLastCalledWith(
      returnToOriginal ? session.id : "other-session", command, undefined, "off",
    );
  });

  it.each([
    ["/wiki 查找相关文章", "Wiki · 已启用"],
    ["  /wiki\nmaintain Library/a.md", "Wiki · 整理"],
    ["/wiki /wiki maintain 的用法是什么？", "Wiki · 已启用"],
  ])("reflects the explicit command %s and lets the user cancel it", (command, label) => {
    const { wiki, input, send, sendMessage } = setup();
    fireEvent.change(input, { target: { value: command } });
    expect(wiki).toHaveAttribute("aria-pressed", "true");
    expect(wiki).toHaveTextContent(label);
    fireEvent.click(wiki);
    expect(wiki).toHaveAttribute("aria-pressed", "false");
    expect(input).toHaveValue(command);
    fireEvent.click(send);
    expect(sendMessage).toHaveBeenLastCalledWith(session.id, command.trim(), undefined, "off");
  });

  it.each(commandCases)("agrees with the server for $message", ({ message, mode }) => {
    const { wiki, input } = setup();
    fireEvent.change(input, { target: { value: message } });
    expect(wiki).toHaveAttribute("aria-pressed", String(mode !== null));
    expect(wiki).toHaveTextContent(mode === "maintain" ? "Wiki · 整理" : mode === "read" ? "Wiki · 已启用" : "Wiki");
  });

  it("can re-enable a cancelled command without rewriting the draft", () => {
    const { wiki, input, send, sendMessage } = setup();
    const command = "/wiki maintain Library/a.md";
    fireEvent.change(input, { target: { value: command } });
    fireEvent.click(wiki);
    fireEvent.click(wiki);
    expect(wiki).toHaveTextContent("Wiki · 整理");
    expect(input).toHaveValue(command);
    fireEvent.click(send);
    expect(sendMessage).toHaveBeenLastCalledWith(session.id, command, undefined, undefined);
  });

  it.each(["讨论 /wiki 的用法", "/wikimedia", "/wiki-maintain", "/Wiki 查询"])(
    "does not indicate access for ordinary text %s", (text) => {
      const { wiki, input } = setup();
      fireEvent.change(input, { target: { value: text } });
      expect(wiki).toHaveAttribute("aria-pressed", "false");
    },
  );
});

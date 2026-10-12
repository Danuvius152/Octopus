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
    pendingForks: {}, credentials: [], backendModels: {},
  });
});
afterEach(cleanup);

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

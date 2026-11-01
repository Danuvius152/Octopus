import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test } from "vitest";
import { MessageBubble } from "./MessageBubble";
import { nativeChatMessages } from "../lib/nativeOutput";
import { useSessionStore } from "../stores/sessionStore";

beforeEach(() => useSessionStore.setState({ messages: {}, streamingItems: {}, subagents: {} }));

test("collected native helper body appears in its card after refresh with its actual completion", () => {
  useSessionStore.getState().setMessages("s", [
    { role: "system", type: "native_subagent", seq: 1, tool_input: { task_id: "child", card_id: "c", name: "/root/reviewer", status: "running", native_thread_id: "child", native_turn_id: "t" } },
    { role: "assistant", type: "text", seq: 2, content: "Helper result", runtime_metadata: { request_id: "r", attempt_id: "a", native: {
      thread_id: "child", turn_id: "t", item_id: "answer", root_thread_id: "root", root_turn_id: "rt" } } },
    { role: "system", type: "native_subagent", seq: 3, runtime_metadata: { request_id: "r", attempt_id: "a" }, tool_input: { task_id: "child", card_id: "c", status: "completed", native_thread_id: "child", native_turn_id: "t" } },
    { role: "assistant", type: "text", seq: 4, content: "Root result" },
  ]);
  const messages = nativeChatMessages(useSessionStore.getState().messages.s);
  const { container } = render(<>{messages.map((message) => <MessageBubble key={message.seq} message={message} sessionId="s" />)}</>);
  expect(container.querySelectorAll(".subagent-card")).toHaveLength(1);
  expect(screen.getByText("helper finished")).toBeInTheDocument();
  expect(screen.getByText("Root result")).toBeInTheDocument();
  expect(screen.getAllByText("Helper result")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: /reviewer/ }));
  expect(screen.getByText("Helper result")).toBeInTheDocument();
});

test("the existing snapshot supplies an active card when the loaded window has only tool output", () => {
  useSessionStore.setState({ subagents: { s: { cached: {
    task_id: "child", card_id: "cached", native_thread_id: "child", native_turn_id: "t", status: "running",
    name: "/root/cached", description: "working", prompt: "", summary: "", steps: [],
    runtime_metadata: { request_id: "r", attempt_id: "a", native: {
      thread_id: "child", turn_id: "t", item_id: null, root_thread_id: "root", root_turn_id: "rt" } },
  } } } });
  render(<MessageBubble sessionId="s" message={{ role: "system", type: "native_subagent", tool_input: { card_id: "cached" } }} />);
  expect(screen.getByText("/root/cached")).toBeInTheDocument();
  expect(screen.getByText("working")).toBeInTheDocument();
  expect(screen.queryByText("helper finished")).not.toBeInTheDocument();
});

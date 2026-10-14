import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";

import { MessageBubble } from "./MessageBubble";
import { useSessionStore, type BgTask, type Message, type SubagentRun } from "../stores/sessionStore";
import type { ToolReceipt } from "../api";
import { handleWsMessage } from "../hooks/useWebSocket";

const metadata = (call: string, receipt?: ToolReceipt) => ({
  request_id: `request-${call}`, attempt_id: `attempt-${call}`, tool_call_id: call,
  receipt: receipt ?? null,
});
const task = (id: string, status: BgTask["status"]): BgTask => ({
  id, session_id: "parent", working_dir: "/tmp", command: "pytest tests/", description: "Run backend tests", status,
  stdout: status === "completed" ? "All passed" : "", stderr: "", exit_code: status === "completed" ? 0 : null,
  truncated: false, started_at: "2026-10-14T00:00:00Z", completed_at: null,
});
const toolUse = (call: string): Message => ({
  role: "assistant", type: "tool_use", tool_name: "mcp__bg__run", tool_use_id: "item_0",
  tool_input: { command: "pytest tests/" }, runtime_metadata: metadata(call),
});
const result = (call: string, taskId: string): Message => ({
  role: "tool", type: "tool_result", tool_use_id: "item_0", content: `Started background command ${taskId}`,
  runtime_metadata: metadata(call, { kind: "bg", task_id: taskId }),
});

beforeEach(() => {
  useSessionStore.setState({ token: "token", messages: {}, bgTasks: {}, delegations: {}, subagents: {}, lastAppliedSeq: {} });
  vi.stubGlobal("fetch", vi.fn(async () => new Response("[]", { status: 200 })));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it.each(["live", "history"])("binds identical bg commands and repeated provider IDs correctly in %s order", async (mode) => {
  const old = toolUse("call-old");
  const fresh = toolUse("call-new");
  if (mode === "live") {
    for (const [call, id, seq] of [["call-old", "bg-old", 1], ["call-new", "bg-new", 3]] as const) {
      handleWsMessage({ type: "tool_use", session_id: "parent", seq: seq - 1,
        tool: "mcp__bg__run", input: { command: "pytest tests/" }, tool_use_id: "item_0", runtime_metadata: metadata(call) });
      handleWsMessage({ type: "tool_result", session_id: "parent", seq, output: `Started ${id}`,
        tool_use_id: "item_0", runtime_metadata: metadata(call, { kind: "bg", task_id: id }) });
    }
    useSessionStore.getState().setBgTasks("parent", [task("bg-old", "completed"), task("bg-new", "running")]);
  } else {
    useSessionStore.getState().setMessages("parent", [old, result("call-old", "bg-old"), fresh, result("call-new", "bg-new")]);
    useSessionStore.getState().setBgTasks("parent", [task("bg-new", "running"), task("bg-old", "completed")]);
  }
  const { container } = render(<><MessageBubble sessionId="parent" message={old} /><MessageBubble sessionId="parent" message={fresh} /></>);
  const cards = container.querySelectorAll<HTMLElement>(".octo-bgtask-chip");
  expect(within(cards[0]).getByText("bg · completed")).toBeTruthy();
  expect(within(cards[0]).queryByTitle("Cancel this background task")).toBeNull();
  expect(within(cards[1]).getByText("bg · running")).toBeTruthy();
  await act(async () => fireEvent.click(within(cards[1]).getByTitle("Cancel this background task")));
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/bg-tasks/bg-new/cancel"), expect.objectContaining({ method: "POST" }));
  expect(screen.getAllByText("pytest tests/")).toHaveLength(2);
});

it("shows an exact-call failure without borrowing an identically named task", () => {
  const failed = { ...result("call-failed", "bg-live"), is_error: true, content: "Command rejected" };
  useSessionStore.getState().setMessages("parent", [toolUse("call-failed"), failed]);
  useSessionStore.getState().setBgTasks("parent", [task("bg-live", "running")]);
  render(<MessageBubble sessionId="parent" message={toolUse("call-failed")} />);
  expect(screen.getByText("Background command failed to start")).toBeTruthy();
  expect(screen.queryByTitle("Cancel this background task")).toBeNull();
});

it("keeps a newly emitted bg call pending until its own receipt arrives", () => {
  useSessionStore.getState().setBgTasks("parent", [task("unrelated", "running")]);
  render(<MessageBubble sessionId="parent" message={toolUse("call-pending")} />);
  expect(screen.getByText("Waiting for background task receipt…")).toBeTruthy();
  expect(screen.queryByTitle("Cancel this background task")).toBeNull();
});

it.each(["live", "snapshot"])("keeps native helpers separate under repeated provider IDs in %s state", (mode) => {
  const old = { ...toolUse("native-old"), tool_name: "subagent:spawn_agent" };
  const fresh = { ...toolUse("native-new"), tool_name: "subagent:spawn_agent" };
  const runs: SubagentRun[] = [
    { task_id: "helper-old", tool_use_id: "item_0", tool_call_id: "native-old", status: "completed", name: "First helper", description: "First work", prompt: "First brief", summary: "First answer", steps: [] },
    { task_id: "helper-new", tool_use_id: "item_0", tool_call_id: "native-new", status: "running", name: "Second helper", description: "Second work", prompt: "Second brief", summary: "", steps: [] },
  ];
  if (mode === "live") {
    for (const run of runs) handleWsMessage({ type: "subagent", session_id: "parent", ...run });
  } else useSessionStore.getState().setSubagents("parent", runs);
  const { container } = render(<><MessageBubble sessionId="parent" message={old} /><MessageBubble sessionId="parent" message={fresh} /></>);
  const cards = container.querySelectorAll<HTMLElement>(".subagent-card");
  expect(cards[0]).toHaveClass("subagent-completed");
  expect(within(cards[0]).getByText("First answer")).toBeTruthy();
  expect(cards[1]).toHaveClass("subagent-running");
  expect(within(cards[1]).getByText("Second work")).toBeTruthy();
});

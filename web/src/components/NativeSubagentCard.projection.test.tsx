import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { NativeSubagentCard } from "./SubagentCard";
import * as nativeOutput from "../lib/nativeOutput";
import { useSessionStore, type Message, type SubagentRun } from "../stores/sessionStore";

const identity = (index: number, item = "live") => ({ request_id: "request", attempt_id: "attempt", native: {
  thread_id: `child-${index}`, turn_id: "turn", item_id: item, root_thread_id: "root", root_turn_id: "root-turn",
} });
const saved = (): Message[] => Array.from({ length: 200 }, (_, seq) => seq < 4
  ? { role: "system", type: "native_subagent", seq, runtime_metadata: identity(seq), tool_input: {
    task_id: `child-${seq}`, card_id: `card-${seq}`, status: "running", name: `Child ${seq}`,
    native_thread_id: `child-${seq}`, native_turn_id: "turn", summary: `Saved child ${seq}`,
  } }
  : { role: "assistant", type: "text", seq, content: `Root history ${seq}` });

beforeEach(() => {
  useSessionStore.setState({ ...useSessionStore.getInitialState(), token: "projection-account" }, true);
  useSessionStore.getState().setMessages("session", saved());
  useSessionStore.getState().setActiveAttempt("session", { request_id: "request", attempt_id: "attempt" });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("keeps four cards' saved projections and detail anchors across twenty accepted live deltas", () => {
  const project = vi.spyOn(nativeOutput, "nativeCard");
  const anchor = vi.spyOn(nativeOutput, "nativeDetailAnchor");
  const { container } = render(<>{[0, 1, 2, 3].map((index) => <NativeSubagentCard key={index} sessionId="session" cardId={`card-${index}`} />)}</>);
  expect(container.querySelectorAll(".subagent-card")).toHaveLength(4);
  expect(project).toHaveBeenCalledTimes(4);
  const original = useSessionStore.getState().messages.session;
  project.mockClear(); anchor.mockClear();
  for (let index = 0; index < 20; index++) {
    act(() => useSessionStore.getState().appendStreamingText("session", "x", identity(0)));
  }
  expect(container.querySelector(".subagent-summary")?.textContent).toBe(`Saved child 0\n\n${"x".repeat(20)}`);
  expect(useSessionStore.getState().messages.session).toBe(original);
  expect(project.mock.calls.length).toBe(0);
  expect(anchor.mock.calls.length).toBe(0);
});

it("updates saved body, terminal status and snapshot facts after retaining the live preview", () => {
  const project = vi.spyOn(nativeOutput, "nativeCard");
  const anchor = vi.spyOn(nativeOutput, "nativeDetailAnchor");
  const { container } = render(<NativeSubagentCard sessionId="session" cardId="card-0" />);
  act(() => useSessionStore.getState().appendStreamingText("session", "Live preview", identity(0)));
  expect(container.querySelector(".subagent-summary")?.textContent).toContain("Live preview");
  project.mockClear(); anchor.mockClear();

  act(() => useSessionStore.getState().addMessage("session", { role: "assistant", type: "text", seq: 200,
    content: "Collected answer", runtime_metadata: identity(0) }));
  expect(screen.getByText("Collected answer")).toBeInTheDocument();
  expect(screen.queryByText(/Live preview/)).not.toBeInTheDocument();
  expect(Object.values(useSessionStore.getState().streamingItems.session)).toHaveLength(0);
  act(() => useSessionStore.getState().addMessage("session", { role: "system", type: "native_subagent", seq: 201,
    runtime_metadata: identity(0), tool_input: { card_id: "card-0", status: "completed" } }));
  expect(screen.getByText("helper finished")).toBeInTheDocument();
  act(() => useSessionStore.getState().setSubagents("session", [{ task_id: "child-0", card_id: "card-0", status: "running",
    native_thread_id: "child-0", native_turn_id: "turn", name: "Snapshot child", description: "Updated snapshot", prompt: "", summary: "", steps: [],
    runtime_metadata: identity(0) } as SubagentRun]));
  expect(screen.getByText("Snapshot child")).toBeInTheDocument();
  expect(screen.getByText("helper finished")).toBeInTheDocument();
  expect(screen.getByText("Collected answer")).toBeInTheDocument();
  expect(project.mock.calls.length).toBe(3);
  expect(anchor.mock.calls.length).toBe(3);
});

it("keeps an expanded work open and reads its newly paged persisted anchor", async () => {
  const store = useSessionStore.getState();
  store.installTranscript("session", { messages: saved().map((row) => ({ ...row, seq: row.seq! + 400 })), high: 599, oldestSeq: 400, hasMore: true });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => Response.json({
    anchor_seq: String(url).includes("/200?") ? 200 : 400, availability: "collected", child: { status: "running" }, messages: [], has_more: false,
  })));
  const project = vi.spyOn(nativeOutput, "nativeCard");
  const anchor = vi.spyOn(nativeOutput, "nativeDetailAnchor");
  render(<NativeSubagentCard sessionId="session" cardId="card-0" />);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: /Child 0/ })); });
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/native-details/400?"), expect.anything());
  project.mockClear(); anchor.mockClear();
  const page = Array.from({ length: 200 }, (_, index): Message => index === 0
    ? { role: "system", type: "native_subagent", seq: 200, runtime_metadata: identity(0), tool_input: { card_id: "card-0", status: "running", name: "Earlier name" } }
    : { role: "assistant", type: "text", seq: 200 + index, content: `Earlier root ${index}` });
  await act(async () => { store.prependMessages("session", page, 200, false,
    { beforeSeq: 400, window: useSessionStore.getState().messageWindows.session }); });
  expect(screen.getByRole("button", { name: /Child 0/ })).toHaveAttribute("aria-expanded", "true");
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/native-details/200?"), expect.anything());
  expect(project.mock.calls.length).toBe(1);
  expect(anchor.mock.calls.length).toBe(1);
});

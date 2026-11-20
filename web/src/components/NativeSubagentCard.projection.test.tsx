import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { components } from "../api/contracts";
import { NativeSubagentCard } from "./SubagentCard";
import { applySessionSnapshot } from "../lib/hydrateSession";
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

it("reads a long saved window once across mounted historical cards", () => {
  const count = 16;
  const history: Message[] = Array.from({ length: 6_000 }, (_, seq) => {
    const index = seq % count;
    if (seq < count) return { role: "system", type: "native_subagent", seq, runtime_metadata: identity(index),
      tool_input: { card_id: `card-${index}`, status: "completed", name: `Historical child ${index}`,
        native_thread_id: `child-${index}`, native_turn_id: "turn" } };
    if (seq < 2 * count) return { role: "assistant", type: "text", seq,
      content: `Historical answer ${index}`, runtime_metadata: identity(index, "saved") };
    return { role: "assistant", type: "text", seq, content: `Root history ${seq}` };
  });
  let rowReads = 0;
  const messages = new Proxy(history, {
    get(target, key, receiver) {
      if (typeof key === "string" && /^\d+$/.test(key)) rowReads++;
      return Reflect.get(target, key, receiver);
    },
  });
  useSessionStore.setState({ messages: { session: messages } });
  const { container } = render(<>{Array.from({ length: count }, (_, index) =>
    <NativeSubagentCard key={index} sessionId="session" cardId={`card-${index}`} />)}</>);
  expect([...container.querySelectorAll(".subagent-summary")].map((row) => row.textContent))
    .toEqual(Array.from({ length: count }, (_, index) => `Historical answer ${index}`));
  expect(rowReads).toBeLessThanOrEqual(history.length);
});

it("keeps four cards' saved projections and detail anchors across twenty accepted live deltas", () => {
  const project = vi.spyOn(nativeOutput, "nativeCardState");
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
  const project = vi.spyOn(nativeOutput, "nativeCardState");
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
  const project = vi.spyOn(nativeOutput, "nativeCardState");
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

it("reads saved helper details when a complete snapshot supplies the native turn", async () => {
  const sessionId = "snapshot-session";
  const runtime_metadata = identity(0, "saved");
  const run: SubagentRun = { task_id: "child-0", card_id: "card-0", status: "completed", name: "Child from snapshot",
    description: "", prompt: "", summary: "", steps: [], native_thread_id: "child-0", native_turn_id: null, runtime_metadata };
  const saved = { role: "assistant", type: "text", seq: 7, content: "Saved helper answer", runtime_metadata,
    body_state: "inline", identity_unavailable: false, attachments: [] } satisfies components["schemas"]["BrowserMessageContent"];
  const snapshot = { id: sessionId, messages: [saved], next_message_seq: 8, oldest_loaded_seq: 7, has_more_messages: false,
    dispatch_state: { generation: "cards", revision: 0, paused: false, active_request_id: null, active_operation: null, items: [] },
    active_attempt: null };
  const detail = { anchor_seq: saved.seq, availability: "available", messages: [saved], next_after_seq: saved.seq, has_more: false,
    child: { task_id: "child-0", native_thread_id: "child-0", native_turn_id: "turn", runtime_metadata,
      status: "unknown", name: "", description: "", prompt: "", summary: "", steps: [], display_truncated: false },
  } satisfies components["schemas"]["BrowserNativeDetailPage"];
  const fetchDetails = vi.fn(async () => Response.json(detail));
  vi.stubGlobal("fetch", fetchDetails);
  applySessionSnapshot(sessionId, { ...snapshot, subagents: [run] });
  render(<NativeSubagentCard sessionId={sessionId} cardId="card-0" />);

  const toggleCard = () => fireEvent.click(screen.getByRole("button", { name: /Child from snapshot/ }));
  await act(async () => { toggleCard(); });
  expect(screen.getByText("No saved reference is available for this helper yet.")).toBeInTheDocument();
  expect(fetchDetails).not.toHaveBeenCalled();
  toggleCard();

  act(() => { applySessionSnapshot(sessionId, { ...snapshot, subagents: [{ ...run, native_turn_id: "turn" }] }); });
  await act(async () => { toggleCard(); });
  expect(fetchDetails).toHaveBeenCalledExactlyOnceWith(`/api/sessions/${sessionId}/native-details/7?view=browser`,
    expect.objectContaining({ headers: { Authorization: "Bearer projection-account" } }));
  const savedDetails = screen.getByRole("region", { name: "Saved helper details" });
  expect(within(savedDetails).getByText("Saved helper answer")).toBeInTheDocument();
});

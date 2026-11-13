import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";

const session = { id: "viewport-a", name: "A", agent_id: "agent", status: "idle", backend: "codex",
  created_at: "2026-11-13", working_dir: "/tmp", archived: false } as SessionInfo;
const queued = new Map<number, FrameRequestCallback>();
let serial = 0;

beforeEach(() => {
  mockChatViewport();
  sessionStorage.clear();
  useSessionStore.getState().setToken("");
  useSessionStore.setState({ activeSessionId: session.id, mainView: "chat", sessions: [session], archivedSessions: [],
    agents: [], credentials: [], backendModels: {},
    messages: { [session.id]: [{ role: "assistant", type: "text", seq: 1, content: "Saved text" }] },
    messageWindows: { [session.id]: Symbol("original window") },
    streamingText: {}, streamingItems: {}, pendingQuestions: {}, subagents: {}, dispatchStates: {}, dispatchUnavailable: {},
  });
  queued.clear(); serial = 0;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    const id = ++serial; queued.set(id, callback); return id;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => queued.delete(id));
  // Only lifecycle is under test here. These platform dimensions enable a
  // pending position; real geometry remains covered by the browser tests.
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("chat-messages") ? 600 : 0;
  });
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("chat-messages") ? 1200 : 0;
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it.each(["before commit", "after successor commit"])("retires a pending position on A→B→A %s", (delivery) => {
  const view = render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()}
      connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>);
  const viewport = view.container.querySelector<HTMLElement>(".chat-messages")!;
  const writes: number[] = [];
  viewport.scrollTo = (optionsOrX?: ScrollToOptions | number, y?: number) => {
    const top = typeof optionsOrX === "number" ? y ?? 0 : optionsOrX?.top ?? 0;
    writes.push(top); viewport.scrollTop = Math.max(0, Math.min(600, top));
  };
  expect(queued.size).toBeGreaterThan(0);
  // A browser callback may already have been taken from its queue. Deliver
  // that captured batch while React still owes the navigation cleanup.
  const delivered = [...queued.values()]; queued.clear();
  act(() => {
    useSessionStore.getState().setActiveSessionId("viewport-b");
    useSessionStore.getState().setActiveSessionId(session.id);
    if (delivery === "before commit") for (const callback of delivered) callback(performance.now());
    expect(writes.filter((top) => top > 10)).toEqual([]);
    expect(viewport.scrollTop).toBe(0);
  });
  if (delivery === "after successor commit") act(() => {
    for (const callback of delivered) callback(performance.now());
    expect(writes.filter((top) => top > 10)).toEqual([]);
  });
  // The predecessor's callback/cleanup must leave the successor usable.
  const successor = [...queued.values()]; queued.clear();
  act(() => { for (const callback of successor) callback(performance.now()); });
  expect(writes.some((top) => top > 10)).toBe(true);
});

it("cancels initial positioning at the fake top without hiding manual history", () => {
  useSessionStore.setState({
    messages: { [session.id]: [{ role: "assistant", type: "text", seq: 200, content: "Saved text" }] },
    oldestSeq: { [session.id]: 200 }, hasMoreMessages: { [session.id]: true },
  });
  const view = render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()}
      connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>);
  const viewport = view.container.querySelector<HTMLElement>(".chat-messages")!;
  const write = vi.fn(); viewport.scrollTo = write;
  const delivered = [...queued.values()]; queued.clear();
  expect(viewport.scrollTop).toBe(0);
  // Controlled native-input signal with no displacement: jsdom has no native
  // scrolling. Browser geometry and actual paging are covered by E2E.
  fireEvent.keyDown(viewport, { key: "PageDown" });
  act(() => { for (const callback of delivered) callback(performance.now()); });
  expect(write).not.toHaveBeenCalled();
  expect(viewport.scrollTop).toBe(0);
  expect(view.getByRole("button", { name: "Load earlier messages" })).toBeVisible();
  act(() => useSessionStore.getState().addMessage(session.id,
    { role: "assistant", type: "text", seq: 201, content: "Output while reading" }));
  act(() => { for (const callback of [...queued.values()]) callback(performance.now()); queued.clear(); });
  expect(write).not.toHaveBeenCalled();
});

it("does not start automatic history from a top report whose viewport becomes zero-height before the effect", async () => {
  useSessionStore.setState({ token: "viewport-test", connected: true,
    messages: { [session.id]: [{ role: "assistant", type: "text", seq: 200, content: "Saved text" }] },
    oldestSeq: { [session.id]: 200 }, hasMoreMessages: { [session.id]: true },
  });
  const requests: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    if (input.includes("before_seq=")) requests.push(input);
    return Response.json({ messages: [], oldest_loaded_seq: 200, has_more_messages: false });
  }));
  const view = render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()}
      connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>);
  const viewport = view.container.querySelector<HTMLElement>(".chat-messages")!;
  let height = 600;
  Object.defineProperty(viewport, "clientHeight", { configurable: true, get: () => height });
  const first = viewport.querySelector<HTMLElement>("[data-message-key]")!;
  vi.spyOn(first, "getBoundingClientRect").mockImplementation(() =>
    new DOMRect(0, -viewport.scrollTop, 800, 80));
  viewport.scrollTo = (optionsOrX?: ScrollToOptions | number, y?: number) => {
    const top = typeof optionsOrX === "number" ? y ?? 0 : optionsOrX?.top ?? 0;
    viewport.scrollTop = Math.max(0, Math.min(600, top));
  };
  // Complete initial positioning at the real bottom so paging is available.
  for (let frame = 0; frame < 5; frame++) act(() => {
    const callbacks = [...queued.values()]; queued.clear();
    for (const callback of callbacks) callback(performance.now());
  });
  expect(viewport.scrollTop).toBe(600);
  expect(requests).toEqual([]);
  await act(async () => {
    viewport.scrollTop = 0;
    fireEvent.scroll(viewport);
    // Layout disappears after the scroll handler but before passive effects.
    height = 0;
  });
  expect(requests).toEqual([]);
  // A fresh visible top report still permits an ordinary history request.
  await act(async () => {
    height = 600;
    viewport.scrollTop = 100; fireEvent.scroll(viewport);
  });
  await act(async () => {
    viewport.scrollTop = 0; fireEvent.scroll(viewport);
  });
  expect(requests).toHaveLength(1);
});

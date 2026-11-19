import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ChatView } from "./ChatView";
import { mockChatItemLayout, mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type Message, type SessionInfo } from "../stores/sessionStore";
import { applyTranscript } from "../lib/transcript";
import { useLayoutEffect } from "react";
import { handleWsMessage } from "../hooks/useWebSocket";

const session = { id: "position", name: "Position", status: "idle", backend: "codex",
  working_dir: "/tmp", archived: false, created_at: "2026-11-17" } as SessionInfo;
const messages = (first = 1, tail = "Saved row 14"): Message[] => Array.from({ length: 15 }, (_, index) => ({
  role: "assistant", type: "text", seq: index ? index + 9 : first, content: index === 14 ? tail : `Saved row ${index}`,
}));
function install(first = 1, tail?: string) {
  applyTranscript(session.id, { messages: messages(first, tail), oldest_loaded_seq: first, next_message_seq: 24 });
}

beforeEach(() => {
  mockChatViewport(); mockChatItemLayout();
  useSessionStore.getState().setToken("");
  useSessionStore.setState({ activeSessionId: session.id, mainView: "chat", sessions: [session],
    archivedSessions: [], agents: [], credentials: [], backendModels: {}, messages: {},
    messageWindows: {}, oldestSeq: {}, hasMoreMessages: {}, lastAppliedSeq: {},
    streamingText: {}, streamingItems: {}, pendingQuestions: {}, subagents: {}, dispatchStates: {}, dispatchUnavailable: {} });
  install();
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

// Drive the public viewport geometry and frame timing with real Virtuoso
// rendering. The reported extent can lag the actual last row.
function layout() {
  let serial = 0;
  let tailHeight = 80;
  let reportedTailHeight: number | undefined;
  let clampBottom = false;
  let hidden = false;
  let deferredScrollEvents = false;
  let beforeRowsCommit: (() => void) | undefined;
  const frames = new Map<number, FrameRequestCallback>();
  const observations = new Map<ResizeObserver, { callback: ResizeObserverCallback; elements: Map<Element, string | null> }>();
  const rectangle = vi.mocked(HTMLElement.prototype.getBoundingClientRect).getMockImplementation()!;
  const footerHeight = (element: HTMLElement) => element.querySelector("[data-chat-footer]")?.childElementCount ? 120 : 0;
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const viewport = this.closest(".chat-messages");
    const top = 1120 - (viewport?.scrollTop ?? 0);
    if (this.dataset.index === "14") return new DOMRect(0, top, 800, tailHeight);
    if (this.firstElementChild?.hasAttribute("data-chat-footer")) {
      return new DOMRect(0, top + tailHeight, 800, footerHeight(this));
    }
    return rectangle.call(this);
  });
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    const id = ++serial;
    frames.set(id, callback);
    return id;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => { frames.delete(id); });
  vi.stubGlobal("ResizeObserver", class implements ResizeObserver {
    constructor(callback: ResizeObserverCallback) { observations.set(this, { callback, elements: new Map() }); }
    observe(element: Element) {
      observations.get(this)!.elements.set(element, null);
    }
    unobserve(element: Element) { observations.get(this)!.elements.delete(element); }
    disconnect() { observations.get(this)!.elements.clear(); }
  });
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("chat-messages") ? 600 : 0;
  });
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("chat-messages") ? 1120 + (reportedTailHeight ?? tailHeight) + footerHeight(this) : 0;
  });
  function CommitProbe() {
    const rows = useSessionStore((state) => state.messages[session.id]);
    // ChatView has installed its current operation/projection in its layout effects;
    // Virtuoso's published props still owe their nested React row commit.
    useLayoutEffect(() => { beforeRowsCommit?.(); }, [rows]);
    return null;
  }
  const view = render(<><ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()}
    connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} /><CommitProbe /></>);
  const viewport = view.container.querySelector<HTMLElement>(".chat-messages")!;
  const writes: { top: number; hidden: boolean }[] = [];
  viewport.scrollTo = (optionsOrX?: ScrollToOptions | number, y?: number) => {
    const top = typeof optionsOrX === "number" ? y ?? 0 : optionsOrX?.top ?? 0;
    viewport.scrollTop = Math.max(0, Math.min(clampBottom ? 500 : viewport.scrollHeight - 600, top));
    writes.push({ top: viewport.scrollTop, hidden: viewport.style.visibility === "hidden" });
    if (!deferredScrollEvents) fireEvent.scroll(viewport);
  };
  const tick = (deliverResize = true) => act(() => {
    const batch = [...frames.values()]; frames.clear();
    for (const callback of batch) callback(performance.now());
    if (!deliverResize) return;
    for (const [observer, { callback, elements }] of observations) for (const [element, previous] of elements) {
      const signature = Array.from(element.children).map((child) => (child as HTMLElement).dataset.index ?? "").join(",");
      if (signature === previous) continue;
      elements.set(element, signature);
      callback([{ target: element } as ResizeObserverEntry], observer);
    }
  });
  return { viewport, view, writes, tick,
    deferScrollEvents: (defer: boolean) => { deferredScrollEvents = defer; },
    beforeRowsCommit: (callback: () => void) => { beforeRowsCommit = callback; },
    reportExtent: (value?: number) => act(() => {
      reportedTailHeight = value;
      const size = { blockSize: viewport.clientHeight, inlineSize: viewport.clientWidth };
      const entry = { target: viewport, contentRect: viewport.getBoundingClientRect(),
        contentBoxSize: [size], borderBoxSize: [size], devicePixelContentBoxSize: [size] };
      for (const [observer, { callback, elements }] of observations) {
        if (elements.has(viewport)) callback([entry], observer);
      }
    }),
    visibility: (value: boolean) => act(() => { hidden = value; document.dispatchEvent(new Event("visibilitychange")); }),
    clamp: (clamp: boolean) => { clampBottom = clamp; },
    takeFrames: () => { const batch = [...frames.values()]; frames.clear(); return batch; },
    settle: (deliverResize = true) => { for (let i = 0; i < 16; i++) tick(deliverResize); },
    height: (value: number) => { tailHeight = value; } };
}

it("reveals a same-height same-tail window without waiting for a natural resize", () => {
  const browser = layout(); browser.settle();
  expect(browser.viewport).toBeVisible();
  browser.writes.length = 0;
  act(() => install(5));
  expect(browser.viewport, "a real window withdraws its predecessor's reveal before probing").not.toBeVisible();
  browser.settle(false);
  expect(browser.viewport).toBeVisible();
  expect(browser.viewport.scrollTop).toBe(600);
  expect(browser.writes.every((write) => write.hidden)).toBe(true);
});

it("keeps a ready viewport visible and following on a same-window chat pane return", () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
  const browser = layout();
  act(() => vi.advanceTimersByTime(1990));
  browser.settle();
  expect(browser.viewport).toBeVisible();
  expect(browser.viewport.scrollTop).toBe(600);
  act(() => useSessionStore.getState().openManage("schedules"));
  browser.writes.length = 0;
  browser.height(180);
  act(() => useSessionStore.getState().showChat());
  expect(browser.viewport).toBeVisible();
  act(() => vi.advanceTimersByTime(20));
  for (let frame = 0; frame < 16; frame++) {
    browser.tick(false);
    act(() => vi.advanceTimersByTime(20));
  }
  expect(browser.viewport.scrollTop).toBe(700);
  expect(browser.writes.every((write) => !write.hidden)).toBe(true);
  expect(browser.view.queryByText(/Automatic positioning did not finish/)).toBeNull();
});

it.each(["ready", "preparing"])("pages on a single top report after a same-window return only if already %s", async (phase) => {
  useSessionStore.setState({ token: "position-test", connected: true, hasMoreMessages: { [session.id]: true } });
  const reads: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    if (input.includes("before_seq=")) reads.push(new URL(input, "http://localhost").searchParams.get("before_seq")!);
    return Response.json({ messages: [], oldest_loaded_seq: 1, has_more_messages: false });
  }));
  const browser = layout();
  if (phase === "ready") browser.settle();
  expect(browser.viewport.scrollTop).toBe(phase === "ready" ? 600 : 0);
  act(() => useSessionStore.getState().openManage("schedules"));
  act(() => useSessionStore.getState().showChat());
  expect(reads).toEqual([]);
  await act(async () => {
    browser.viewport.scrollTop = 0;
    fireEvent.scroll(browser.viewport);
  });
  expect(reads).toEqual(phase === "ready" ? ["1"] : []);
});

it("preserves a failed paging stop on the same-window return until manual retry or a genuine departure", async () => {
  useSessionStore.setState({ token: "position-test", connected: true, hasMoreMessages: { [session.id]: true } });
  const reads: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    if (input.includes("before_seq=")) reads.push(input);
    return Response.json({ detail: "History temporarily unavailable" }, { status: 503 });
  }));
  const browser = layout();
  browser.settle();
  const scroll = (top: number) => act(async () => {
    browser.viewport.scrollTop = top;
    fireEvent.scroll(browser.viewport);
  });
  await scroll(0);
  expect(reads).toHaveLength(1);
  act(() => useSessionStore.getState().openManage("schedules"));
  act(() => useSessionStore.getState().showChat());
  await scroll(0);
  expect(reads).toHaveLength(1);
  await act(async () => fireEvent.click(browser.view.getByRole("button", { name: "Load earlier messages" })));
  expect(reads).toHaveLength(2);
  browser.height(180);
  await scroll(650);
  await scroll(0);
  expect(reads).toHaveLength(3);
});

it.each(["root", "native"])("reveals the normal same-tail window during zero-publication %s output", (kind) => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
  useSessionStore.getState().updateSessionStatus(session.id, "running");
  const attempt = { request_id: "r", attempt_id: "a" };
  const native = { thread_id: "root", root_thread_id: "root", turn_id: "turn", root_turn_id: "turn", item_id: "item" };
  const runtime_metadata = kind === "native" ? { ...attempt, native } : attempt;
  const send = (content: string) =>
    handleWsMessage({ type: "assistant_delta", session_id: session.id, content, runtime_metadata });
  handleWsMessage({ type: "attempt_started", session_id: session.id, ...attempt });
  send("x".repeat(70 * 1024));
  const browser = layout();
  browser.settle();
  expect(browser.viewport).toBeVisible();
  const shown = browser.view.getByTestId("streaming-text").textContent;
  expect(shown).toMatch(/x{10}…/);
  browser.writes.length = 0;

  act(() => install(5));
  expect(browser.viewport).not.toBeVisible();
  const before = useSessionStore.getState();
  act(() => { for (let index = 0; index < 20; index++) send("y"); });
  expect(useSessionStore.getState()).toBe(before);
  for (let frame = 0; frame < 16 && browser.viewport.style.visibility === "hidden"; frame++) {
    act(() => vi.advanceTimersByTime(16));
    browser.tick(false); // Size notifications stay withheld while normal positioning completes.
  }
  expect(browser.viewport).toBeVisible();
  expect(browser.viewport.scrollTop).toBe(720);
  expect(browser.view.getByTestId("streaming-text").textContent).toBe(shown);
  expect(browser.writes.every((write) => write.hidden)).toBe(true);
  act(() => vi.advanceTimersByTime(2000));
  expect(browser.view.queryByText(/Automatic positioning did not finish/)).toBeNull();
});

it("cannot authorize new same-key same-height text from an old DOM commit", () => {
  const browser = layout(); browser.clamp(true); browser.settle();
  expect(browser.view.getByText("Saved row 14")).toBeInTheDocument();
  const callbacks = browser.takeFrames();
  browser.writes.length = 0;
  act(() => {
    install(5, "New tail text with the same key and height");
    expect(browser.view.queryByText("New tail text with the same key and height")).toBeNull();
    for (const callback of callbacks) callback(performance.now());
    expect(browser.writes).toEqual([]);
  });
  expect(browser.viewport).not.toBeVisible();
  expect(browser.view.getByText("New tail text with the same key and height")).toBeInTheDocument();
  browser.clamp(false); browser.settle();
  expect(browser.viewport).toBeVisible();
  expect(browser.viewport.scrollTop).toBe(600);
});

it.each([1, 5])("checks the current operation against the old row commit before measuring same-key same-height text (first=%s)", (first) => {
  const browser = layout(); browser.clamp(true); browser.settle();
  let checked = false;
  browser.beforeRowsCommit(() => {
    if (checked) return;
    checked = true;
    expect(browser.view.queryByText("New tail text with the same key and height")).toBeNull();
    const oldRow = browser.view.getByText("Saved row 14").closest<HTMLElement>("[data-index]")!;
    const measurement = vi.mocked(HTMLElement.prototype.getBoundingClientRect);
    const measuredBefore = measurement.mock.contexts.length;
    browser.viewport.scrollTop = browser.viewport.scrollHeight - browser.viewport.clientHeight;
    const currentFrames = browser.takeFrames();
    expect(currentFrames.length).toBeGreaterThan(0);
    browser.deferScrollEvents(true);
    for (const frame of currentFrames) frame(performance.now());
    browser.deferScrollEvents(false);
    expect(measurement.mock.contexts.slice(measuredBefore).filter((element) => element === oldRow),
      "old committed text cannot supply the current operation's physical measurement").toEqual([]);
    expect(browser.viewport).not.toBeVisible();
  });
  act(() => install(first, "New tail text with the same key and height"));
  expect(checked).toBe(true);
  expect(browser.view.getByText("New tail text with the same key and height")).toBeInTheDocument();
  browser.clamp(false); browser.settle();
  expect(browser.viewport).toBeVisible();
  expect(browser.viewport.scrollTop).toBe(600);
});

it("waits for a new extent before revealing the latest accepted text", () => {
  const browser = layout(); browser.settle();
  browser.reportExtent(80); browser.height(180);
  act(() => install(5, "Earlier replacement"));
  browser.settle();
  expect(browser.viewport).not.toBeVisible();
  act(() => install(5, "Latest replacement"));
  browser.settle();
  expect(browser.viewport).not.toBeVisible();
  browser.reportExtent(); browser.settle();
  expect(browser.viewport).toBeVisible();
  expect(browser.view.getByText("Latest replacement")).toBeVisible();
  expect(browser.viewport.scrollTop).toBe(700);
  expect(browser.writes.every((write) => write.hidden)).toBe(true);
});

it("accepts late geometry while hidden without revealing or writing until resume", () => {
  const browser = layout(); browser.settle();
  browser.reportExtent(80); browser.height(180);
  act(() => install(5)); browser.settle();
  expect(browser.viewport).not.toBeVisible();
  browser.visibility(true);
  const top = browser.viewport.scrollTop;
  browser.writes.length = 0;
  browser.reportExtent(); browser.settle();
  expect(browser.writes).toEqual([]);
  expect(browser.viewport.scrollTop).toBe(top);
  expect(browser.viewport).not.toBeVisible();
  browser.visibility(false); browser.settle();
  expect(browser.viewport).toBeVisible();
  expect(browser.viewport.scrollTop).toBe(700);
});

it("does not let a pre-pause frame consume the resumed position's frame", () => {
  const browser = layout(); browser.clamp(true); browser.tick();
  const callbacks = browser.takeFrames();
  expect(callbacks.length).toBeGreaterThan(0);
  browser.visibility(true); browser.visibility(false);
  browser.writes.length = 0;
  act(() => { for (const callback of callbacks) callback(performance.now()); });
  expect(browser.writes, "cancelled frames cannot write or displace the current frame").toEqual([]);
  browser.clamp(false); browser.settle();
  expect(browser.viewport).toBeVisible();
  expect(browser.viewport.scrollTop).toBe(600);
});

it.each(["before commit", "after commit"])("retires old measurement frames on A→B→A %s", (delivery) => {
  const browser = layout(); browser.settle();
  browser.clamp(true); browser.height(180);
  act(() => install(5));
  for (let i = 0; i < 3; i++) browser.tick();
  // Queue the opening's own position check through its viewport observer.
  // Virtuoso can finish its measurements within the resize callback.
  browser.reportExtent();
  expect(browser.viewport).not.toBeVisible();
  const callbacks = browser.takeFrames();
  expect(callbacks.length).toBeGreaterThan(0);
  browser.writes.length = 0;
  act(() => {
    useSessionStore.getState().setActiveSessionId("other");
    useSessionStore.getState().setActiveSessionId(session.id);
    if (delivery === "before commit") for (const callback of callbacks) callback(performance.now());
  });
  if (delivery === "after commit") act(() => { for (const callback of callbacks) callback(performance.now()); });
  expect(browser.writes).toEqual([]);
  browser.clamp(false);
  browser.settle();
  const viewport = browser.view.container.querySelector<HTMLElement>(".chat-messages")!;
  expect(viewport).toBeVisible();
});

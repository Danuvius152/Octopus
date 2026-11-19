import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Profiler, type ProfilerOnRenderCallback } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatItemLayout, mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { sessions } from "../lib/sessionList";
import { applyTranscript } from "../lib/transcript";
import { observeSession } from "../lib/hydrateSession";
import { deferred } from "../test-support/deferred";
import { handleWsMessage } from "../hooks/useWebSocket";

beforeEach(() => {
  mockChatViewport();
  useSessionStore.getState().setToken("");
  useSessionStore.setState({ activeSessionId: "cold", mainView: "chat", sessions: [{
    id: "cold", name: "Cold session", status: "running", backend: "codex",
    created_at: "2026-11-16", working_dir: "/tmp", archived: false,
  } as SessionInfo], archivedSessions: [], agents: [], credentials: [], backendModels: {},
    messages: { cold: [{ role: "assistant", type: "text", seq: 50, content: "Live tail without installed history" }] },
    messageWindows: { cold: Symbol("not a baseline") }, oldestSeq: {}, hasMoreMessages: {}, lastAppliedSeq: {},
    streamingText: {}, streamingItems: {}, activeAttempts: {}, subagents: {}, pendingQuestions: {}, dispatchStates: {}, dispatchUnavailable: {},
  });
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("chat-messages") ? 600 : 0;
  });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function chat(onRender?: ProfilerOnRenderCallback) {
  const view = <VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()}
      connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>;
  return render(onRender ? <Profiler id="chat" onRender={onRender}>{view}</Profiler> : view);
}

it("opens trusted content immediately at expiry and retires delayed positioning work", () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
  applyTranscript("cold", { messages: [{ role: "assistant", type: "text", seq: 50, content: "Installed history" }],
    oldest_loaded_seq: 50, next_message_seq: 51 });
  const frames = new Map<number, FrameRequestCallback>();
  let serial = 0;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.set(++serial, callback); return serial; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  const view = chat();
  const viewport = view.container.querySelector<HTMLElement>(".chat-messages")!;
  const status = view.container.querySelector('[role="status"][aria-atomic="true"]');
  const scroll = vi.fn();
  viewport.scrollTo = scroll;
  const late = [...frames.values()];
  screen.getByLabelText("Conversation preparation").focus();
  // No frame or row commit is needed to authorize showing the trusted list.
  act(() => vi.advanceTimersByTime(2000));
  expect(viewport).toBeVisible();
  expect(viewport).not.toHaveAttribute("inert");
  expect(viewport).not.toHaveAttribute("aria-hidden");
  expect(screen.queryByLabelText("Conversation preparation")).toBeNull();
  expect(screen.queryByRole("button", { name: "Retry positioning" })).toBeNull();
  expect(screen.getByText(/Automatic positioning did not finish/)).toBeVisible();
  expect(screen.getByText(/Automatic positioning did not finish/)).toBe(status);
  const announcement = status!.firstChild;
  expect(document.activeElement).toBe(viewport);
  const writes = scroll.mock.calls.length;
  act(() => { for (const callback of late) callback(performance.now()); });
  act(() => vi.advanceTimersByTime(20_000));
  expect(scroll).toHaveBeenCalledTimes(writes);
  expect(status!.firstChild).toBe(announcement);
  expect(viewport).toBeVisible();
});

it.each(["opening", "composer"])("does not let a pending timeout focus handoff override the next %s", (boundary) => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
  applyTranscript("cold", { messages: useSessionStore.getState().messages.cold,
    oldest_loaded_seq: 50, next_message_seq: 51 });
  chat();
  const overlay = screen.getByLabelText("Conversation preparation");
  overlay.focus();
  expect(document.activeElement).toBe(overlay);
  const composer = screen.getByRole("textbox");
  act(() => {
    vi.advanceTimersByTime(2000);
    if (boundary === "opening") {
      const state = useSessionStore.getState();
      state.setActiveSessionId("next");
      useSessionStore.setState({ sessions: [...state.sessions, { ...state.sessions[0], id: "next" }] });
      applyTranscript("next", { messages: [{ role: "assistant", type: "text", seq: 1, content: "Next opening" }],
        oldest_loaded_seq: 1, next_message_seq: 2 });
    }
    if (boundary === "composer") composer.focus();
  });
  if (boundary === "opening") {
    const nextComposer = screen.getByRole("textbox");
    nextComposer.focus();
    fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
    expect(document.activeElement).toBe(nextComposer);
  } else expect(document.activeElement).toBe(composer);
});

function timedOutViewport(onRender?: ProfilerOnRenderCallback) {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
  applyTranscript("cold", { messages: useSessionStore.getState().messages.cold,
    oldest_loaded_seq: 50, next_message_seq: 51 });
  const frames = new Map<number, FrameRequestCallback>();
  let serial = 0, height = 600, extent = 1600, hidden = false;
  const observations = new Map<ResizeObserver, { callback: ResizeObserverCallback; element?: Element }>();
  vi.stubGlobal("ResizeObserver", class implements ResizeObserver {
    constructor(callback: ResizeObserverCallback) { observations.set(this, { callback }); }
    observe(element: Element) { observations.get(this)!.element = element; }
    unobserve(element: Element) {
      const observation = observations.get(this);
      if (observation?.element === element) observation.element = undefined;
    }
    disconnect() { observations.delete(this); }
  });
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.set(++serial, callback); return serial; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  const view = chat(onRender);
  const viewport = view.container.querySelector<HTMLElement>(".chat-messages")!;
  Object.defineProperty(viewport, "scrollHeight", { configurable: true, get: () => extent });
  Object.defineProperty(viewport, "clientHeight", { configurable: true, get: () => height });
  const writes: boolean[] = [];
  viewport.scrollTo = (options) => {
    if (typeof options !== "object") return;
    viewport.scrollTop = Math.max(0, Math.min(extent - height, options.top ?? 0));
    writes.push(viewport.style.visibility === "hidden");
    fireEvent.scroll(viewport);
  };
  act(() => vi.advanceTimersByTime(2000));
  const frame = () => act(() => {
    vi.advanceTimersByTime(20);
    const batch = [...frames.values()]; frames.clear();
    for (const callback of batch) callback(performance.now());
  });
  const settle = () => {
    for (let index = 0; index < 30; index++) frame();
  };
  return { view, viewport, writes, settle, frame,
    grow: (amount: number) => { extent += amount; },
    setHeight: (next: number) => { height = next; },
    notifyViewportResize: () => act(() => {
      const contentRect = new DOMRect(0, 0, viewport.clientWidth, viewport.clientHeight);
      const size = [{ inlineSize: contentRect.width, blockSize: contentRect.height }];
      const entry: ResizeObserverEntry = { target: viewport, contentRect, contentBoxSize: size,
        borderBoxSize: size, devicePixelContentBoxSize: size };
      for (const [observer, observation] of observations) if (observation.element === viewport) {
        observation.callback([entry], observer);
      }
    }),
    pause: (reason: "hidden" | "zero", active: boolean) => act(() => {
      if (reason === "hidden") hidden = active;
      else height = active ? 0 : 600;
      // Existing restoration events recheck both current dimensions and visibility.
      document.dispatchEvent(new Event("visibilitychange"));
    }),
  };
}

it.each(["delta", "complete"])("hands the first accepted %s after timeout to visible follow without reopening initial", (kind) => {
  const browser = timedOutViewport();
  expect(browser.viewport).toBeVisible();
  expect(browser.writes).toEqual([]);
  act(() => {
    if (kind === "delta") useSessionStore.getState().appendStreamingText("cold", "New actual output");
    else useSessionStore.getState().addMessage("cold", { role: "assistant", type: "text", seq: 51, content: "New completed output" });
  });
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
  expect(browser.writes.length).toBeGreaterThan(0);
  expect(browser.writes.every((hidden) => !hidden)).toBe(true);
  expect(screen.queryByText(/Automatic positioning did not finish/)).toBeNull();
  act(() => applyTranscript("cold", { messages: [{ role: "assistant", type: "text", seq: 200, content: "Later history window" }],
    oldest_loaded_seq: 200, next_message_seq: 201 }));
  expect(browser.viewport).toBeVisible();
  expect(screen.queryByRole("button", { name: "Browse loaded content" })).toBeNull();
});

it.each(["hidden", "zero"] as const)("merges arrivals while %s and consumes only on an existing restoration event", (reason) => {
  const browser = timedOutViewport();
  browser.pause(reason, true);
  act(() => {
    for (let index = 0; index < 20; index++) useSessionStore.getState().appendStreamingText("cold", "More actual output");
  });
  browser.settle();
  expect(browser.writes).toEqual([]);
  browser.pause(reason, false);
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
  expect(screen.queryByText(/Automatic positioning did not finish/)).toBeNull();
});

it.each(["on commit", "after resize"])("restores an unanchored reading position when the chat pane is measurable %s", (when) => {
  const browser = hideUnanchoredReading();
  if (when === "on commit") browser.setHeight(600);
  act(() => useSessionStore.getState().showChat());
  if (when === "after resize") {
    expect(browser.viewport.scrollTop).toBe(0);
    browser.setHeight(600);
    fireEvent.scroll(browser.viewport);
    browser.notifyViewportResize();
  }
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(300);
  expect(browser.writes).toEqual([]);
});

function hideUnanchoredReading() {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 300;
  fireEvent.keyDown(browser.viewport, { key: "PageUp" });
  act(() => useSessionStore.getState().openManage("schedules"));
  browser.setHeight(0);
  // A display:none scroller may report zero while its layout box is absent.
  browser.viewport.scrollTop = 0;
  return browser;
}

function deferUnanchoredReadingReturn() {
  const browser = hideUnanchoredReading();
  act(() => useSessionStore.getState().showChat());
  expect(browser.viewport.scrollTop).toBe(0);
  browser.setHeight(600);
  return browser;
}

function observeEarlierPages() {
  const reads: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    if (input.includes("before_seq=")) reads.push(input);
    return Response.json({ messages: [], oldest_loaded_seq: 50, has_more_messages: false });
  }));
  act(() => useSessionStore.setState({ token: "viewport-test", connected: true, hasMoreMessages: { cold: true } }));
  return reads;
}

it("does not page from a temporary top while a reading handoff is pending", async () => {
  const browser = deferUnanchoredReadingReturn();
  browser.setHeight(0);
  const reads = observeEarlierPages();
  browser.setHeight(600);
  const savedRow = screen.getByText("Live tail without installed history").closest<HTMLElement>("[data-message-key]")!;
  vi.spyOn(savedRow, "getBoundingClientRect").mockReturnValue(new DOMRect(0, -100, 800, 80));
  await act(async () => fireEvent.scroll(browser.viewport));
  expect(reads).toEqual([]);
  browser.notifyViewportResize();
  expect(browser.viewport.scrollTop).toBe(300);
  await act(async () => {
    browser.viewport.scrollTop = 0;
    fireEvent.scroll(browser.viewport);
  });
  expect(reads).toEqual([expect.stringContaining("before_seq=50")]);
});

it("does not treat a clamped reading restoration as navigation to the top", async () => {
  const browser = deferUnanchoredReadingReturn();
  browser.grow(-1000);
  const reads = observeEarlierPages();
  browser.notifyViewportResize();
  expect(browser.viewport.scrollTop).toBe(0);
  await act(async () => {});
  expect(reads).toEqual([]);
  browser.grow(1000);
  browser.notifyViewportResize();
  expect(browser.viewport.scrollTop).toBe(300);
  await act(async () => {
    browser.viewport.scrollTop = 0;
    fireEvent.scroll(browser.viewport);
  });
  expect(reads).toEqual([expect.stringContaining("before_seq=50")]);
});

it("does not discard a stationary scrollbar handoff when the returning layout initially has no extent", () => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 300;
  fireEvent.keyDown(browser.viewport, { key: "PageUp" });
  scrollbarPointer(browser.viewport, "pointerdown");
  act(() => useSessionStore.getState().openManage("schedules"));
  browser.setHeight(0);
  browser.viewport.scrollTop = 0;
  act(() => useSessionStore.getState().showChat());
  browser.grow(-1000);
  browser.setHeight(600);
  scrollbarPointer(browser.viewport, "pointerup");
  expect(browser.viewport.scrollTop).toBe(0);
  browser.grow(1000);
  browser.notifyViewportResize();
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(300);
});

it.each([1, 4])("does not page when an application scroll notification in a %spx range arrives after expanding a tool", async (range) => {
  useSessionStore.setState({ messages: { cold: [
    { role: "assistant", type: "tool_use", seq: 50, tool_name: "Bash", tool_input: { command: "pwd" } },
  ] } });
  const browser = deferUnanchoredReadingReturn();
  browser.grow(range - 1000);
  const reads = observeEarlierPages();
  browser.notifyViewportResize();
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(range);
  const tool = screen.getByRole("button", { name: /Bash/ });
  fireEvent.click(tool);
  expect(tool).toHaveAttribute("aria-expanded", "true");
  await act(async () => fireEvent.scroll(browser.viewport));
  expect(reads).toEqual([]);
  await act(async () => {
    browser.viewport.scrollTop = 0;
    fireEvent.scroll(browser.viewport);
  });
  expect(reads).toEqual([expect.stringContaining("before_seq=50")]);
});

it("settles an actually short list without restoring an old offset on later output", async () => {
  const browser = deferUnanchoredReadingReturn();
  browser.grow(-1000);
  const reads = observeEarlierPages();
  browser.notifyViewportResize();
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(0);
  await act(async () => fireEvent.scroll(browser.viewport));
  expect(reads).toEqual([]);
  browser.grow(1000);
  browser.notifyViewportResize();
  expect(browser.viewport.scrollTop).toBe(0);
});

it("uses the only top report when a late scrollbar scroll supersedes the restored anchor", async () => {
  const browser = timedOutViewport();
  const reads = observeEarlierPages();
  browser.viewport.scrollTop = 200;
  const moveAnchor = moveReadingAnchor(browser.viewport);
  fireEvent.keyDown(browser.viewport, { key: "PageUp" });
  scrollbarPointer(browser.viewport, "pointerdown");
  act(() => useSessionStore.getState().openManage("schedules"));
  browser.setHeight(0);
  act(() => useSessionStore.getState().showChat());
  moveAnchor(80);
  browser.setHeight(600);
  scrollbarPointer(browser.viewport, "pointerup");
  await act(async () => {
    browser.viewport.scrollTop = 0;
    fireEvent.scroll(browser.viewport);
  });
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(0);
  expect(reads).toEqual([expect.stringContaining("before_seq=50")]);
});

it.each(["wheel", "scrollbar"])("accepts a native %s top report after reading intent captures the new position", async (source) => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  fireEvent.keyDown(browser.viewport, { key: "PageUp" });
  fireEvent.scroll(browser.viewport);
  const reads = observeEarlierPages();
  if (source === "scrollbar") scrollbarPointer(browser.viewport, "pointerdown");
  browser.viewport.scrollTop = 0;
  if (source === "wheel") fireEvent.wheel(browser.viewport, { deltaY: -200 });
  else scrollbarPointer(browser.viewport, "pointerup", 350);
  await act(async () => fireEvent.scroll(browser.viewport));
  expect(reads).toEqual([expect.stringContaining("before_seq=50")]);
});

it.each(["before the frame", "after the frame"])("yields a short-range restoration to native motion notified %s", async (when) => {
  const browser = deferUnanchoredReadingReturn();
  browser.grow(-900);
  const reads = observeEarlierPages();
  browser.notifyViewportResize();
  browser.viewport.scrollTop = 100;
  scrollbarPointer(browser.viewport, "pointerdown");
  scrollbarPointer(browser.viewport, "pointerup");
  browser.viewport.scrollTop = 0;
  if (when === "before the frame") await act(async () => fireEvent.scroll(browser.viewport));
  browser.frame();
  expect(browser.viewport.scrollTop).toBe(0);
  if (when === "after the frame") await act(async () => fireEvent.scroll(browser.viewport));
  expect(reads).toEqual([expect.stringContaining("before_seq=50")]);
});

it("consumes the first page's top report and accepts a new one after correction", async () => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  const moveAnchor = moveReadingAnchor(browser.viewport);
  fireEvent.keyDown(browser.viewport, { key: "PageUp" });
  const first = deferred<Response>();
  const reads: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    if (!input.includes("before_seq=")) return Response.json({});
    reads.push(input);
    return reads.length === 1 ? first.promise : Response.json({
      messages: [], oldest_loaded_seq: 49, has_more_messages: false,
    });
  }));
  act(() => useSessionStore.setState({ token: "viewport-test", connected: true, hasMoreMessages: { cold: true } }));
  await act(async () => {
    browser.viewport.scrollTop = 0;
    fireEvent.scroll(browser.viewport);
  });
  expect(reads).toEqual([expect.stringContaining("before_seq=50")]);
  await act(async () => first.resolve(Response.json({
    messages: [{ role: "assistant", type: "text", seq: 49, content: "Earlier" }], oldest_loaded_seq: 49, has_more_messages: true,
  })));
  moveAnchor(80);
  browser.frame();
  expect(browser.viewport.scrollTop).toBe(80);
  fireEvent.scroll(browser.viewport);
  browser.settle();
  expect(reads).toHaveLength(1);
  await act(async () => {
    browser.viewport.scrollTop = 0;
    fireEvent.scroll(browser.viewport);
  });
  expect(reads).toEqual([expect.stringContaining("before_seq=50"), expect.stringContaining("before_seq=49")]);
});

it.each([
  ["touch", false], ["touch", true], ["pen", false], ["pen", true],
] as const)("waits for %s release before restoring an unanchored read (movement=%s)", (source, moved) => {
  const browser = deferUnanchoredReadingReturn();
  const contact = { identifier: 7, clientY: 400 };
  if (source === "touch") fireEvent.touchStart(browser.viewport, { touches: [contact] });
  else chatPointer(browser.viewport, "pointerdown", source, contact.clientY);
  browser.notifyViewportResize();
  expect(browser.viewport.scrollTop).toBe(0);
  if (moved) {
    if (source === "touch") fireEvent.touchMove(browser.viewport, { touches: [{ ...contact, clientY: 350 }] });
    else chatPointer(browser.viewport, "pointermove", source, 350);
    browser.viewport.scrollTop = 100;
    fireEvent.scroll(browser.viewport);
  }
  if (source === "touch") fireEvent.touchEnd(document.body, { touches: [], changedTouches: [contact] });
  else chatPointer(browser.viewport, "pointerup", source, moved ? 350 : 400);
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(moved ? 100 : 300);
  expect(browser.writes).toEqual([]);
});

it.each(["leaves again", "replaces the window"])("preserves the original reading position when chat %s before measurement", (next) => {
  const browser = deferUnanchoredReadingReturn();
  if (next === "leaves again") {
    act(() => useSessionStore.getState().openManage("schedules"));
    act(() => useSessionStore.getState().showChat());
  } else {
    act(() => replaceReadingWindow());
  }
  browser.notifyViewportResize();
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(300);
  expect(browser.writes).toEqual([]);
});

it.each([
  ["resize first", false], ["resize first", true], ["release first", false], ["release first", true],
] as const)("settles delayed unanchored reading with %s and scrollbar movement=%s", (order, moved) => {
  const browser = deferUnanchoredReadingReturn();
  scrollbarPointer(browser.viewport, "pointerdown");
  if (moved) browser.viewport.scrollTop = 100;
  if (order === "resize first") {
    browser.notifyViewportResize();
    expect(browser.viewport.scrollTop).toBe(moved ? 100 : 0);
  }
  scrollbarPointer(browser.viewport, "pointerup");
  browser.notifyViewportResize();
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(moved ? 100 : 300);
  expect(browser.writes).toEqual([]);
});

it("consumes a saturated delta on a batched chat pane return without a layout notification", () => {
  const send = (content: string) => handleWsMessage({ type: "assistant_delta", session_id: "cold", content });
  send("x".repeat(70 * 1024));
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  act(() => {
    useSessionStore.getState().openManage("schedules");
    const hidden = useSessionStore.getState();
    send("y");
    expect(useSessionStore.getState()).toBe(hidden);
    expect(browser.writes).toEqual([]);
    useSessionStore.getState().showChat();
  });
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
  expect(screen.queryByText(/Automatic positioning did not finish/)).toBeNull();
});

it.each(["root", "native"].flatMap((kind) => [false, true].map((reader) => ({ kind, reader }))))(
  "settles the timeout on a zero-publication $kind delta (reader=$reader)", ({ kind, reader }) => {
    const attempt = { request_id: "request", attempt_id: "attempt" };
    const runtime_metadata = kind === "root" ? attempt : { ...attempt, native: {
      thread_id: "root", root_thread_id: "root", turn_id: "turn", root_turn_id: "turn", item_id: "item",
    } };
    handleWsMessage({ type: "attempt_started", session_id: "cold", ...attempt });
    const send = (content: string) => handleWsMessage({ type: "assistant_delta", session_id: "cold", content, runtime_metadata });
    send("x".repeat(70 * 1024));
    const browser = timedOutViewport();
    const saturated = useSessionStore.getState();
    browser.viewport.scrollTop = 200;
    if (reader) fireEvent.keyDown(browser.viewport, { key: "PageUp" });
    browser.pause("hidden", true);
    act(() => {
      for (let index = 0; index < 20; index++) send("y");
    });
    expect(useSessionStore.getState()).toBe(saturated);
    browser.settle();
    expect(browser.writes).toEqual([]);
    browser.pause("hidden", false);
    browser.settle();
    expect(browser.viewport.scrollTop).toBe(reader ? 200 : 1000);
    expect(browser.writes.some((hidden) => !hidden)).toBe(!reader);
    expect(browser.writes).not.toContain(true);
    const writes = browser.writes.length;
    act(() => send("after settlement"));
    browser.settle();
    expect(browser.writes).toHaveLength(writes);
    expect(browser.viewport).toBeVisible();
  },
);

it.each(["wheel first", "arrival first"])("lets active reading retire arrival recovery before any frame: %s", (order) => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  if (order === "arrival first") act(() => useSessionStore.getState().appendStreamingText("cold", "Before reading input"));
  fireEvent.wheel(browser.viewport, { deltaY: -120 });
  act(() => useSessionStore.getState().appendStreamingText("cold", "After reading input"));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(200);
  expect(browser.writes).toEqual([]);
  expect(screen.queryByText(/New output will return/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Jump to latest" }));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
  expect(browser.writes.every((hidden) => !hidden)).toBe(true);
});

it.each(["arrival first", "contact first"])("preserves one recovery across an unmoved touch contact: %s", (order) => {
  const browser = timedOutViewport();
  const touch = { identifier: 7, clientY: 400 };
  if (order === "arrival first") act(() => useSessionStore.getState().appendStreamingText("cold", "Before contact"));
  fireEvent.touchStart(browser.viewport, { touches: [touch] });
  act(() => useSessionStore.getState().appendStreamingText("cold", "During contact"));
  browser.settle();
  expect(browser.writes).toEqual([]);
  fireEvent.touchEnd(document.body, { touches: [], changedTouches: [touch] });
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
});

it("dismisses only the timeout hint and keeps the one accepted-text recovery", () => {
  const browser = timedOutViewport();
  fireEvent.click(screen.getByRole("button", { name: "Dismiss positioning notice" }));
  expect(screen.queryByText(/Automatic positioning did not finish/)).toBeNull();
  expect(screen.getByRole("button", { name: "Jump to latest" })).toBeVisible();
  act(() => useSessionStore.getState().appendStreamingText("cold", "After dismissing the notice"));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
});

it("reuses the reading presentation on repeated wheel and scroll reports", () => {
  const commits = vi.fn();
  const browser = timedOutViewport(commits);
  browser.viewport.scrollTop = 300;
  const replayWheel = () => {
    fireEvent.wheel(browser.viewport, { deltaY: -120 });
    fireEvent.scroll(browser.viewport);
  };
  expect(screen.getByText(/New output will return/)).toBeVisible();
  replayWheel();
  expect(screen.queryByText(/New output will return/)).toBeNull();
  commits.mockClear();

  for (let index = 0; index < 8; index++) replayWheel();
  expect(commits).toHaveBeenCalledTimes(0);
  expect(screen.getByText(/Automatic positioning did not finish/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Dismiss positioning notice" }));
  expect(screen.queryByText(/Automatic positioning did not finish/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Jump to latest" }));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
  expect(screen.queryByRole("button", { name: "Jump to latest" })).toBeNull();
  fireEvent.wheel(browser.viewport, { deltaY: 120 });
  expect(screen.queryByRole("button", { name: "Jump to latest" })).toBeNull();
});

function chatPointer(viewport: HTMLElement, type: "pointerdown" | "pointermove" | "pointerup" | "pointercancel", pointerType: "mouse" | "touch" | "pen", clientY = 400) {
  const mouse = pointerType === "mouse";
  const event = new MouseEvent(type, { bubbles: true, button: 0, clientX: mouse ? 801 : 300, clientY });
  Object.defineProperties(event, { pointerId: { value: mouse ? 1 : 2 }, pointerType: { value: pointerType }, isPrimary: { value: true } });
  fireEvent(viewport, event);
}

function scrollbarPointer(viewport: HTMLElement, type: "pointerdown" | "pointerup", clientY = 400) {
  chatPointer(viewport, type, "mouse", clientY);
}

it.each(["while held", "after release", "while zero height"] as const)("keeps reading when native scrollbar navigation is uncertain and text arrives %s", (arrival) => {
  const browser = timedOutViewport();
  const append = () => act(() => useSessionStore.getState().appendStreamingText("cold", "Accepted output"));
  browser.viewport.scrollTop = 200;
  scrollbarPointer(browser.viewport, "pointerdown");
  if (arrival === "while zero height") browser.pause("zero", true);
  if (arrival !== "after release") append();
  browser.settle();
  expect(browser.writes).toEqual([]);
  scrollbarPointer(browser.viewport, "pointerup");
  if (arrival === "while zero height") browser.pause("zero", false);
  if (arrival === "after release") append();
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(200);
  expect(browser.writes).toEqual([]);
  expect(screen.getByText("Accepted output")).toBeVisible();
  expect(screen.queryByText(/New output will return/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Jump to latest" }));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
});

it.each(["scroll before release", "offset before release", "scroll after release"])("preserves same-y track navigation with %s", (delivery) => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  scrollbarPointer(browser.viewport, "pointerdown");
  if (delivery !== "scroll after release") browser.viewport.scrollTop = 100;
  if (delivery === "scroll before release") fireEvent.scroll(browser.viewport);
  act(() => useSessionStore.getState().appendStreamingText("cold", "While clicking the track"));
  scrollbarPointer(browser.viewport, "pointerup");
  if (delivery === "scroll after release") {
    browser.viewport.scrollTop = 100;
    fireEvent.scroll(browser.viewport);
  }
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(100);
  expect(browser.writes).toEqual([]);
  expect(screen.queryByText(/New output will return/)).toBeNull();
});

it("keeps the compensated position when scrollbar navigation cannot be ruled out", () => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  scrollbarPointer(browser.viewport, "pointerdown");
  browser.grow(80);
  browser.viewport.scrollTop += 80;
  fireEvent.scroll(browser.viewport);
  act(() => useSessionStore.getState().appendStreamingText("cold", "While content shifts above"));
  browser.settle();
  expect(browser.writes).toEqual([]);
  scrollbarPointer(browser.viewport, "pointerup");
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(280);
  expect(browser.writes).toEqual([]);
  expect(screen.queryByText(/New output will return/)).toBeNull();
});

it("cancels the first queued timeout return when a native scrollbar contact intervenes", () => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  act(() => useSessionStore.getState().appendStreamingText("cold", "Output before scrollbar contact"));
  scrollbarPointer(browser.viewport, "pointerdown");
  scrollbarPointer(browser.viewport, "pointerup");
  browser.viewport.scrollTop = 100;
  fireEvent.scroll(browser.viewport);
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(100);
  expect(browser.writes).toEqual([]);
});

it.each(["Page Up", "wheel up"])("keeps the reading decision when %s interrupts first follow during scrollbar contact", (input) => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  act(() => useSessionStore.getState().appendStreamingText("cold", "First output"));
  scrollbarPointer(browser.viewport, "pointerdown");
  if (input === "Page Up") fireEvent.keyDown(browser.viewport, { key: "PageUp" });
  else fireEvent.wheel(browser.viewport, { deltaY: -100 });
  scrollbarPointer(browser.viewport, "pointerup");
  act(() => useSessionStore.getState().appendStreamingText("cold", " and another output"));
  browser.settle();
  expect(screen.getByText("First output and another output")).toBeVisible();
  expect(browser.viewport.scrollTop).toBe(200);
  expect(browser.writes).toEqual([]);
});

it.each([
  ["mouse", "mouse"], ["mouse", "touch"], ["touch", "mouse"], ["touch", "touch"],
] as const)("keeps mixed input with %s down first and %s released last", (first, last) => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  const touch = { identifier: 7, clientY: 400 };
  const touchStart = () => {
    chatPointer(browser.viewport, "pointerdown", "touch");
    fireEvent.touchStart(browser.viewport, { touches: [touch] });
  };
  const touchEnd = () => {
    fireEvent.touchEnd(document.body, { touches: [], changedTouches: [touch] });
    chatPointer(browser.viewport, "pointerup", "touch");
  };
  if (first === "touch") touchStart();
  scrollbarPointer(browser.viewport, "pointerdown");
  if (first === "mouse") touchStart();
  act(() => useSessionStore.getState().appendStreamingText("cold", "Output during mixed input"));
  if (last === "mouse") touchEnd();
  else {
    // Native panning can cancel the pointer while its touch is still down.
    chatPointer(browser.viewport, "pointercancel", "touch");
    scrollbarPointer(browser.viewport, "pointerup");
  }
  browser.settle();
  expect(browser.writes).toEqual([]);
  expect(screen.getByText(/New output will return/)).toBeVisible();
  if (last === "mouse") scrollbarPointer(browser.viewport, "pointerup");
  else touchEnd();
  browser.settle();
  expect(screen.queryByText(/New output will return/)).toBeNull();
  expect(screen.getByText("Output during mixed input")).toBeVisible();
  expect(browser.viewport.scrollTop).toBe(200);
  expect(browser.writes).toEqual([]);
});

function replaceReadingWindow(seq = 49) {
  applyTranscript("cold", {
    messages: [{ role: "assistant", type: "text", seq, content: "New earlier row" },
      ...useSessionStore.getState().messages.cold],
    oldest_loaded_seq: seq, next_message_seq: 51,
  });
}

function moveReadingAnchor(viewport: HTMLElement) {
  const rowKey = screen.getByText("Live tail without installed history").closest<HTMLElement>("[data-message-key]")!.dataset.messageKey;
  let rowTop = 210;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this === viewport) return new DOMRect(0, 0, 800, 600);
    if (this.dataset.messageKey === rowKey) return new DOMRect(0, rowTop - viewport.scrollTop, 800, 80);
    return new DOMRect();
  });
  return (amount: number) => { rowTop += amount; };
}

it("verifies a remounted history anchor again even when the list height is unchanged", () => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  const moveAnchor = moveReadingAnchor(browser.viewport);
  fireEvent.keyDown(browser.viewport, { key: "PageUp" });
  act(replaceReadingWindow);
  browser.frame();
  const row = screen.getByText("Live tail without installed history").closest<HTMLElement>("[data-message-key]")!;
  const key = row.dataset.messageKey!;
  // Hold the row identity out of the committed range for one positioning frame.
  delete row.dataset.messageKey;
  browser.frame();
  row.dataset.messageKey = key;
  browser.viewport.scrollTop = 200;
  browser.notifyViewportResize();
  browser.frame();
  moveAnchor(20);
  browser.notifyViewportResize();
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(220);
});

it.each([
  [false, false], [true, false], [false, true], [true, true],
])("retains reading across window publication (visible anchor=%s, first follow granted=%s)", (anchored, granted) => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  const moveAnchor = anchored ? moveReadingAnchor(browser.viewport) : undefined;
  if (granted) act(() => useSessionStore.getState().appendStreamingText("cold", "First output before window change"));
  scrollbarPointer(browser.viewport, "pointerdown");
  act(() => {
    replaceReadingWindow();
    scrollbarPointer(browser.viewport, "pointerup");
    moveAnchor?.(80);
  });
  browser.settle();
  expect(screen.getByText("New earlier row")).toBeVisible();
  expect(screen.queryByText(/New output will return/)).toBeNull();
  expect(browser.viewport.scrollTop).toBe(anchored ? 280 : 200);
  expect(browser.writes).toEqual([]);
});

it.each(["touch", "pen"] as const)("keeps downward %s reading across a window change without reviving follow", (source) => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  const moveAnchor = moveReadingAnchor(browser.viewport);
  fireEvent.keyDown(browser.viewport, { key: "PageUp" });
  const touch = { identifier: 7, clientY: 400 };
  if (source === "touch") fireEvent.touchStart(browser.viewport, { touches: [touch] });
  else chatPointer(browser.viewport, "pointerdown", source, touch.clientY);
  act(replaceReadingWindow);
  moveAnchor(80);
  if (source === "touch") fireEvent.touchMove(browser.viewport, { touches: [{ ...touch, clientY: 350 }] });
  else chatPointer(browser.viewport, "pointermove", source, 350);
  browser.viewport.scrollTop = 1000;
  fireEvent.scroll(browser.viewport);
  if (source === "touch") fireEvent.touchEnd(document.body, { touches: [], changedTouches: [touch] });
  else chatPointer(browser.viewport, "pointerup", source, 350);
  browser.grow(100);
  act(() => useSessionStore.getState().appendStreamingText("cold", "Output after window change"));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
  expect(browser.writes).toEqual([]);
});

it.each([
  ["before commit", false, false], ["before commit", true, false], ["before commit", true, true],
  ["after commit", false, false], ["after commit", true, false], ["after commit", true, true],
] as const)("hands off a held scrollbar released %s (late movement=%s, scroll event=%s)", (release, moved, notified) => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  const moveAnchor = moveReadingAnchor(browser.viewport);
  act(() => useSessionStore.getState().appendStreamingText("cold", "First output before window change"));
  scrollbarPointer(browser.viewport, "pointerdown");
  act(() => {
    replaceReadingWindow();
    moveAnchor(80);
    if (release === "before commit") scrollbarPointer(browser.viewport, "pointerup");
  });
  if (release === "after commit") {
    browser.settle();
    expect(browser.viewport.scrollTop).toBe(200);
    scrollbarPointer(browser.viewport, "pointerup");
  }
  if (moved) {
    browser.viewport.scrollTop = 100;
    if (notified) fireEvent.scroll(browser.viewport);
  }
  browser.settle();
  expect(screen.getByText("New earlier row")).toBeVisible();
  expect(browser.viewport.scrollTop).toBe(moved ? 100 : 280);
  expect(browser.writes).toEqual([]);
});

it("restores a held scrollbar's reading anchor when its replacement becomes measurable", () => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  const moveAnchor = moveReadingAnchor(browser.viewport);
  act(() => useSessionStore.getState().appendStreamingText("cold", "First output before window change"));
  scrollbarPointer(browser.viewport, "pointerdown");
  act(replaceReadingWindow);
  moveAnchor(80);
  browser.pause("zero", true);
  scrollbarPointer(browser.viewport, "pointerup");
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(200);
  browser.pause("zero", false);
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(280);
  expect(browser.writes).toEqual([]);
});

it.each([
  ["native contact", "stationary"], ["native contact", "scroll event"], ["native contact", "before scroll event"],
  ["PageUp", "stationary"], ["PageUp", "scroll event"], ["PageUp", "before scroll event"],
])("hands off reading twice after %s with native input %s", (first, delivery) => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  const moveAnchor = moveReadingAnchor(browser.viewport);
  if (first === "native contact") {
    act(() => useSessionStore.getState().appendStreamingText("cold", "First output before window change"));
    scrollbarPointer(browser.viewport, "pointerdown");
  } else fireEvent.keyDown(browser.viewport, { key: "PageUp" });
  act(replaceReadingWindow);
  moveAnchor(80);
  if (first === "native contact") scrollbarPointer(browser.viewport, "pointerup");
  expect(browser.viewport.scrollTop).toBe(200);

  // Another track click may already have moved the reader before the new
  // window retires the first window's still-pending correction.
  scrollbarPointer(browser.viewport, "pointerdown");
  if (delivery !== "stationary") browser.viewport.scrollTop = 100;
  if (delivery === "scroll event") fireEvent.scroll(browser.viewport);
  act(() => replaceReadingWindow(48));
  scrollbarPointer(browser.viewport, "pointerup");
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(delivery === "stationary" ? 280 : 100);
  expect(browser.writes).toEqual([]);
});

it("does not restore a pagination anchor captured during native contact after the reader moves", async () => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  const moveAnchor = moveReadingAnchor(browser.viewport);
  const page = deferred<Record<string, unknown>>();
  const fetch = vi.fn(async () => ({ ok: true, json: () => page.promise }));
  vi.stubGlobal("fetch", fetch);
  const sessionRows = useSessionStore.getState().sessions;
  act(() => {
    useSessionStore.setState({ token: "viewport-test", connected: true, hasMoreMessages: { cold: true } });
    useSessionStore.setState({ sessions: sessionRows });
  });
  fireEvent.click(screen.getByRole("button", { name: "Load earlier messages" }));
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/messages?before_seq=50"), expect.anything());
  scrollbarPointer(browser.viewport, "pointerdown");
  await act(async () => page.resolve({ messages: [{ role: "assistant", type: "text", seq: 49, content: "Earlier page" }],
    oldest_loaded_seq: 49, has_more_messages: false }));
  expect(useSessionStore.getState().messages.cold.map((message) => message.seq)).toEqual([49, 50]);
  moveAnchor(80);
  browser.viewport.scrollTop = 100;
  act(() => replaceReadingWindow(48));
  scrollbarPointer(browser.viewport, "pointerup");
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(100);
  expect(browser.writes).toEqual([]);
});

it("retires ordinary reading correction before a late same-coordinate native scroll", () => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  const moveAnchor = moveReadingAnchor(browser.viewport);
  fireEvent.keyDown(browser.viewport, { key: "PageUp" });
  act(replaceReadingWindow);
  moveAnchor(80);
  scrollbarPointer(browser.viewport, "pointerdown");
  scrollbarPointer(browser.viewport, "pointerup");
  browser.viewport.scrollTop = 100;
  fireEvent.scroll(browser.viewport);
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(100);
  expect(browser.writes).toEqual([]);
  fireEvent.click(screen.getByRole("button", { name: "Jump to latest" }));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
});

it("preserves newer downward input through a stationary native contact and late scroll", () => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 900;
  fireEvent.keyDown(browser.viewport, { key: "PageUp" });
  fireEvent.scroll(browser.viewport);
  scrollbarPointer(browser.viewport, "pointerdown");
  fireEvent.wheel(browser.viewport, { deltaY: 80 });
  act(() => vi.advanceTimersByTime(600));
  scrollbarPointer(browser.viewport, "pointerup");
  browser.viewport.scrollTop = 1000;
  fireEvent.scroll(browser.viewport);
  browser.grow(80);
  act(() => useSessionStore.getState().appendStreamingText("cold", "Output after native return"));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1080);
});

it.each(["pointerup", "pointercancel"] as const)("preserves a downward wheel in the new window after old scrollbar %s", (end) => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 900;
  fireEvent.scroll(browser.viewport);
  scrollbarPointer(browser.viewport, "pointerdown");
  act(replaceReadingWindow);
  fireEvent.wheel(browser.viewport, { deltaY: 80 });
  chatPointer(browser.viewport, end, "mouse");
  browser.viewport.scrollTop = 1000;
  fireEvent.scroll(browser.viewport);
  browser.grow(80);
  act(() => useSessionStore.getState().appendStreamingText("cold", "Output after the new window's downward wheel"));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1080);
});

it.each(["pointerup", "pointercancel"] as const)("only finishes a pointer's own downward navigation on %s", (end) => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 900;
  fireEvent.scroll(browser.viewport);
  chatPointer(browser.viewport, "pointerdown", "touch");
  chatPointer(browser.viewport, "pointermove", "touch", 300);
  chatPointer(browser.viewport, end, "touch", 300);
  browser.viewport.scrollTop = 1000;
  fireEvent.scroll(browser.viewport);
  browser.grow(80);
  act(() => useSessionStore.getState().appendStreamingText("cold", "Output after a pointer's own downward motion"));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(end === "pointerup" ? 1080 : 1000);
});

it("honors an explicit Jump requested while a scrollbar contact is still held", () => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  scrollbarPointer(browser.viewport, "pointerdown");
  fireEvent.click(screen.getByRole("button", { name: "Jump to latest" }));
  browser.settle();
  expect(browser.writes).toEqual([]);
  scrollbarPointer(browser.viewport, "pointerup");
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
});

it("keeps first automatic follow paused after a quarantined window until a trusted repair is committed", () => {
  const browser = timedOutViewport();
  browser.viewport.scrollTop = 200;
  act(() => useSessionStore.getState().appendStreamingText("cold", "First output before damage"));
  act(() => { expect(applyTranscript("cold", { transcript_integrity: "duplicate_seq" })).toBe(false); });
  browser.settle();
  expect(browser.writes).toEqual([]);
  expect(browser.viewport.scrollTop).toBe(200);
  act(() => {
    expect(applyTranscript("cold", {
      messages: [{ role: "assistant", type: "text", seq: 51, content: "Trusted repair" }],
      oldest_loaded_seq: 51, next_message_seq: 52,
    })).toBe(true);
    sessions.patch("cold", { transcript_integrity: "ok" });
  });
  browser.settle();
  expect(useSessionStore.getState().messages.cold[0].content).toBe("Trusted repair");
  expect(browser.viewport).toBeVisible();
  expect(browser.viewport.scrollTop).toBe(1000);
});

it("restores active following after a stationary scrollbar contact once timeout recovery has reached the bottom", () => {
  const browser = timedOutViewport();
  act(() => useSessionStore.getState().appendStreamingText("cold", "Initial recovery output"));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
  scrollbarPointer(browser.viewport, "pointerdown");
  browser.grow(80);
  act(() => useSessionStore.getState().appendStreamingText("cold", "Later output while held"));
  scrollbarPointer(browser.viewport, "pointerup");
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1080);
});

it.each(["replacement", "mixed inputs"])("respects a scrollbar drag without pointermove during %s after ordinary follow has resumed", (boundary) => {
  const browser = timedOutViewport();
  act(() => useSessionStore.getState().appendStreamingText("cold", "Initial recovery output"));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
  const initialWrites = browser.writes.length;
  scrollbarPointer(browser.viewport, "pointerdown");
  const touch = { identifier: 7, clientY: 520 };
  if (boundary === "replacement") act(replaceReadingWindow);
  else {
    chatPointer(browser.viewport, "pointerdown", "touch", touch.clientY);
    fireEvent.touchStart(browser.viewport, { touches: [touch] });
  }
  browser.viewport.scrollTop = 700;
  fireEvent.scroll(browser.viewport);
  scrollbarPointer(browser.viewport, "pointerup", 320);
  if (boundary === "mixed inputs") {
    fireEvent.touchEnd(document.body, { touches: [], changedTouches: [touch] });
    chatPointer(browser.viewport, "pointerup", "touch", touch.clientY);
  }
  browser.grow(80);
  act(() => useSessionStore.getState().appendStreamingText("cold", "Output after drag"));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(700);
  expect(browser.writes).toHaveLength(initialWrites);
});

it("resumes ordinary following when only the secondary touch release has a different coordinate", () => {
  const browser = timedOutViewport();
  act(() => useSessionStore.getState().appendStreamingText("cold", "Initial recovery output"));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
  const touch = { identifier: 1, clientY: 520 };
  scrollbarPointer(browser.viewport, "pointerdown");
  chatPointer(browser.viewport, "pointerdown", "touch", touch.clientY);
  fireEvent.touchStart(browser.viewport, { touches: [touch] });
  scrollbarPointer(browser.viewport, "pointerup");
  fireEvent.touchEnd(document.body, { touches: [], changedTouches: [touch] });
  chatPointer(browser.viewport, "pointerup", "touch", touch.clientY);
  browser.grow(80);
  act(() => useSessionStore.getState().appendStreamingText("cold", "Output after stationary contact"));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1080);
});

it.each(["mouse", "touch"])("keeps real touch reading with a stationary scrollbar when %s releases last", (last) => {
  const browser = timedOutViewport();
  act(() => useSessionStore.getState().appendStreamingText("cold", "Initial recovery output"));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
  const initialWrites = browser.writes.length;
  const touch = { identifier: 7, clientY: 400 };
  const moved = { ...touch, clientY: 520 };
  scrollbarPointer(browser.viewport, "pointerdown");
  chatPointer(browser.viewport, "pointerdown", "touch", touch.clientY);
  fireEvent.touchStart(browser.viewport, { touches: [touch] });
  fireEvent.touchMove(browser.viewport, { touches: [moved] });
  browser.viewport.scrollTop = 700;
  fireEvent.scroll(browser.viewport);
  const endMouse = () => scrollbarPointer(browser.viewport, "pointerup");
  const endTouch = () => {
    fireEvent.touchEnd(document.body, { touches: [], changedTouches: [moved] });
    chatPointer(browser.viewport, "pointerup", "touch", moved.clientY);
  };
  if (last === "mouse") { endTouch(); endMouse(); }
  else { endMouse(); endTouch(); }
  browser.grow(80);
  act(() => useSessionStore.getState().appendStreamingText("cold", "Output during touch reading"));
  browser.settle();
  expect(screen.getByText(/Output during touch reading/)).toBeVisible();
  expect(browser.viewport.scrollTop).toBe(700);
  expect(browser.writes).toHaveLength(initialWrites);
});

it.each(["empty", "user", "notice", "snapshot", "layout", "replay", "wrong attempt", "terminal"])("does not authorize recovery from %s publication or observation", (signal) => {
  const browser = timedOutViewport();
  const store = useSessionStore.getState();
  act(() => {
    if (signal === "empty") store.appendStreamingText("cold", "");
    if (signal === "user") store.addMessage("cold", { role: "user", type: "text", seq: 51, content: "User text" });
    if (signal === "notice") store.addMessage("cold", { role: "system", type: "notice", content: "Status only" });
    if (signal === "snapshot") applyTranscript("cold", { messages: [{ role: "assistant", type: "text", seq: 200, content: "New snapshot text" }],
      oldest_loaded_seq: 200, next_message_seq: 201 });
    if (signal === "layout") { browser.grow(80); fireEvent.scroll(browser.viewport); }
    if (signal === "replay") store.addMessage("cold", { role: "assistant", type: "text", seq: 50, content: "Replayed saved text" });
    if (signal === "wrong attempt" || signal === "terminal") {
      const identity = { request_id: "request", attempt_id: "attempt" };
      store.setActiveAttempt("cold", identity);
      if (signal === "terminal") store.addMessage("cold", { role: "assistant", type: "result", seq: 51, runtime_metadata: identity, content: "Finished" });
      store.appendStreamingText("cold", "Unaccepted text", signal === "terminal" ? identity : { ...identity, attempt_id: "old" });
    }
  });
  browser.settle();
  expect(browser.writes).toEqual([]);
  expect(browser.viewport.scrollTop).toBe(0);
  expect(screen.getByText(/New output will return/)).toBeVisible();
  act(() => store.appendStreamingText("cold", "Actually accepted next text"));
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(browser.viewport.scrollHeight - browser.viewport.clientHeight);
});

it("waits for installed history while allowing reading of an already visible live tail", () => {
  const frames = new Map<number, FrameRequestCallback>();
  let serial = 0;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.set(++serial, callback); return serial; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  const view = render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()}
      connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>);
  const viewport = view.container.querySelector<HTMLElement>(".chat-messages")!;
  act(() => { for (const callback of frames.values()) callback(performance.now()); frames.clear(); });
  expect(viewport).not.toBeVisible();
  expect(screen.getByRole("button", { name: "Browse loaded content" })).toBeVisible();
  expect(screen.getByRole("textbox")).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
  expect(viewport).toBeVisible();
  expect(screen.getByText("Live tail without installed history")).toBeVisible();
  expect(screen.queryByText("Preparing latest messages…")).toBeNull();
});

it.each(["document", "chat pane"])("keeps one opening's remaining budget while the %s is hidden and the window changes", (surface) => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
  let hidden = false;
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
  applyTranscript("cold", { messages: [{ role: "assistant", type: "text", seq: 50, content: "Installed history" }],
    oldest_loaded_seq: 50, next_message_seq: 51 });
  const view = chat();
  const viewport = view.container.querySelector<HTMLElement>(".chat-messages")!;
  let height = 600;
  Object.defineProperty(viewport, "clientHeight", { configurable: true, get: () => height });
  act(() => vi.advanceTimersByTime(900));
  act(() => {
    if (surface === "document") {
      hidden = true;
      document.dispatchEvent(new Event("visibilitychange"));
    } else {
      useSessionStore.getState().openManage("schedules");
      height = 0;
    }
    vi.advanceTimersByTime(20_000);
  });
  act(() => applyTranscript("cold", { messages: [{ role: "assistant", type: "text", seq: 200, content: "New history window" }],
    oldest_loaded_seq: 200, next_message_seq: 201 }));
  act(() => { hidden = false; height = 0; document.dispatchEvent(new Event("visibilitychange")); vi.advanceTimersByTime(20_000); });
  expect(screen.queryByText(/Automatic positioning did not finish/)).toBeNull();
  act(() => {
    height = 600;
    if (surface === "chat pane") useSessionStore.getState().showChat();
    else document.dispatchEvent(new Event("visibilitychange"));
  });
  act(() => vi.advanceTimersByTime(1099));
  expect(screen.queryByText(/Automatic positioning did not finish/)).toBeNull();
  act(() => vi.advanceTimersByTime(1));
  expect(screen.getByText(/Automatic positioning did not finish/)).toBeVisible();
  expect(viewport).toBeVisible();
  expect(screen.queryByRole("button", { name: "Retry positioning" })).toBeNull();
  act(() => { useSessionStore.getState().addMessage("cold", { role: "assistant", type: "text", seq: 201, content: "Late output" });
    document.dispatchEvent(new Event("visibilitychange")); vi.advanceTimersByTime(20_000); });
  expect(screen.queryByText(/Automatic positioning did not finish/)).toBeNull();
  expect(viewport).toBeVisible();
  expect(screen.queryByRole("button", { name: "Browse loaded content" })).toBeNull();
});

it("does not renew a preparation budget when repeated equivalent history stops publishing rows", () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
  const snapshot = { messages: [{ role: "assistant" as const, type: "text", seq: 50, content: "Equivalent history" }],
    oldest_loaded_seq: 50, next_message_seq: 51 };
  applyTranscript("cold", snapshot);
  const before = useSessionStore.getState().messages.cold;
  const view = chat();
  for (let index = 0; index < 20; index++) act(() => {
    vi.advanceTimersByTime(80);
    applyTranscript("cold", JSON.parse(JSON.stringify(snapshot)));
  });
  expect(useSessionStore.getState().messages.cold).toBe(before);
  act(() => vi.advanceTimersByTime(399));
  expect(screen.queryByText(/Automatic positioning did not finish/)).toBeNull();
  act(() => vi.advanceTimersByTime(1));
  expect(screen.getByText(/Automatic positioning did not finish/)).toBeVisible();
  expect(view.container.querySelector(".chat-messages")).toBeVisible();
});

it("retains an in-flight new-text recovery through equivalent history with no display publication", () => {
  const browser = timedOutViewport();
  const snapshot = { messages: useSessionStore.getState().messages.cold, oldest_loaded_seq: 50, next_message_seq: 51 };
  browser.pause("hidden", true);
  act(() => useSessionStore.getState().appendStreamingText("cold", "Actually accepted text"));
  for (let index = 0; index < 20; index++) act(() => applyTranscript("cold", JSON.parse(JSON.stringify(snapshot))));
  expect(useSessionStore.getState().messages.cold).toBe(snapshot.messages);
  expect(browser.writes).toEqual([]);
  browser.pause("hidden", false);
  browser.settle();
  expect(browser.viewport.scrollTop).toBe(1000);
  expect(browser.writes.length).toBeGreaterThan(0);
  expect(browser.writes.every(hidden => !hidden)).toBe(true);
  expect(screen.queryByText(/Automatic positioning did not finish/)).toBeNull();
});

it("exposes a failed necessary history read and retains the explicit reading choice through a late success", async () => {
  useSessionStore.setState({ token: "cold-token", connected: true });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "History unavailable" }, { status: 503 })));
  const view = chat();
  await act(async () => { await observeSession("cold", "detail").done.catch(() => {}); });
  expect(screen.getByText("Conversation unavailable")).toBeVisible();
  expect(screen.getByText(/Could not load session state/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
  act(() => applyTranscript("cold", { messages: [{ role: "assistant", type: "text", seq: 200, content: "Late legitimate history" }],
    oldest_loaded_seq: 200, next_message_seq: 201 }));
  expect(view.container.querySelector(".chat-messages")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Browse loaded content" })).toBeNull();
});

it("shows the latest failed history retry while the conversation is unavailable", async () => {
  useSessionStore.setState({ token: "cold-token", connected: true });
  let status = 503;
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({}, { status })));
  chat();
  await act(async () => { await observeSession("cold", "detail").done.catch(() => {}); });
  const preparation = within(screen.getByLabelText("Conversation preparation"));
  expect(preparation.getByText(/Could not load session state \(HTTP 503\)/)).toBeVisible();

  status = 504;
  await act(async () => fireEvent.click(preparation.getByRole("button", { name: "Retry loading" })));
  expect(preparation.getByText("Conversation unavailable")).toBeVisible();
  expect(preparation.getByText(/Could not load session state \(HTTP 504\)/)).toBeVisible();
  expect(preparation.queryByText(/Could not load session state \(HTTP 503\)/)).toBeNull();
});

it("resumes the remaining preparation budget when a touch contact is cancelled", () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
  applyTranscript("cold", { messages: [{ role: "assistant", type: "text", seq: 50, content: "Installed history" }],
    oldest_loaded_seq: 50, next_message_seq: 51 });
  const view = chat();
  const viewport = view.container.querySelector<HTMLElement>(".chat-messages")!;
  const touch = { identifier: 7, clientY: 400 };
  act(() => vi.advanceTimersByTime(500));
  fireEvent.touchStart(viewport, { touches: [touch] });
  act(() => vi.advanceTimersByTime(10_000));
  expect(screen.queryByText(/Automatic positioning did not finish/)).toBeNull();
  fireEvent.touchCancel(viewport, { touches: [], changedTouches: [touch] });
  act(() => vi.advanceTimersByTime(1500));
  expect(screen.getByText(/Automatic positioning did not finish/)).toBeVisible();
  expect(viewport).toBeVisible();
});

it("keeps the remaining preparation budget paused across a window replacement until contact ends", () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
  applyTranscript("cold", { messages: useSessionStore.getState().messages.cold,
    oldest_loaded_seq: 50, next_message_seq: 51 });
  const view = chat();
  const viewport = view.container.querySelector<HTMLElement>(".chat-messages")!;
  const touch = { identifier: 7, clientY: 400 };
  act(() => vi.advanceTimersByTime(500));
  fireEvent.touchStart(viewport, { touches: [touch] });
  act(() => applyTranscript("cold", { messages: [{ role: "assistant", type: "text", seq: 200, content: "Replaced while touching" }],
    oldest_loaded_seq: 200, next_message_seq: 201 }));
  act(() => vi.advanceTimersByTime(10_000));
  expect(screen.queryByText(/Automatic positioning did not finish/)).toBeNull();
  fireEvent.touchEnd(document.body, { touches: [], changedTouches: [touch] });
  act(() => vi.advanceTimersByTime(1500));
  expect(screen.getByText(/Automatic positioning did not finish/)).toBeVisible();
  expect(viewport).toBeVisible();
});

it.each(["position first", "deadline first"])("settles reveal and expiry in either delivery order: %s", (order) => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
  mockChatItemLayout();
  useSessionStore.getState().updateSessionStatus("cold", "idle");
  applyTranscript("cold", { messages: useSessionStore.getState().messages.cold,
    oldest_loaded_seq: 50, next_message_seq: 51 });
  const frames = new Map<number, FrameRequestCallback>();
  let serial = 0;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.set(++serial, callback); return serial; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  const view = chat();
  const viewport = view.container.querySelector<HTMLElement>(".chat-messages")!;
  const flush = () => {
    const batch = [...frames.values()]; frames.clear();
    act(() => { for (const callback of batch) callback(performance.now()); });
  };
  act(() => vi.advanceTimersByTime(1990));
  if (order === "position first") {
    for (let frame = 0; frame < 4 && viewport.style.visibility === "hidden"; frame++) flush();
    expect(viewport).toBeVisible();
    // No intervening frame may be needed to cancel the deadline after a
    // qualified reveal. Deliver expiry before any remaining frame callbacks.
    const late = [...frames.values()];
    act(() => vi.advanceTimersByTime(10));
    expect(screen.queryByText(/Automatic positioning did not finish/)).toBeNull();
    expect(viewport).toBeVisible();
    act(() => { for (const callback of late) callback(performance.now()); });
    expect(viewport).toBeVisible();
  } else {
    const late = [...frames.values()];
    act(() => vi.advanceTimersByTime(10));
    expect(screen.getByText(/Automatic positioning did not finish/)).toBeVisible();
    act(() => { for (const callback of late) callback(performance.now()); });
    flush();
    expect(screen.getByText(/Automatic positioning did not finish/)).toBeVisible();
    expect(viewport).toBeVisible();
  }
});

it("settles repeated damaged-history retries without losing rejection coverage, then accepts a repaired detail", async () => {
  const info = useSessionStore.getState().sessions[0];
  useSessionStore.setState({ token: "damage-token", connected: true, lastAppliedSeq: { cold: 50 } });
  // The real session-list owner clears account rows when the token changes.
  useSessionStore.setState({ sessions: [info] });
  useSessionStore.getState().applyDispatchState("cold", { generation: "damage-service", revision: 0,
    paused: false, items: [], active_request_id: null, active_operation: null });
  let damaged = true;
  const read = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).includes("/submissions")) return Response.json({ items: [], next_cursor: null });
    return Response.json({ ...info, transcript_integrity: damaged ? "duplicate_seq" : "ok",
      messages: damaged ? [] : [{ role: "assistant", type: "text", seq: 51, content: "Repaired history" }],
      oldest_loaded_seq: damaged ? null : 51, next_message_seq: damaged ? 51 : 52, has_more_messages: false,
      pending_questions: [], subagents: [], active_attempt: null,
      dispatch_state: { generation: "damage-service", revision: 1, paused: false, items: [], active_request_id: null, active_operation: null } });
  });
  vi.stubGlobal("fetch", read);
  const view = chat();
  await act(async () => { expect(await observeSession("cold", "detail").done).not.toBeNull(); });
  expect(useSessionStore.getState().sessions.find((row) => row.id === "cold")?.transcript_integrity).toBe("duplicate_seq");
  expect(screen.getByText("Conversation unavailable")).toBeVisible();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Retry loading" })); });
  expect(screen.getByText("Conversation unavailable")).toBeVisible();
  expect(useSessionStore.getState().lastAppliedSeq.cold).toBe(50);
  damaged = false;
  await act(async () => { await observeSession("cold", "detail").done; });
  expect(useSessionStore.getState().lastAppliedSeq.cold).toBe(51);
  // A repaired read opens trusted content without renewing positioning work.
  expect(screen.queryByText("Conversation unavailable")).toBeNull();
  expect(screen.queryByRole("button", { name: "Retry positioning" })).toBeNull();
  expect(view.getByText("Repaired history")).toBeVisible();
});

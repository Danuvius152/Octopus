import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { applyTranscript } from "../lib/transcript";
import { observeSession } from "../lib/hydrateSession";

beforeEach(() => {
  mockChatViewport();
  useSessionStore.getState().setToken("");
  useSessionStore.setState({ activeSessionId: "cold", mainView: "chat", sessions: [{
    id: "cold", name: "Cold session", status: "running", backend: "codex",
    created_at: "2026-11-16", working_dir: "/tmp", archived: false,
  } as SessionInfo], archivedSessions: [], agents: [], credentials: [], backendModels: {},
    messages: { cold: [{ role: "assistant", type: "text", seq: 50, content: "Live tail without installed history" }] },
    messageWindows: { cold: Symbol("not a baseline") }, oldestSeq: {}, hasMoreMessages: {}, lastAppliedSeq: {},
    streamingText: {}, streamingItems: {}, subagents: {}, pendingQuestions: {}, dispatchStates: {}, dispatchUnavailable: {},
  });
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("chat-messages") ? 600 : 0;
  });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function chat() {
  return render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()}
      connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>);
}

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

it("keeps one opening's remaining foreground budget across hidden time and window replacement", () => {
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
  act(() => { hidden = true; document.dispatchEvent(new Event("visibilitychange")); vi.advanceTimersByTime(20_000); });
  act(() => applyTranscript("cold", { messages: [{ role: "assistant", type: "text", seq: 200, content: "New history window" }],
    oldest_loaded_seq: 200, next_message_seq: 201 }));
  act(() => { hidden = false; height = 0; document.dispatchEvent(new Event("visibilitychange")); vi.advanceTimersByTime(20_000); });
  expect(screen.queryByText("Positioning incomplete")).toBeNull();
  act(() => { height = 600; document.dispatchEvent(new Event("visibilitychange")); vi.advanceTimersByTime(1099); });
  expect(screen.queryByText("Positioning incomplete")).toBeNull();
  act(() => vi.advanceTimersByTime(1));
  expect(screen.getByText("Positioning incomplete")).toBeVisible();
  expect(viewport).not.toBeVisible();
  act(() => { useSessionStore.getState().addMessage("cold", { role: "assistant", type: "text", seq: 201, content: "Late output" });
    document.dispatchEvent(new Event("visibilitychange")); vi.advanceTimersByTime(20_000); });
  expect(screen.getByText("Positioning incomplete")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Retry positioning" }));
  expect(screen.queryByText("Positioning incomplete")).toBeNull();
  act(() => vi.advanceTimersByTime(1999));
  expect(screen.queryByText("Positioning incomplete")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
  act(() => vi.advanceTimersByTime(20_000));
  expect(viewport).toBeVisible();
  expect(screen.queryByText("Positioning incomplete")).toBeNull();
});

it("exposes a failed necessary history read and retains the explicit reading choice through a late success", async () => {
  useSessionStore.setState({ token: "cold-token", connected: true });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "History unavailable" }, { status: 503 })));
  const view = chat();
  await act(async () => { await observeSession("cold", "detail").done.catch(() => {}); });
  expect(screen.getByText("Positioning incomplete")).toBeVisible();
  expect(screen.getByText(/Could not load session state/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
  act(() => applyTranscript("cold", { messages: [{ role: "assistant", type: "text", seq: 200, content: "Late legitimate history" }],
    oldest_loaded_seq: 200, next_message_seq: 201 }));
  expect(view.container.querySelector(".chat-messages")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Browse loaded content" })).toBeNull();
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
  expect(screen.queryByText("Positioning incomplete")).toBeNull();
  fireEvent.touchCancel(viewport, { touches: [], changedTouches: [touch] });
  act(() => vi.advanceTimersByTime(1500));
  expect(screen.getByText("Positioning incomplete")).toBeVisible();
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
  expect(screen.queryByText("Positioning incomplete")).toBeNull();
  fireEvent.touchEnd(document.body, { touches: [], changedTouches: [touch] });
  act(() => vi.advanceTimersByTime(1500));
  expect(screen.getByText("Positioning incomplete")).toBeVisible();
});

it.each(["position first", "deadline first"])("settles reveal and expiry in either delivery order: %s", (order) => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
  useSessionStore.setState({ messages: {} });
  applyTranscript("cold", { messages: [], next_message_seq: 0, oldest_loaded_seq: null, has_more_messages: false });
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
    expect(screen.queryByText("Positioning incomplete")).toBeNull();
    expect(viewport).toBeVisible();
    act(() => { for (const callback of late) callback(performance.now()); });
    expect(viewport).toBeVisible();
  } else {
    const late = [...frames.values()];
    act(() => vi.advanceTimersByTime(10));
    expect(screen.getByText("Positioning incomplete")).toBeVisible();
    act(() => { for (const callback of late) callback(performance.now()); });
    flush();
    expect(screen.getByText("Positioning incomplete")).toBeVisible();
    expect(viewport).not.toBeVisible();
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
  expect(screen.getByText("Positioning incomplete")).toBeVisible();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Retry positioning" })); });
  expect(screen.getByText("Positioning incomplete")).toBeVisible();
  expect(useSessionStore.getState().lastAppliedSeq.cold).toBe(50);
  damaged = false;
  await act(async () => { await observeSession("cold", "detail").done; });
  expect(useSessionStore.getState().lastAppliedSeq.cold).toBe(51);
  // A repaired read does not silently restart a settled preparation operation.
  expect(screen.getByText("Positioning incomplete")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
  expect(view.getByText("Repaired history")).toBeVisible();
});

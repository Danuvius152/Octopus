import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ChatView } from "./ChatView";
import { mockChatViewport, mockChatItemLayout } from "./chatViewport.test-support";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { applyTranscript } from "../lib/transcript";

const session = { id: "motion", name: "Motion", agent_id: "agent", status: "idle", backend: "codex",
  created_at: "2026-11-13", working_dir: "/tmp", archived: false } as SessionInfo;

beforeEach(() => {
  mockChatViewport();
  sessionStorage.clear();
  useSessionStore.getState().setToken("");
  useSessionStore.setState({ activeSessionId: session.id, mainView: "chat", sessions: [session], archivedSessions: [],
    agents: [], credentials: [], backendModels: {},
    messages: { [session.id]: Array.from({ length: 15 }, (_, index) => ({
      role: "assistant", type: "text", seq: index + 1, content: `Saved text ${index + 1}`,
    })) },
    messageWindows: { [session.id]: Symbol("original window") },
    streamingText: {}, streamingItems: {}, pendingQuestions: {}, subagents: {}, dispatchStates: {}, dispatchUnavailable: {},
  });
  applyTranscript(session.id, { messages: useSessionStore.getState().messages[session.id],
    next_message_seq: 16, oldest_loaded_seq: 1, has_more_messages: false });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

// Only browser dimensions, delivery and time are controlled. The real ChatView,
// store and real Virtuoso measurement path still own rendering and following.
function motionViewport(bottomRemainder = 0, revealGrowth = 0, stopAtReveal = false, scrollQuantum = 1) {
  mockChatItemLayout();
  let time = 1000;
  let height = 1200;
  let viewportHeight = 600;
  let hidden = false;
  let serial = 0;
  let afterScroll: (() => void) | undefined;
  const layout = vi.mocked(HTMLElement.prototype.getBoundingClientRect).getMockImplementation()!;
  let footerHeight = 0;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const viewport = this.closest<HTMLElement>(".chat-messages");
    if (viewport && this.hasAttribute("data-chat-rows")) {
      return new DOMRect(0, -viewport.scrollTop, 800, height - footerHeight);
    }
    if (viewport && this.dataset.index === "14") {
      return new DOMRect(0, height - footerHeight - 80 - viewport.scrollTop, 800, 80);
    }
    if (viewport && this.firstElementChild?.hasAttribute("data-chat-footer")) {
      // One real layout change at the reveal boundary, with no subsequent
      // token or ResizeObserver delivery to rescue a missing write check.
      if (revealGrowth && !footerHeight && viewport.style.visibility !== "hidden") {
        footerHeight = revealGrowth;
        height += revealGrowth;
      }
      return new DOMRect(0, height - viewport.scrollTop - footerHeight, 800, footerHeight);
    }
    return layout.call(this);
  });
  const queued = new Map<number, FrameRequestCallback>();
  const observers = new Map<ResizeObserver, { callback: ResizeObserverCallback; elements: Set<Element> }>();
  vi.spyOn(performance, "now").mockImplementation(() => time);
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    const id = ++serial; queued.set(id, callback); return id;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => queued.delete(id));
  vi.stubGlobal("ResizeObserver", class implements ResizeObserver {
    constructor(callback: ResizeObserverCallback) { observers.set(this, { callback, elements: new Set() }); }
    observe(element: Element) { observers.get(this)!.elements.add(element); }
    unobserve(element: Element) { observers.get(this)!.elements.delete(element); }
    disconnect() { observers.delete(this); }
  });
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("chat-messages") ? viewportHeight : 0;
  });
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("chat-messages") ? height : 0;
  });
  const view = render(<ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()}
    connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />);
  const viewport = view.container.querySelector<HTMLElement>(".chat-messages")!;
  const writes: { at: number; top: number }[] = [];
  viewport.scrollTo = (optionsOrX?: ScrollToOptions | number, y?: number) => {
    const top = typeof optionsOrX === "number" ? y ?? 0 : optionsOrX?.top ?? 0;
    const rounded = Math.round(top / scrollQuantum) * scrollQuantum;
    viewport.scrollTop = Math.max(0, Math.min(height - viewportHeight - bottomRemainder, rounded));
    writes.push({ at: time, top: viewport.scrollTop });
    fireEvent.scroll(viewport);
    afterScroll?.();
  };
  const tick = (dt: number) => {
    time += dt;
    const batch = [...queued.values()]; queued.clear();
    // A frame timeline can precede the request/execution clock. Preserve the
    // observed ordering rather than making the platform imply a positive dt.
    act(() => {
      for (const callback of batch) callback(time - 40);
      for (const [observer, { callback, elements }] of observers) for (const element of elements) {
        if (element === viewport) continue;
        const signature = Array.from(element.children).map((child) => (child as HTMLElement).dataset.index ?? "").join(",");
        if (measured.get(element) === signature) continue;
        measured.set(element, signature);
        callback([{ target: element } as ResizeObserverEntry], observer);
      }
    });
  };
  const measured = new WeakMap<Element, string>();
  const notify = () => {
    const entry: ResizeObserverEntry = { target: viewport, contentRect: new DOMRect(0, 0, 800, viewportHeight),
      borderBoxSize: [{ inlineSize: 800, blockSize: viewportHeight }], contentBoxSize: [{ inlineSize: 800, blockSize: viewportHeight }],
      devicePixelContentBoxSize: [] };
    act(() => {
      for (const [observer, { callback, elements }] of observers) {
        if (elements.has(viewport)) callback([entry], observer);
      }
    });
  };
  for (let index = 0; index < (revealGrowth ? 12 : 8); index++) {
    tick(16);
    if (stopAtReveal && viewport.style.visibility !== "hidden") break;
  }
  expect(viewport.scrollTop, "initial positioning uses the physical bottom").toBe(600 + revealGrowth - bottomRemainder);
  if (!revealGrowth) expect(view.getByText("Saved text 15"), "the initial target participates in the rendered list").toBeVisible();
  expect(viewport).toBeVisible();
  writes.length = 0;
  return { viewport, writes, tick,
    grow: (amount: number, deliverResize = true) => {
      height += amount;
      viewport.scrollTop = Math.min(viewport.scrollTop, Math.max(0, height - viewportHeight - bottomRemainder));
      if (deliverResize) notify();
    },
    resize: (next: number) => { viewportHeight = next; notify(); },
    afterScroll: (callback?: () => void) => { afterScroll = callback; },
    takeFrames: () => { const batch = [...queued.values()]; queued.clear(); return batch; },
    unmount: () => view.unmount(),
    now: () => time, pending: () => queued.size,
    visibility: (next: boolean) => act(() => { hidden = next; document.dispatchEvent(new Event("visibilitychange")); }) };
}

it.each([
  ["60Hz", [1000 / 60]], ["120Hz", [1000 / 120]], ["irregular frames", [8, 25, 11, 33, 17]],
] as const)("converges across successive reply growth and stops at %s", (_label, cadence) => {
  const motion = motionViewport();
  const start = motion.now();
  let additions = 1;
  let lastGrowth = start;
  let frame = 0;
  const positions: number[] = [];
  motion.grow(100);
  while (motion.now() - start < 600) {
    motion.tick(cadence[frame++ % cadence.length]);
    positions.push(motion.viewport.scrollTop);
    if (additions < 3 && motion.now() - start >= additions * 80) {
      motion.grow(100); additions++; lastGrowth = motion.now();
    }
    if (additions === 3 && motion.viewport.scrollTop >= 898) break;
  }
  expect(additions).toBe(3);
  expect(motion.viewport.scrollTop).toBeGreaterThanOrEqual(898);
  expect(motion.now() - lastGrowth, "300px burst converges within the frozen budget").toBeLessThanOrEqual(350);
  expect(positions.filter((top, index) => index > 0 && top > positions[index - 1] && top < 898).length).toBeGreaterThanOrEqual(3);
  expect(positions.every((top, index) => top <= 900 && (index === 0 || top >= positions[index - 1]))).toBe(true);
  for (let index = 0; index < 4; index++) motion.tick(20);
  expect(motion.viewport.scrollTop).toBe(900);
  const settled = motion.writes.length;
  for (let index = 0; index < 12; index++) motion.tick(20);
  expect(motion.writes).toHaveLength(settled);
  expect(motion.pending(), "settled following leaves no queued frames").toBe(0);
});

it("self-verifies Footer growth during reveal layout without another output or resize", () => {
  const motion = motionViewport(0, 900);
  expect(motion.viewport.scrollTop).toBe(1500);
  for (let index = 0; index < 12; index++) motion.tick(16);
  expect(motion.writes).toHaveLength(0);
  expect(motion.pending()).toBe(0);
});

it("reaches the latest output on the first revealed frame, then resumes normal following", () => {
  const motion = motionViewport(0, 0, true);
  // The reveal layout has committed. A live line can still grow before the
  // first revealed frame, without a separate resize notification.
  motion.grow(23, false);
  act(() => useSessionStore.setState({ sessions: [{ ...session, status: "running" }],
    streamingText: { [session.id]: { content: "Output before the first revealed frame\n" } } }));
  expect(screen.getByText("Output before the first revealed frame")).toBeVisible();
  motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(623);
  motion.grow(300);
  motion.tick(16);
  expect(motion.viewport.scrollTop).toBeGreaterThan(623);
  expect(motion.viewport.scrollTop).toBeLessThan(923);
  for (let index = 0; index < 32; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(923);
  expect(motion.pending()).toBe(0);
});

it("stops immediately while the page is hidden and resumes with a fresh time origin", () => {
  const motion = motionViewport();
  motion.grow(300); motion.tick(16);
  const readingTop = motion.viewport.scrollTop;
  expect(readingTop).toBeGreaterThan(600);
  expect(readingTop).toBeLessThan(900);
  expect(motion.pending()).toBeGreaterThan(0);
  motion.visibility(true);
  const stopped = motion.writes.length;
  // Virtuoso may still owe one browser measurement from the preceding scroll.
  // It cannot turn into a write or a continuing automatic task while hidden.
  motion.tick(0);
  expect(motion.writes).toHaveLength(stopped);
  expect(motion.pending()).toBe(0);
  motion.tick(60_000); motion.grow(100); motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(readingTop);
  expect(motion.writes).toHaveLength(stopped);
  expect(motion.pending()).toBe(0);
  motion.visibility(false); motion.tick(16);
  expect(motion.viewport.scrollTop).toBeGreaterThan(readingTop);
  expect(motion.viewport.scrollTop - readingTop, "hidden time cannot become one full-gap step").toBeLessThan(150);
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(1000);
  expect(motion.pending()).toBe(0);
});

it("honors reduced motion with one immediate update and no idle follow frames", () => {
  const query = vi.fn((media: string) => ({ media, matches: media === "(prefers-reduced-motion: reduce)" }));
  vi.stubGlobal("matchMedia", query);
  const motion = motionViewport();
  motion.grow(300); motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(900);
  motion.tick(0);
  expect(motion.writes).toHaveLength(1);
  expect(motion.pending()).toBe(0);
  motion.grow(100); motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(1000);
  expect(query.mock.calls.filter(([media]) => media === "(prefers-reduced-motion: reduce)")).toHaveLength(1);
  const settled = motion.writes.length;
  for (let index = 0; index < 8; index++) motion.tick(16);
  expect(motion.writes).toHaveLength(settled);
});

it("settles at a fractional physical bottom without repeating clamped scroll writes", () => {
  // Rounded size properties report a bottom of 901, while the browser can
  // apply at most 900.5. The completed movement must not poll that residual.
  const motion = motionViewport(0.5);
  motion.grow(301);
  for (let index = 0; index < 40; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(900.5);
  expect(motion.pending(), "a subpixel remainder is already within the completion tolerance").toBe(0);
  const settled = motion.writes.length;
  for (let index = 0; index < 12; index++) motion.tick(16);
  expect(motion.writes).toHaveLength(settled);
});

it.each([false, true])("handles a quantized step before the reachable tail (navigation=%s)", (navigate) => {
  const motion = motionViewport(0, 0, false, 4);
  motion.grow(4);
  if (navigate) motion.afterScroll(() => fireEvent.keyDown(motion.viewport, { key: "PageUp" }));
  motion.tick(16);
  expect(motion.writes.map(({ top }) => top)).toEqual(navigate ? [600] : [600, 604]);
  for (let frame = 0; frame < 8; frame++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(navigate ? 600 : 604);
  expect(motion.pending()).toBe(0);
});

it("takes an intermediate step on a short frame under browser zoom", () => {
  vi.stubGlobal("devicePixelRatio", 0.25);
  const motion = motionViewport(0, 0, false, 4);
  motion.grow(80);
  motion.tick(1);
  expect(motion.writes.map(({ top }) => top)).toEqual([604]);
  for (let frame = 0; frame < 20; frame++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(680);
  expect(motion.pending()).toBe(0);
});

it.each([0, 0.5])("keeps following after a downward wheel at a %spx bottom remainder", (remainder) => {
  const motion = motionViewport(remainder);
  fireEvent.wheel(motion.viewport, { deltaY: 100 });
  motion.grow(100);
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(700 - remainder);
  expect(motion.pending()).toBe(0);
});

it.each(["wheel", "End"])("resumes reading clamped to bottom on explicit %s without extra displacement", (input) => {
  const motion = motionViewport(0.5);
  fireEvent.keyDown(motion.viewport, { key: "PageUp" });
  motion.viewport.scrollTop = 400;
  fireEvent.scroll(motion.viewport);
  motion.grow(-200);
  expect(motion.viewport.scrollTop).toBe(399.5);
  motion.tick(16);
  expect(motion.writes).toHaveLength(0);
  if (input === "wheel") fireEvent.wheel(motion.viewport, { deltaY: 100 });
  else fireEvent.keyDown(motion.viewport, { key: "End" });
  motion.grow(100);
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(499.5);
  expect(motion.pending()).toBe(0);
});

it("preserves reading without a visible anchor when detail replaces the same opening's window", () => {
  const motion = motionViewport();
  fireEvent.keyDown(motion.viewport, { key: "PageUp" });
  motion.viewport.scrollTop = 400;
  fireEvent.scroll(motion.viewport);
  // No visible DOM anchor is available in this controlled geometry, as when
  // reading inside a tall Footer. Window replacement must retain the intent.
  act(() => applyTranscript(session.id, { messages: [
    { role: "assistant", type: "text", seq: 200, content: "Detail arrived while reading" },
  ], oldest_loaded_seq: 200, next_message_seq: 201, has_more_messages: false }));
  for (let index = 0; index < 12; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(400);
  expect(motion.writes).toHaveLength(0);
  motion.grow(100);
  for (let index = 0; index < 12; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(400);
  expect(motion.pending()).toBe(0);
  expect(screen.getByText(/The previous reading position is unavailable/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Jump to latest" }));
  motion.tick(16);
  expect(motion.viewport.scrollTop).toBeGreaterThan(400);
  expect(motion.viewport.scrollTop).toBeLessThan(700);
  for (let index = 0; index < 21; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(700);
  expect(screen.queryByText(/The previous reading position is unavailable/)).toBeNull();
});

it.each([false, true])("shows a readable boundary and returns to latest after a lost identity (equivalent refresh=%s)", (refresh) => {
  const motion = motionViewport();
  const first = motion.viewport.querySelector<HTMLElement>("[data-message-key]")!;
  vi.spyOn(motion.viewport, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 800, 600));
  vi.spyOn(first, "getBoundingClientRect").mockReturnValue(new DOMRect(0, -20, 800, 80));
  fireEvent.keyDown(motion.viewport, { key: "PageUp" });
  motion.viewport.scrollTop = 400;
  fireEvent.scroll(motion.viewport);
  act(() => applyTranscript(session.id, { messages: [
    { role: "assistant", type: "text", seq: 200, content: "Replacement readable boundary" },
  ], oldest_loaded_seq: 200, next_message_seq: 201, has_more_messages: false }));
  for (let index = 0; index < 12; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(400);
  expect(screen.getByText(/The previous reading position is unavailable/)).toBeVisible();
  motion.grow(100); motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(400);
  if (refresh) {
    act(() => applyTranscript(session.id, { messages: [
      { role: "assistant", type: "text", seq: 200, content: "Replacement readable boundary" },
    ], oldest_loaded_seq: 200, next_message_seq: 201, has_more_messages: false }));
    for (let index = 0; index < 12; index++) motion.tick(16);
  }
  fireEvent.click(screen.getByRole("button", { name: "Jump to latest" }));
  motion.tick(16);
  expect(motion.viewport.scrollTop).toBeGreaterThan(400);
  expect(motion.viewport.scrollTop).toBeLessThan(700);
  for (let index = 0; index < 21; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(700);
  expect(screen.queryByText(/The previous reading position is unavailable/)).toBeNull();
});

it("pauses output for the whole touch contact and resumes only after its qualified downward release", () => {
  const motion = motionViewport();
  const touch = (clientY: number) => ({ identifier: 7, clientY });
  fireEvent.touchStart(motion.viewport, { touches: [touch(400)] });
  motion.grow(100);
  for (let index = 0; index < 12; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(600);
  expect(motion.writes).toHaveLength(0);
  fireEvent.touchMove(motion.viewport, { touches: [touch(300)] });
  motion.viewport.scrollTop = 700; fireEvent.scroll(motion.viewport);
  motion.grow(100);
  for (let index = 0; index < 12; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(700);
  motion.viewport.scrollTop = 800; fireEvent.scroll(motion.viewport);
  fireEvent.touchEnd(motion.viewport, { touches: [], changedTouches: [touch(300)] });
  motion.grow(100);
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(900);
  expect(motion.pending()).toBe(0);
});

it("ignores an unrelated changed touch before the qualified owner releases", () => {
  const motion = motionViewport();
  fireEvent.keyDown(motion.viewport, { key: "PageUp" });
  const touch = { identifier: 7, clientY: 400 };
  fireEvent.touchStart(motion.viewport, { touches: [touch] });
  const moved = { ...touch, clientY: 300 };
  fireEvent.touchMove(motion.viewport, { touches: [moved] });
  fireEvent.touchEnd(motion.viewport, { touches: [moved], changedTouches: [{ identifier: 8, clientY: 300 }] });
  fireEvent.touchEnd(motion.viewport, { touches: [], changedTouches: [moved] });
  motion.grow(100);
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(700);
  expect(motion.pending()).toBe(0);
});

function thumbEvent(viewport: HTMLElement, type: "pointerdown" | "pointerup", clientY: number) {
  // JSDOM omits PointerEvent. Supply the browser event fields used by the
  // real capture handler; a native scrollbar may deliver no pointermove.
  const event = new MouseEvent(type, { bubbles: true, button: 0, clientX: 801, clientY });
  Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: "mouse" }, isPrimary: { value: true } });
  fireEvent(viewport, event);
}

it("keeps an upward thumb drag in reading when native movement emits no pointermove", () => {
  const motion = motionViewport();
  thumbEvent(motion.viewport, "pointerdown", 500);
  motion.viewport.scrollTop = 400;
  fireEvent.scroll(motion.viewport);
  thumbEvent(motion.viewport, "pointerup", 380);
  motion.grow(100);
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(400);
  expect(motion.pending()).toBe(0);
});

it.each(["at release", "after release", "after deadline"] as const)(
  "uses the release deadline for the first upward thumb displacement %s", (timing) => {
    const motion = motionViewport();
    const jump = () => screen.queryByRole("button", { name: "Jump to latest" });
    thumbEvent(motion.viewport, "pointerdown", 500);
    motion.tick(650);
    expect(jump()).toBeNull();
    if (timing === "at release") motion.viewport.scrollTop = 400;
    thumbEvent(motion.viewport, "pointerup", 380);
    if (timing !== "at release") {
      expect(jump()).toBeNull();
      motion.tick(timing === "after deadline" ? 550 : 16);
      motion.viewport.scrollTop = 400;
      fireEvent.scroll(motion.viewport);
    }
    if (timing === "after deadline") expect(jump()).toBeNull();
    else expect(jump()).toBeVisible();
  },
);

it("retains the original following intent through a window replacement during an unmoved thumb contact", () => {
  const motion = motionViewport();
  thumbEvent(motion.viewport, "pointerdown", 500);
  act(() => applyTranscript(session.id, { messages: [
    { role: "assistant", type: "text", seq: 200, content: "Replacement while holding thumb" },
  ], oldest_loaded_seq: 200, next_message_seq: 201, has_more_messages: false }));
  thumbEvent(motion.viewport, "pointerup", 500);
  for (let index = 0; index < 12; index++) motion.tick(16);
  motion.grow(100);
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(700);
  expect(motion.pending()).toBe(0);
});

it("keeps a contact suspended through a same-opening window replacement until its matching release", () => {
  const motion = motionViewport();
  const touch = { identifier: 7, clientY: 400 };
  fireEvent.touchStart(motion.viewport, { touches: [touch] });
  act(() => applyTranscript(session.id, { messages: [
    { role: "assistant", type: "text", seq: 200, content: "Detail while still touching" },
  ], oldest_loaded_seq: 200, next_message_seq: 201, has_more_messages: false }));
  motion.grow(100);
  for (let index = 0; index < 12; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(600);
  expect(motion.writes).toHaveLength(0);
  // The original target may have gone away; the opening still owns the contact.
  fireEvent.touchEnd(document.body, { touches: [], changedTouches: [touch] });
  for (let index = 0; index < 12; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(700);
  motion.grow(100);
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(800);
  expect(motion.pending()).toBe(0);
});

it.each(["tap", "cancel", "multiple contacts"])("does not turn paused reading into following after a touch %s", (ending) => {
  const motion = motionViewport();
  fireEvent.keyDown(motion.viewport, { key: "PageUp" });
  const touch = { identifier: 7, clientY: 400 };
  fireEvent.touchStart(motion.viewport, { touches: [touch] });
  if (ending === "multiple contacts") {
    fireEvent.touchStart(motion.viewport, { touches: [touch, { identifier: 8, clientY: 300 }] });
    fireEvent.touchEnd(motion.viewport, { touches: [], changedTouches: [touch] });
  } else if (ending === "cancel") {
    fireEvent.touchMove(motion.viewport, { touches: [{ ...touch, clientY: 300 }] });
    fireEvent.touchCancel(motion.viewport, { touches: [], changedTouches: [touch] });
  } else fireEvent.touchEnd(motion.viewport, { touches: [], changedTouches: [touch] });
  motion.grow(100);
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(600);
  expect(motion.pending()).toBe(0);
});

it("keeps following suspended until the last finger leaves a multiple-contact gesture", () => {
  const motion = motionViewport();
  const first = { identifier: 7, clientY: 400 };
  const second = { identifier: 8, clientY: 300 };
  fireEvent.touchStart(motion.viewport, { touches: [first] });
  fireEvent.touchStart(motion.viewport, { touches: [first, second] });
  motion.grow(100);
  for (let index = 0; index < 12; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(600);
  fireEvent.touchEnd(motion.viewport, { touches: [second], changedTouches: [first] });
  motion.grow(100);
  for (let index = 0; index < 12; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(600);
  fireEvent.touchEnd(motion.viewport, { touches: [], changedTouches: [second] });
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(800);
});

it("retires a zero-height viewport's motion at measurement and resumes from its new geometry", () => {
  const motion = motionViewport();
  motion.grow(300); motion.tick(16);
  const top = motion.viewport.scrollTop;
  motion.resize(0);
  const stopped = motion.writes.length;
  motion.tick(0);
  expect(motion.writes).toHaveLength(stopped);
  expect(motion.pending()).toBe(0);
  motion.tick(60_000);
  expect(motion.writes).toHaveLength(stopped);
  motion.resize(600); motion.tick(16);
  expect(motion.viewport.scrollTop).toBeGreaterThan(top);
  expect(motion.viewport.scrollTop - top).toBeLessThan(100);
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(900);
  expect(motion.pending()).toBe(0);
});

it("keeps reading paused through page hiding and viewport resizing", () => {
  const motion = motionViewport();
  motion.grow(300); motion.tick(16);
  fireEvent.keyDown(motion.viewport, { key: "PageUp" });
  const readingTop = motion.viewport.scrollTop;
  const stopped = motion.writes.length;
  motion.visibility(true); motion.resize(0); motion.grow(100); motion.tick(60_000);
  motion.visibility(false); motion.resize(600);
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(readingTop);
  expect(motion.writes).toHaveLength(stopped);
  expect(motion.pending()).toBe(0);
});

it("keeps a successor created during a cancelled scroll write usable", () => {
  const motion = motionViewport();
  motion.grow(300);
  motion.afterScroll(() => {
    motion.afterScroll();
    // Controlled input and physical return while the old platform write is
    // still on the stack. The successor owns the next measured growth.
    fireEvent.keyDown(motion.viewport, { key: "PageUp" });
    fireEvent.keyDown(motion.viewport, { key: "End" });
    motion.viewport.scrollTop = 900;
    fireEvent.scroll(motion.viewport);
    motion.grow(100);
  });
  motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(900);
  expect(motion.pending()).toBeGreaterThan(0);
  const writes = motion.writes.length;
  motion.tick(16);
  expect(motion.writes.length - writes, "one follow step per delivered frame").toBe(1);
  expect(motion.viewport.scrollTop).toBeGreaterThan(900);
  expect(motion.viewport.scrollTop).toBeLessThan(1000);
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(1000);
  expect(motion.pending()).toBe(0);
});

it("consumes the final height change delivered inside a finishing scroll write", () => {
  const motion = motionViewport();
  motion.grow(2);
  motion.afterScroll(() => { motion.afterScroll(); motion.grow(100); });
  motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(602);
  expect(motion.pending()).toBe(1);
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(702);
  expect(motion.pending()).toBe(0);
});

it.each(["before commit", "after successor commit"])("ignores an already taken follow callback after A→B→A %s", (delivery) => {
  const motion = motionViewport();
  motion.grow(300);
  const taken = motion.takeFrames();
  expect(taken.length).toBeGreaterThan(0);
  const stopped = motion.writes.length;
  act(() => {
    useSessionStore.getState().setActiveSessionId("other-motion");
    useSessionStore.getState().setActiveSessionId(session.id);
    if (delivery === "before commit") for (const callback of taken) callback(motion.now() - 40);
    expect(motion.writes).toHaveLength(stopped);
  });
  if (delivery === "after successor commit") act(() => {
    for (const callback of taken) callback(motion.now() - 40);
    expect(motion.writes).toHaveLength(stopped);
  });
  // A current replacement window positions instantly, then later output can
  // again move continuously. Old cleanup must leave both handoffs usable.
  for (let index = 0; index < 8; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(900);
  motion.grow(100); motion.tick(16);
  expect(motion.viewport.scrollTop).toBeGreaterThan(900);
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(1000);
  expect(motion.pending()).toBe(0);
});

it("uses the browser's shrink clamp without reversing toward an old target", () => {
  const motion = motionViewport();
  motion.grow(300); motion.tick(16);
  motion.grow(-500);
  expect(motion.viewport.scrollTop).toBe(400);
  const clamped = motion.writes.length;
  for (let index = 0; index < 8; index++) motion.tick(16);
  expect(motion.writes).toHaveLength(clamped);
  expect(motion.pending()).toBe(0);
  motion.grow(100); motion.tick(16);
  expect(motion.viewport.scrollTop).toBeGreaterThan(400);
  expect(motion.viewport.scrollTop).toBeLessThan(500);
});

it("applies a changed reduced-motion preference during the active movement", () => {
  let reduced = false;
  const query = vi.fn((media: string) => ({ media, get matches() {
    return media === "(prefers-reduced-motion: reduce)" && reduced;
  } }));
  vi.stubGlobal("matchMedia", query);
  const motion = motionViewport();
  motion.grow(300); motion.tick(16);
  expect(motion.viewport.scrollTop).toBeGreaterThan(600);
  expect(motion.viewport.scrollTop).toBeLessThan(900);
  reduced = true; motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(900);
  motion.tick(0);
  expect(motion.pending()).toBe(0);
  reduced = false; motion.grow(100); motion.tick(16);
  expect(motion.viewport.scrollTop).toBeGreaterThan(900);
  expect(motion.viewport.scrollTop).toBeLessThan(1000);
  expect(query.mock.calls.filter(([media]) => media === "(prefers-reduced-motion: reduce)")).toHaveLength(1);
});

it.each([0, -1])("retains an unfinished target without writing when execution time advances by %sms", (dt) => {
  const motion = motionViewport();
  motion.grow(100); motion.tick(dt);
  expect(motion.writes).toHaveLength(0);
  expect(motion.pending(), "unfinished output retains one next frame").toBe(1);
  motion.tick(0);
  expect(motion.writes).toHaveLength(0);
  expect(motion.pending()).toBe(1);
  // No further ResizeObserver delivery: the final target must finish itself
  // when the execution clock advances, even after equal/coarse readings.
  motion.tick(16);
  expect(motion.viewport.scrollTop).toBeGreaterThan(600);
  for (let index = 0; index < 30; index++) motion.tick(16);
  expect(motion.viewport.scrollTop).toBe(700);
  expect(motion.pending()).toBe(0);
  const settled = motion.writes.length;
  for (let index = 0; index < 8; index++) motion.tick(16);
  expect(motion.writes).toHaveLength(settled);
});

it("leaves no queued follow frame at the completed bottom even with an equal clock reading", () => {
  const motion = motionViewport();
  motion.grow(0); motion.tick(0);
  expect(motion.writes).toHaveLength(0);
  expect(motion.pending()).toBe(0);
});

it("removes its visibility listener and rejects an already taken frame after unmount", () => {
  const added = vi.spyOn(document, "addEventListener");
  const removed = vi.spyOn(document, "removeEventListener");
  const motion = motionViewport();
  const listeners = added.mock.calls.filter(([type]) => type === "visibilitychange");
  expect(listeners.length).toBeGreaterThan(0);
  motion.grow(300);
  const taken = motion.takeFrames();
  expect(taken.length).toBeGreaterThan(0);
  motion.unmount();
  for (const [, registered] of listeners) {
    expect(removed.mock.calls.filter(([type, listener]) => type === "visibilitychange" && listener === registered)).toHaveLength(1);
  }
  act(() => { for (const callback of taken) callback(motion.now()); });
  motion.visibility(false); motion.grow(100); motion.tick(16);
  expect(motion.writes).toHaveLength(0);
  expect(motion.pending()).toBe(0);
});

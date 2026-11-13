import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";

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
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

// Only browser dimensions, delivery and time are controlled. The real ChatView,
// store and public Virtuoso mock context still own rendering and following.
function motionViewport(bottomRemainder = 0) {
  let time = 1000;
  let height = 1200;
  let viewportHeight = 600;
  let hidden = false;
  let serial = 0;
  let afterScroll: (() => void) | undefined;
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
  const view = render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()}
      connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>);
  const viewport = view.container.querySelector<HTMLElement>(".chat-messages")!;
  const writes: { at: number; top: number }[] = [];
  viewport.scrollTo = (optionsOrX?: ScrollToOptions | number, y?: number) => {
    const top = typeof optionsOrX === "number" ? y ?? 0 : optionsOrX?.top ?? 0;
    viewport.scrollTop = Math.max(0, Math.min(height - viewportHeight - bottomRemainder, Math.round(top)));
    writes.push({ at: time, top: viewport.scrollTop });
    fireEvent.scroll(viewport);
    afterScroll?.();
  };
  const tick = (dt: number) => {
    time += dt;
    const batch = [...queued.values()]; queued.clear();
    // A frame timeline can precede the request/execution clock. Preserve the
    // observed ordering rather than making the platform imply a positive dt.
    act(() => { for (const callback of batch) callback(time - 40); });
  };
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
  for (let index = 0; index < 8; index++) tick(16);
  expect(viewport.scrollTop, "initial positioning uses the physical bottom").toBe(600 - bottomRemainder);
  expect(view.getByText("Saved text 15"), "the initial target participates in the rendered list").toBeVisible();
  writes.length = 0;
  return { viewport, writes, tick,
    grow: (amount: number) => {
      height += amount;
      viewport.scrollTop = Math.min(viewport.scrollTop, Math.max(0, height - viewportHeight - bottomRemainder));
      notify();
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

it("stops immediately while the page is hidden and resumes with a fresh time origin", () => {
  const motion = motionViewport();
  motion.grow(300); motion.tick(16);
  const readingTop = motion.viewport.scrollTop;
  expect(readingTop).toBeGreaterThan(600);
  expect(readingTop).toBeLessThan(900);
  expect(motion.pending()).toBeGreaterThan(0);
  motion.visibility(true);
  expect(motion.pending(), "hiding cancels without waiting for a background frame").toBe(0);
  const stopped = motion.writes.length;
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

it("retires a zero-height viewport's motion at measurement and resumes from its new geometry", () => {
  const motion = motionViewport();
  motion.grow(300); motion.tick(16);
  const top = motion.viewport.scrollTop;
  motion.resize(0);
  expect(motion.pending(), "a zero-height measurement stops without a future frame").toBe(0);
  const stopped = motion.writes.length;
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
  expect(motion.pending()).toBe(1);
  motion.tick(16);
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
  expect(listeners).toHaveLength(1);
  motion.grow(300);
  const taken = motion.takeFrames();
  expect(taken.length).toBeGreaterThan(0);
  motion.unmount();
  expect(removed.mock.calls.filter(([type, listener]) => type === "visibilitychange" && listener === listeners[0][1])).toHaveLength(1);
  act(() => { for (const callback of taken) callback(motion.now()); });
  motion.visibility(false); motion.grow(100); motion.tick(16);
  expect(motion.writes).toHaveLength(0);
  expect(motion.pending()).toBe(0);
});

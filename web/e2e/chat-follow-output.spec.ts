import { expect, test, type Page, type WebSocketRoute } from "./test";
import { enterAccountCredentials } from "./current-account";
import type { Message, PendingQuestion } from "../src/stores/sessionStore";
import type { RuntimeMetadata } from "../src/api";
import type { Locator, Route } from "@playwright/test";
import { capturePaint } from "./paint-capture";

// Chromium headless hides native scrollbars by default. A real thumb drag
// needs the actual scrollbar, rather than an empty CSS-reserved gutter.
test.use({ launchOptions: { ignoreDefaultArgs: ["--hide-scrollbars"] } });

const identity = (thread: string, item: string): RuntimeMetadata => ({ request_id: "request", attempt_id: "attempt", native: {
  thread_id: thread, turn_id: "turn", item_id: item, root_thread_id: "root", root_turn_id: "turn",
} });
const helper = { card_id: "helper-card", task_id: "helper", native_thread_id: "helper", native_turn_id: "turn",
  runtime_metadata: identity("helper", "item"), status: "running", name: "Helper", description: "Reading", prompt: "", summary: "", steps: [] };

async function trackTop(locator: Locator) {
  return locator.evaluateHandle((element) => {
    const top = element.getBoundingClientRect().top;
    let maximum = 0;
    let frame = 0;
    const sample = () => {
      maximum = element.isConnected ? Math.max(maximum, Math.abs(element.getBoundingClientRect().top - top)) : Infinity;
      frame = requestAnimationFrame(sample);
    };
    frame = requestAnimationFrame(sample);
    return { stop: () => { cancelAnimationFrame(frame); return maximum; } };
  });
}

function scrollbarThumb(element: Element) {
  const viewport = element as HTMLElement;
  const box = viewport.getBoundingClientRect();
  const gutter = viewport.offsetWidth - viewport.clientWidth;
  const height = viewport.clientHeight ** 2 / viewport.scrollHeight;
  return { x: box.right - Math.max(2, gutter / 2),
    y: box.top + viewport.clientHeight * viewport.scrollTop / viewport.scrollHeight + height / 2, gutter };
}

async function expectFooterMarkerInViewport(scroller: Locator) {
  // Streaming text is deliberately plain. Measure its final text range rather
  // than looking for a Markdown heading or accepting a tall ancestor's overlap.
  await expect.poll(() => scroller.evaluate((viewport) => {
    const text = viewport.querySelector(".msg-streaming-text")?.firstChild;
    const marker = "Static Footer final marker";
    const index = text?.textContent?.lastIndexOf(marker) ?? -1;
    if (!text || index < 0) return false;
    const range = document.createRange();
    range.setStart(text, index); range.setEnd(text, index + marker.length);
    const bounds = range.getBoundingClientRect();
    const visible = viewport.getBoundingClientRect();
    return bounds.height > 0 && bounds.top >= visible.top && bounds.bottom <= visible.bottom + 1;
  })).toBe(true);
}

function holdAnimationFrames(page: Page) {
  return page.evaluateHandle(() => {
    const request = window.requestAnimationFrame, cancel = window.cancelAnimationFrame;
    let serial = 0;
    const pending = new Map<number, FrameRequestCallback>();
    window.requestAnimationFrame = (callback) => { const id = --serial; pending.set(id, callback); return id; };
    window.cancelAnimationFrame = (id) => { if (id < 0) pending.delete(id); else cancel(id); };
    return { count: () => pending.size, flush: () => {
      const batch = [...pending.values()]; pending.clear();
      for (const callback of batch) callback(performance.now());
    }, release: () => {
      window.requestAnimationFrame = request; window.cancelAnimationFrame = cancel;
      for (const callback of pending.values()) request(callback);
      pending.clear();
    } };
  });
}

async function revealWithHeldFrames(scroller: Locator, gate: Awaited<ReturnType<typeof holdAnimationFrames>>) {
  for (let frame = 0; frame < 24; frame++) {
    await gate.evaluate((queued) => queued.flush());
    if (await scroller.evaluate((element) => getComputedStyle(element).visibility === "visible")) return true;
  }
  return false;
}

function controlledTouch(scroller: Locator) {
  return scroller.evaluateHandle((viewport) => ({
    viewport,
    emit(type: "touchstart" | "touchmove" | "touchend", clientY: number) {
      const point = new Touch({ identifier: 71, target: viewport, clientX: 400, clientY });
      viewport.dispatchEvent(new TouchEvent(type, { bubbles: true,
        touches: type === "touchend" ? [] : [point], changedTouches: [point] }));
    },
  }));
}

function holdNativeScroll(scroller: Locator) {
  return scroller.evaluateHandle((viewport) => {
    const suppress = (event: Event) => {
      if (event.target === viewport && event.isTrusted) event.stopImmediatePropagation();
    };
    document.addEventListener("scroll", suppress, true);
    return { release: () => document.removeEventListener("scroll", suppress, true) };
  });
}

interface ChatSnapshot {
  messages: Message[];
  next_message_seq: number;
  oldest_loaded_seq: number | null;
  has_more_messages: boolean;
}

async function openChat(page: Page, history: number | ChatSnapshot = 40, pending: PendingQuestion[] = [], beforeSelect?: () => Promise<void>,
  content = (index: number) => `Saved row ${index + 1}`) {
  const session = { id: "follow-output", agent_id: "follow-agent", name: "Following output", backend: "codex",
    status: "running", archived: false, working_dir: "/tmp", created_at: "2026-11-12T00:00:00Z" };
  const count = typeof history === "number" ? history : history.messages.length;
  let snapshot: ChatSnapshot = typeof history === "number" ? {
    messages: Array.from({ length: count }, (_, index) => ({
      role: "assistant", type: "text", seq: index + 1, content: content(index),
    })),
    next_message_seq: count ? count + 1 : 0, oldest_loaded_seq: count ? 1 : null, has_more_messages: false,
  } : history;
  const pages: Route[] = [];
  const submissions: Array<{ content: string; client_request_id: string }> = [];
  let socket!: WebSocketRoute;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) { await route.continue(); return; }
    let data: unknown = [];
    if (path === "/api/auth/state") data = { accounts_exist: true };
    else if (path === "/api/auth/login") data = { token: "follow-token" };
    else if (path === "/api/auth/identity") data = { user_id: "follow-user", label: "Reader", is_admin: false };
    else if (path === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend: "codex", pinned: true, is_system: true }];
    else if (path === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
    else if (path === "/api/sessions") data = [session];
    else if (path.endsWith("/submissions")) data = { items: [], next_cursor: null };
    else if (path === `/api/sessions/${session.id}/messages`) { pages.push(route); return; }
    else if (path.startsWith(`/api/sessions/${session.id}/native-details/`)) data = {
      anchor_seq: 41, child: helper, availability: "not_collected", messages: [], next_after_seq: null, has_more: false,
    };
    else if (path === `/api/sessions/${session.id}`) data = { ...session,
      active_attempt: { request_id: "request", attempt_id: "attempt" },
      ...snapshot, pending_questions: pending, subagents: [],
      dispatch_state: { generation: "follow-service", revision: 1, paused: false, active_request_id: "request", active_operation: "message", items: [] },
    };
    await route.fulfill({ json: data });
  });
  await page.routeWebSocket("**/ws?*", (ws) => {
    socket = ws;
    ws.onMessage((payload) => {
      const message = JSON.parse(String(payload));
      if (message.type === "send_message") submissions.push(message);
    });
  });
  await page.goto("/");
  await enterAccountCredentials(page);
  await page.locator(".agent-item", { hasText: "Octo" }).click();
  await beforeSelect?.();
  await page.locator(".session-item", { hasText: session.name }).click();
  await expect(page.locator(".conn-status.on")).toBeVisible();
  const scroller = page.locator(".chat-messages");
  const gap = () => scroller.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop);
  const settle = () => scroller.evaluate(async () => {
    for (let frame = 0; frame < 24; frame++) await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
  });
  if (!beforeSelect && typeof history === "number") {
    await expect(scroller.getByText(`Saved row ${count}`, { exact: true })).toBeVisible();
    await expect.poll(gap).toBeLessThanOrEqual(2);
  }
  const send = (event: Record<string, unknown>) => socket.send(JSON.stringify({ ...event, session_id: session.id }));
  const output = (label: string) => Array.from({ length: 12 }, (_, index) => `${label} line ${index + 1}`).join("\n") + "\n";
  const replaceOnReconnect = (next: typeof snapshot) => {
    snapshot = next;
    socket.close({ code: 1012, reason: "Read a replacement transcript window" });
  };
  return { scroller, gap, send, settle, output, session, pages, replaceOnReconnect, submissions };
}

const snoozedQuestionEvent = (seq: number): Message => ({ role: "system", type: "question_snoozed", seq,
  tool_use_id: `optional-question-${seq}`, content: "Automatic question expiry paused while you answer.",
  tool_input: { deadline: null, snoozed: true } });

test("hidden question events leave history readable on first reveal, paging and reconnect", async ({ page }) => {
  const row = (seq: number): Message => ({ role: "assistant", type: "text", seq, content: `Saved row ${seq}` });
  const messages = Array.from({ length: 60 }, (_, index) => {
    const seq = index + 100;
    return seq === 100 || seq === 138 || seq >= 140 ? snoozedQuestionEvent(seq) : row(seq);
  });
  let gate!: Awaited<ReturnType<typeof holdAnimationFrames>>;
  const { scroller, pages, replaceOnReconnect } = await openChat(page, {
    messages, next_message_seq: 160, oldest_loaded_seq: 100, has_more_messages: true,
  }, [], async () => { gate = await holdAnimationFrames(page); });
  try {
    await expect(scroller).toBeAttached();
    expect(await scroller.evaluate((element) => getComputedStyle(element).visibility)).toBe("hidden");
    expect(await revealWithHeldFrames(scroller, gate)).toBe(true);
    await expect(scroller.getByText("Saved row 139", { exact: true })).toBeInViewport({ ratio: 1 });
    await expect(scroller.locator('[data-message-key="message:138"], [data-message-key="message:159"]')).toHaveCount(0);
  } finally { await gate.evaluate((queued) => queued.release()); await gate.dispose(); }

  await scroller.press("Home");
  await expect.poll(() => pages.length).toBe(1);
  expect(new URL(pages[0].request().url()).searchParams.get("before_seq")).toBe("100");
  const recentStart = scroller.getByText("Saved row 101", { exact: true });
  await expect(recentStart).toBeInViewport();
  const listHeight = () => scroller.evaluate((element) => element.scrollHeight);
  const readingOffset = () => recentStart.evaluate((element) =>
    element.getBoundingClientRect().top - element.closest(".chat-messages")!.getBoundingClientRect().top);
  const beforePageHeight = await listHeight();
  const beforePageOffset = await readingOffset();
  await pages[0].fulfill({ json: {
    messages: [snoozedQuestionEvent(80), ...Array.from({ length: 19 }, (_, index) => row(81 + index))],
    oldest_loaded_seq: 80, has_more_messages: false,
  } });
  await expect.poll(listHeight).toBeGreaterThan(beforePageHeight + 100);
  await expect(recentStart).toBeInViewport();
  await expect.poll(async () => Math.abs(await readingOffset() - beforePageOffset)).toBeLessThanOrEqual(2);

  replaceOnReconnect({ messages: [...messages, row(160), snoozedQuestionEvent(161)],
    next_message_seq: 162, oldest_loaded_seq: 100, has_more_messages: true });
  await page.getByRole("button", { name: "Jump to latest", exact: true }).click();
  await expect(scroller.getByText("Saved row 160", { exact: true })).toBeInViewport({ ratio: 1 });
  await expect(scroller.locator('[data-message-key="message:161"]')).toHaveCount(0);
  await scroller.press("Home");
  await expect(scroller.getByText("Saved row 81", { exact: true })).toBeInViewport();
  await expect(scroller.locator('[data-message-key="message:80"]')).toHaveCount(0);
  expect(pages).toHaveLength(1);
});

for (const [native, selected, zoom] of [
  [false, false, 1], [false, true, 1], [true, false, 1], [true, true, 1], [false, true, 2.25],
] as const) {
  const source = native ? "native item" : "root text";
  test(`saving a live reply preserves the ${selected ? "selected" : "reading"} paragraph (${source}${zoom === 1 ? "" : `, CSS zoom ${zoom}`})`, async ({ page }, info) => {
    const { scroller, gap, send, settle } = await openChat(page);
    if (zoom !== 1) {
      await scroller.evaluate((element, scale) => { element.style.zoom = String(scale); }, zoom);
      await settle();
    }
    const content = Array.from({ length: 150 }, (_, index) =>
      `Section ${index + 100} contains part of the same reply, with enough words to wrap across the viewport.`).join(" ");
    const runtime = native ? { runtime_metadata: identity("root", "root-item") } : {};
    send({ type: "assistant_delta", content, ...runtime });
    await expect(scroller.getByTestId("streaming-text")).toContainText("Section 249");
    await expect.poll(gap).toBeLessThanOrEqual(2);
    if (!selected) {
      await scroller.hover();
      await page.mouse.wheel(0, -1120);
      await expect.poll(gap).toBeGreaterThan(900);
      await settle();
    }
    await capturePaint(page, info, async (paintFrames) => {
      const reading = await scroller.evaluateHandle((viewport, { selected, zoom }) => {
        // Move inside the same tall live reply without creating an upward
        // input. This isolates the selection pause from history intent.
        if (selected) viewport.scrollTop -= 1120;
        const bounds = viewport.getBoundingClientRect();
        const live = viewport.querySelector(".msg-streaming-text")?.firstChild;
        if (!live?.textContent) throw new Error("Live reply was not mounted");
        const range = document.createRange();
        const words = [...live.textContent.matchAll(/Section \d{3}/g)];
        const marker = words.find(({ index, 0: text }) => {
          range.setStart(live, index!);
          range.setEnd(live, index! + text.length);
          const rect = range.getBoundingClientRect();
          return rect.top > bounds.top + 100 && rect.bottom < bounds.bottom - 100;
        });
        if (!marker) throw new Error("No live text at the current reading position");
        if (selected) {
          const selection = window.getSelection()!;
          selection.removeAllRanges();
          selection.addRange(range.cloneRange());
        }
        const paragraph = range.getBoundingClientRect();
        const offset = paragraph.top - bounds.top;
        const getOffset = (node: Node | null | undefined) => {
          if (!node?.textContent?.includes(marker[0])) return null;
          const index = node.textContent.indexOf(marker[0]);
          range.setStart(node, index);
          range.setEnd(node, index + marker[0].length);
          return range.getBoundingClientRect().top - viewport.getBoundingClientRect().top - offset;
        };
        const savedText = () => viewport.querySelector('[data-message-key="message:41"] .msg-content p')?.firstChild;
        const nativeFrame = window.requestAnimationFrame;
        let requested = 0;
        if (zoom !== 1) window.requestAnimationFrame = (callback) => { requested++; return nativeFrame(callback); };
        const samples: { at: number; saved: boolean; delta: number | null; requested: number }[] = [];
        let frame = nativeFrame(function sample(at) {
          const saved = savedText();
          samples.push({ at, saved: !!saved, delta: getOffset(saved ?? viewport.querySelector(".msg-streaming-text")?.firstChild), requested });
          frame = nativeFrame(sample);
        });
        return { paragraph: paragraph.toJSON(), currentOffset: () => getOffset(savedText()), quietFrames: async () => {
          for (let i = 0; i < 24; i++) await new Promise<void>((done) => nativeFrame(() => done()));
        }, stop: () => {
          cancelAnimationFrame(frame);
          if (zoom !== 1) window.requestAnimationFrame = nativeFrame;
          return samples;
        } };
      }, { selected, zoom });
      let evidence;
      let savingAt: number | undefined;
      try {
        await settle();
        if (selected) await expect(page.getByRole("button", { name: "Jump to latest" })).toHaveCount(0);
        savingAt = await page.evaluate(() => performance.timeOrigin + performance.now());
        send({ type: "assistant_text", seq: 41, content, ...runtime });
        await expect(scroller.getByTestId("streaming-text")).toHaveCount(0);
        await expect.poll(() => reading.evaluate((frame) => Math.abs(frame.currentOffset() ?? Infinity))).toBeLessThanOrEqual(2);
        await expect.poll(gap).toBeGreaterThan(900);
        if (selected) await expect.poll(() => page.evaluate(() => window.getSelection()?.toString())).toMatch(/^Section \d{3}$/);
        if (zoom === 1) await settle();
        else await reading.evaluate((probe) => probe.quietFrames());
      } finally {
        evidence = await reading.evaluate((frame) => ({ samples: frame.stop(), paragraph: frame.paragraph }));
        await reading.dispose();
        await info.attach("live-transfer-offsets", { body: JSON.stringify({ ...evidence, savingAt, paintFrames }), contentType: "application/json" });
      }
      if (selected) expect(Math.abs(evidence.samples.find((sample) => sample.saved)?.delta ?? Infinity),
        "the saved text first appears at the selected paragraph").toBeLessThanOrEqual(2);
      else expect(evidence.samples.filter(({ delta }) => delta === null || Math.abs(delta) > 2),
        "the same paragraph stays in view through the transfer").toEqual([]);
      if (zoom !== 1) {
        const settled = evidence.samples.slice(-8);
        expect(settled).toHaveLength(8);
        expect(settled.every(({ requested }) => requested === settled[0].requested),
          "the page stops requesting frames after the selected paragraph settles").toBe(true);
      }
    });
    if (selected) {
      await page.evaluate(() => window.getSelection()?.removeAllRanges());
      await expect.poll(gap).toBeLessThanOrEqual(2);
    }
  });
}

for (const opening of ["no displayed rows", "hidden question history", "saved history"]) {
test(`a static tall Footer completes first presentation with ${opening}`, async ({ page }) => {
  const hasDisplayedRows = opening === "saved history";
  const history = opening === "hidden question history" ? {
    messages: [snoozedQuestionEvent(40), snoozedQuestionEvent(41)],
    next_message_seq: 42, oldest_loaded_seq: 40, has_more_messages: false,
  } : hasDisplayedRows ? 40 : 0;
  let gate!: Awaited<ReturnType<typeof holdAnimationFrames>>;
  const { scroller, gap, send, settle } = await openChat(page, history, [], async () => {
    gate = await holdAnimationFrames(page);
  });
  try {
    expect(await scroller.evaluate((element) => getComputedStyle(element).visibility)).toBe("hidden");
    send({ type: "assistant_delta", content: "Static output\n\n".repeat(80) + "### Static Footer final marker" });
    await expect(scroller.getByTestId("streaming-text")).toContainText("Static Footer final marker");
    let beforeCorrection: number | undefined;
    if (!hasDisplayedRows) {
      const nearTail = await scroller.evaluate((viewport) => {
        const footer = viewport.querySelector<HTMLElement>("[data-chat-footer]")!.parentElement!;
        const text = footer.querySelector<HTMLElement>(".msg-streaming-text")!;
        text.style.fontSize = "15.5px";
        text.style.lineHeight = "23.375px";
        const bottom = footer.getBoundingClientRect().bottom - viewport.getBoundingClientRect().top;
        // Place the browser one rounded pixel short with a fractional Footer
        // edge that still needs a real scroll correction.
        footer.style.paddingBottom = `${(Math.ceil(bottom) - bottom + 0.25) % 1}px`;
        viewport.scrollTop = viewport.scrollHeight - viewport.clientHeight - 1;
        return { top: viewport.scrollTop, gap: viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop,
          footerOverhang: footer.getBoundingClientRect().bottom - viewport.getBoundingClientRect().bottom };
      });
      expect(nearTail.gap).toBe(1);
      expect(nearTail.footerOverhang).toBeGreaterThan(1);
      beforeCorrection = nearTail.top;
    }
    expect(await revealWithHeldFrames(scroller, gate), "the last output does not require another token or an idle interval").toBe(true);
    expect(await page.locator("[data-chat-timeout]").count()).toBe(0);
    if (beforeCorrection !== undefined) {
      expect(await scroller.evaluate((viewport) => viewport.scrollTop)).toBeGreaterThan(beforeCorrection);
    }
    expect(await gap()).toBeLessThanOrEqual(1);
    await expectFooterMarkerInViewport(scroller);
    if (!hasDisplayedRows) await expect(scroller.locator("[data-message-key]")).toHaveCount(0);
    if (hasDisplayedRows) {
      // Let Chromium deliver the first upward wheel with the reveal's
      // positioning callback still queued. Late output arrives in that gap.
      expect(await gate.evaluate((queued) => queued.count())).toBeGreaterThan(0);
      const before = await scroller.evaluate((element) => element.scrollTop);
      const box = (await scroller.boundingBox())!;
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.wheel(0, -320);
      const top = () => scroller.evaluate((element) => element.scrollTop);
      await expect.poll(top).toBeLessThan(before - 200);
      const reading = await top();
      send({ type: "assistant_delta", content: "\n\n" + "More output after the first wheel.\n\n".repeat(40) });
      await expect(scroller.getByTestId("streaming-text")).toContainText("More output after the first wheel.");
      expect(await gap()).toBeGreaterThan(700);
      expect(Math.abs(await top() - reading)).toBeLessThanOrEqual(2);
      await gate.evaluate((queued) => queued.release());
      await settle();
      expect(Math.abs(await top() - reading)).toBeLessThanOrEqual(2);
    }
  } finally { await gate.evaluate((queued) => queued.release()); await gate.dispose(); }
});
}

test("first presentation follows a live reply that becomes a saved row before reveal", async ({ page }) => {
  let gate!: Awaited<ReturnType<typeof holdAnimationFrames>>;
  const { scroller, send } = await openChat(page, 0, [], async () => {
    gate = await holdAnimationFrames(page);
    await page.addStyleTag({ content: ".chat-messages { zoom: 1.1; }" });
  });
  try {
    const content = "A pending reply\n\n".repeat(60) + "### Saved before reveal";
    send({ type: "assistant_delta", content });
    await expect(scroller.getByTestId("streaming-text")).toContainText("Saved before reveal");
    send({ type: "assistant_text", seq: 1, content });
    await expect(scroller.getByTestId("streaming-text")).toHaveCount(0);
    expect(await scroller.evaluate((viewport) => getComputedStyle(viewport).visibility)).toBe("hidden");
    await expect.poll(async () => {
      await gate.evaluate((queued) => queued.flush());
      return scroller.evaluate((viewport) => getComputedStyle(viewport).visibility);
    }, { intervals: [0, 25, 50], timeout: 1200 }).toBe("visible");
    const marker = scroller.locator('[data-message-key="message:1"] h3');
    const first = await marker.evaluate((heading) => {
      const rect = heading.getBoundingClientRect();
      const clip = heading.closest(".chat-messages")!.getBoundingClientRect();
      return rect.top >= clip.top && rect.bottom <= clip.bottom + 1;
    });
    expect(first).toBe(true);
    await expect(page.locator("[data-chat-timeout]")).toHaveCount(0);
  } finally { await gate.evaluate((queued) => queued.release()); await gate.dispose(); }
});

test("initial positioning timeout leaves loaded history available and Jump to latest restores the tail", async ({ page }) => {
  let gate!: Awaited<ReturnType<typeof holdAnimationFrames>>;
  const { scroller, gap } = await openChat(page, 40, [], async () => { gate = await holdAnimationFrames(page); });
  try {
    expect(await scroller.evaluate((viewport) => getComputedStyle(viewport).visibility)).toBe("hidden");
    await expect(page.getByText("Automatic positioning did not finish. Loaded content is available.")).toBeVisible();
    await expect(scroller).toBeVisible();
    await gate.evaluate((queued) => queued.release());
    await expect(scroller.getByText("Saved row 1", { exact: true })).toBeInViewport();
    expect(await gap()).toBeGreaterThan(100);
    await page.getByRole("button", { name: "Dismiss positioning notice" }).click();
    await expect(page.locator("[data-chat-timeout]")).toHaveCount(0);
    await page.getByRole("button", { name: "Jump to latest" }).click();
    await expect(scroller.getByText("Saved row 40", { exact: true })).toBeInViewport();
    await expect.poll(gap).toBeLessThanOrEqual(2);
    await expect(page.locator("[data-chat-timeout]")).toHaveCount(0);
  } finally { await gate.evaluate((queued) => queued.release()); await gate.dispose(); }
});

for (const short of [true, false]) {
  test(`a zoomed transcript with a native horizontal scrollbar ${short ? "reveals without timing out" : "keeps its tail through the handoff to following"}`,
    async ({ page }, info) => {
      const count = short ? 2 : 80;
      const marker = "Last paragraph under zoom";
      let gate!: Awaited<ReturnType<typeof holdAnimationFrames>>;
      const { scroller, settle } = await openChat(page, count, [], async () => {
        gate = await holdAnimationFrames(page);
        await page.addStyleTag({ content: `
          .chat-messages { zoom: 1.1; flex: none; height: ${short ? 101.5625 : 480.3125}px !important; }
          .chat-messages .msg-content { overflow-wrap: normal; }
        ` });
      }, (index) => index === count - 1 ? `${"wide".repeat(300)}\n\n${marker}`
        : `History ${index}\n\n${"A saved paragraph.\n\n".repeat(index % 4)}`);
      try {
        const reading = () => scroller.evaluate((viewport, marker) => {
          const box = viewport.getBoundingClientRect();
          const clipHeight = viewport.querySelector('[data-viewport-type="element"]')!.getBoundingClientRect().height;
          const clipBottom = box.top + clipHeight;
          const content = [...viewport.querySelectorAll(".msg-content p")].find((node) => node.textContent === marker);
          const text = content?.lastChild;
          const range = document.createRange();
          if (text instanceof Text && text.length) {
            range.setStart(text, text.length - 1);
            range.setEnd(text, text.length);
          }
          const rect = range.collapsed ? undefined : range.getBoundingClientRect();
          return {
            horizontalScrollbar: viewport.scrollWidth > viewport.clientWidth && box.bottom > clipBottom + 1,
            tailInView: !!rect && rect.top >= box.top && rect.bottom <= clipBottom + 1,
            top: viewport.scrollTop, clipHeight,
            footerOverhang: viewport.querySelector("[data-chat-footer]")!.parentElement!.getBoundingClientRect().bottom - clipBottom,
            markerTop: rect ? rect.top - box.top : null, markerOverhang: rect ? rect.bottom - clipBottom : null,
          };
        }, marker);
        await expect.poll(async () => {
          await gate.evaluate((queued) => queued.flush());
          return scroller.evaluate((viewport) => getComputedStyle(viewport).visibility);
        }, { intervals: [0, 25, 50], timeout: 1400 }).toBe("visible");
        const reveal = await reading();
        await info.attach("scrollbar-reveal", { body: JSON.stringify(reveal), contentType: "application/json" });
        expect(reveal, JSON.stringify(reveal)).toMatchObject({ horizontalScrollbar: true, tailInView: true });
        await expect(page.locator("[data-chat-timeout]")).toHaveCount(0);

        const writes = await scroller.evaluateHandle((viewport) => {
          const scrollTo = viewport.scrollTo;
          const native = scrollTo.bind(viewport);
          let count = 0;
          viewport.scrollTo = (options?: ScrollToOptions | number, y?: number) => {
            count++;
            if (typeof options === "number") native(options, y ?? 0);
            else native(options);
          };
          return { count: () => count, restore: () => { viewport.scrollTo = scrollTo; } };
        });
        try {
          await gate.evaluate((queued) => queued.release());
          await settle();
          const following = await reading();
          await info.attach("scrollbar-following", { body: JSON.stringify(following), contentType: "application/json" });
          expect(following).toMatchObject({ horizontalScrollbar: true, tailInView: true });
          const settledWrites = await writes.evaluate((scroll) => scroll.count());
          await settle();
          expect(await writes.evaluate((scroll) => scroll.count())).toBe(settledWrites);
        } finally { await writes.evaluate((scroll) => scroll.restore()); await writes.dispose(); }
      } finally { await gate.evaluate((queued) => queued.release()); await gate.dispose(); }
    });
}

test("return to latest consumes an unchanged tail measurement after detail refresh under a static tall Footer", async ({ page }) => {
  const { scroller, gap, send, settle, session, replaceOnReconnect } = await openChat(page);
  await scroller.press("Home");
  await expect(scroller.getByText("Saved row 1", { exact: true })).toBeInViewport();
  const snapshot = { messages: [{ role: "assistant" as const, type: "text", seq: 200, content: "Unchanged saved tail" }],
    oldest_loaded_seq: 200, next_message_seq: 201, has_more_messages: false };
  replaceOnReconnect(snapshot);
  await expect(page.getByText(/The previous reading position is unavailable/)).toBeVisible();
  send({ type: "assistant_delta", content: "Static output\n\n".repeat(80) + "### Static Footer final marker" });
  await expect(scroller.getByTestId("streaming-text")).toContainText("Static Footer final marker");
  await settle();
  const refreshed = page.waitForResponse((response) => new URL(response.url()).pathname === `/api/sessions/${session.id}`);
  replaceOnReconnect(snapshot);
  await refreshed;
  await settle();
  await page.getByRole("button", { name: "Jump to latest" }).click();
  await expect.poll(gap).toBeLessThanOrEqual(1);
  await expectFooterMarkerInViewport(scroller);
});

test("a nonempty replacement window uses the current list and keeps history usable", async ({ page }) => {
  const { scroller, gap, send, settle, pages, replaceOnReconnect } = await openChat(page);
  const original = await scroller.elementHandle();
  await scroller.press("Home");
  await expect(scroller.getByText("Saved row 1", { exact: true })).toBeVisible();
  const row = (seq: number): Message => ({ role: "assistant", type: "text", seq, content: `Replacement row ${seq}` });
  replaceOnReconnect({ messages: Array.from({ length: 60 }, (_, index) => row(index + 200)),
    next_message_seq: 260, oldest_loaded_seq: 200, has_more_messages: true });
  await expect(page.getByText(/The previous reading position is unavailable/)).toBeVisible();
  await expect(scroller.getByText("Replacement row 200", { exact: true })).toBeInViewport();
  expect(await scroller.evaluate((element, old) => element === old, original)).toBe(true);
  const readingTop = await scroller.evaluate((element) => element.scrollTop);
  send({ type: "assistant_text", seq: 260, content: "After the replacement" });
  await settle();
  expect(await scroller.evaluate((element) => element.scrollTop)).toBe(readingTop);
  await page.getByRole("button", { name: "Jump to latest" }).click();
  await expect(scroller.getByText("After the replacement", { exact: true })).toBeVisible();
  await expect.poll(gap).toBeLessThanOrEqual(2);
  await scroller.press("Home");
  await expect.poll(() => pages.length).toBe(1);
  expect(new URL(pages[0].request().url()).searchParams.get("before_seq")).toBe("200");
  await pages[0].fulfill({ json: { messages: Array.from({ length: 15 }, (_, index) => row(index + 185)),
    oldest_loaded_seq: 185, has_more_messages: false } });
  await settle();
  await scroller.press("Home");
  await expect(scroller.getByText("Replacement row 185", { exact: true })).toBeVisible();
});

test("a tall replacement reaches its actual tail without paging before navigation", async ({ page }) => {
  const { scroller, gap, send, settle, pages, replaceOnReconnect } = await openChat(page);
  const content = "Replacement reading\n\n" + Array.from({ length: 45 }, (_, index) =>
    `### Paragraph ${index + 1}\n\nA long saved explanation keeps its reading position when new output arrives. ` +
    "这是需要连续阅读的历史正文，正在查看的段落不应因其他更新反复计算。\n\n" +
    `| name | value |\n| --- | --- |\n| item ${index + 1} | $x^2 + y^2$ |\n\n`).join("");
  replaceOnReconnect({ messages: [
    { role: "user", type: "text", seq: 200, content: "Replacement question" },
    { role: "assistant", type: "text", seq: 201, content },
    { role: "assistant", type: "text", seq: 202, content: "Replacement tail" },
  ], next_message_seq: 203, oldest_loaded_seq: 200, has_more_messages: true });
  await expect(scroller.locator('[data-message-key="message:201"]')).toBeAttached();
  await settle(); await settle();
  expect(pages, "a replacement's transient top must not request history and pause following").toHaveLength(0);
  await expect(scroller.getByText("Replacement tail", { exact: true })).toBeInViewport();
  await expect.poll(gap).toBeLessThanOrEqual(2);
  send({ type: "assistant_text", seq: 203, content: "After the tall replacement" });
  await expect(scroller.getByText("After the tall replacement", { exact: true })).toBeInViewport();
  await expect.poll(gap).toBeLessThanOrEqual(2);
  // Genuine navigation still grants paging after the replacement settles.
  await scroller.press("Home");
  await expect.poll(() => pages.length).toBe(1);
  expect(new URL(pages[0].request().url()).searchParams.get("before_seq")).toBe("200");
  await pages[0].fulfill({ json: { messages: [
    { role: "assistant", type: "text", seq: 199, content: "Earlier replacement history" },
  ], oldest_loaded_seq: 199, has_more_messages: false } });
  await settle();
});

test("an empty displayed window keeps manual history retry and later automatic paging usable", async ({ page }) => {
  const { scroller, pages, settle, replaceOnReconnect } = await openChat(page);
  replaceOnReconnect({ messages: [{ role: "assistant", type: "thinking", seq: 200, content: "Saved reasoning" }],
    next_message_seq: 201, oldest_loaded_seq: 200, has_more_messages: true });
  await expect(scroller.locator("[data-message-key]")).toHaveCount(0);
  await settle();
  expect(pages, "mount-time top must not fetch an older page").toHaveLength(0);
  const load = scroller.getByRole("button", { name: "Load earlier messages" });
  await load.click(); await expect.poll(() => pages.length).toBe(1);
  await pages[0].fulfill({ status: 503, json: { detail: "Temporary history read failure" } });
  await expect(load).toBeVisible(); await settle();
  expect(pages, "a failed read does not retry itself").toHaveLength(1);
  await load.click(); await expect.poll(() => pages.length).toBe(2);
  const row = (seq: number): Message => ({ role: "assistant", type: "text", seq, content: `Recovered row ${seq}` });
  await pages[1].fulfill({ json: { messages: Array.from({ length: 15 }, (_, index) => row(index + 185)),
    oldest_loaded_seq: 185, has_more_messages: true } });
  await settle();
  expect(pages, "manual recovery does not loop at the old top").toHaveLength(2);
  await scroller.press("End"); await settle();
  await scroller.press("Home"); await expect.poll(() => pages.length).toBe(3);
  expect(new URL(pages[2].request().url()).searchParams.get("before_seq")).toBe("185");
  await pages[2].fulfill({ json: { messages: Array.from({ length: 15 }, (_, index) => row(index + 170)),
    oldest_loaded_seq: 170, has_more_messages: false } });
  await settle(); await scroller.press("Home");
  await expect(scroller.getByText("Recovered row 170", { exact: true })).toBeVisible();
});

for (const opening of ["empty", "tall tail"] as const) {
test(`a replacement with ${opening} settles and follows its last output`, async ({ page }) => {
  const { scroller, gap, send, settle, output, replaceOnReconnect } = await openChat(page);
  const content = Array.from({ length: 80 }, (_, index) => `Saved paragraph ${index + 1}\n\n`).join("") +
    "### Initial final marker";
  replaceOnReconnect({ messages: opening === "empty" ? [] :
    [{ role: "assistant", type: "text", seq: 200, content }],
    next_message_seq: 201, oldest_loaded_seq: opening === "empty" ? 0 : 200, has_more_messages: false });
  if (opening === "empty") await expect(scroller.locator("[data-message-key]")).toHaveCount(0);
  else await expect(scroller.getByRole("heading", { name: "Initial final marker" })).toBeInViewport();
  await settle();
  await expect.poll(gap).toBeLessThanOrEqual(2);
  send({ type: "assistant_delta", content: output("Final streaming output") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("Final streaming output line 12");
  await settle(); await expect.poll(gap).toBeLessThanOrEqual(2);
  send({ type: "assistant_text", seq: 201, content: "Saved final output\n\n### Persisted final marker" });
  await expect(scroller.getByTestId("streaming-text")).toHaveCount(0);
  await settle();
  await expect(scroller.getByRole("heading", { name: "Persisted final marker" })).toBeInViewport();
  await expect.poll(gap).toBeLessThanOrEqual(2);
});
}

test("navigation before the initial target retires it through late viewport measurement", async ({ page }) => {
  let gate!: Awaited<ReturnType<typeof page.evaluateHandle<{
    flush: () => void; release: () => void; writes: () => number;
  }>>>;
  const { scroller, settle } = await openChat(page, 60, [], async () => {
    gate = await page.evaluateHandle(() => {
      const request = window.requestAnimationFrame, cancel = window.cancelAnimationFrame;
      const scroll = HTMLElement.prototype.scrollTo;
      let serial = 0, writes = 0;
      const pending = new Map<number, FrameRequestCallback>();
      HTMLElement.prototype.scrollTo = function (...args: any[]) {
        if (this.classList.contains("chat-messages") && (args[0]?.top ?? args[1]) > 50) writes++;
        (scroll as any).apply(this, args);
      };
      window.requestAnimationFrame = (callback) => { const id = --serial; pending.set(id, callback); return id; };
      window.cancelAnimationFrame = (id) => { if (id < 0) pending.delete(id); else cancel(id); };
      return { writes: () => writes,
        flush: () => { for (const id of [...pending.keys()]) { const callback = pending.get(id); pending.delete(id); callback?.(performance.now()); } },
        release: () => { window.requestAnimationFrame = request; window.cancelAnimationFrame = cancel;
          HTMLElement.prototype.scrollTo = scroll;
          for (const callback of pending.values()) request(callback); pending.clear(); },
      };
    });
  });
  try {
    await expect.poll(async () => {
      const extent = await scroller.evaluate((element) => element.scrollHeight - element.clientHeight);
      if (extent > 300) return extent;
      expect(await gate.evaluate((queued) => queued.writes())).toBe(0);
      await gate.evaluate((queued) => queued.flush());
      return scroller.evaluate((element) => element.scrollHeight - element.clientHeight);
    }).toBeGreaterThan(300);
    expect(await gate.evaluate((queued) => queued.writes())).toBe(0);
    await page.getByLabel("Conversation preparation").press("PageDown");
    const reading = await scroller.evaluate((element) => {
      element.scrollTop = 200;
      element.style.flex = "none";
      element.style.height = `${element.clientHeight - 80}px`;
      return element.scrollTop;
    });
    expect(reading).toBe(200);
    await gate.evaluate((queued) => queued.release());
    await settle();
    expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - reading)).toBeLessThanOrEqual(2);
    await expect(scroller).toBeVisible();
  } finally { await gate.evaluate((queued) => queued.release()); await gate.dispose(); }
});

test("a late downward scroll after expansion cannot restore follow intent", async ({ page }) => {
  const { scroller, send, settle, output } = await openChat(page);
  send({ type: "subagent", ...helper, seq: 41 });
  const card = scroller.locator(".subagent-card");
  await expect(card).toBeVisible();
  await settle();
  await card.locator(".subagent-summary-row").click();
  await expect(card.getByRole("region", { name: "Saved helper details" })).toBeVisible();
  await settle();
  // Simulate a late position notification from earlier application/layout
  // work, without a new downward input from the reader.
  await scroller.evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await settle();
  const reading = await scroller.evaluate((element) => element.scrollTop);
  send({ type: "assistant_delta", content: output("After the stale scroll") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("After the stale scroll line 12");
  await settle();
  expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - reading)).toBeLessThanOrEqual(2);
});

for (const release of ["clear selection", "click return"] as const) {
test(`a reply selection keeps the current paragraph visible until ${release}`, async ({ page }) => {
  const { scroller, gap, send, settle } = await openChat(page);
  const paragraph = scroller.getByText("Saved row 40", { exact: true });
  await expect(paragraph).toBeInViewport({ ratio: 1 });
  const selected = await paragraph.evaluate((element) => {
    const selection = window.getSelection()!;
    selection.selectAllChildren(element);
    return selection.toString();
  });
  expect(selected).toBe("Saved row 40");
  const movement = await trackTop(paragraph);
  const viewport = await scroller.boundingBox();
  send({ type: "assistant_delta", content: "An additional line of output.\n".repeat(24) + "Static Footer final marker" });
  await expect(scroller.getByTestId("streaming-text")).toContainText("Static Footer final marker");
  await settle();
  await expect(paragraph).toBeInViewport({ ratio: 1 });
  expect(await movement.evaluate((tracker) => tracker.stop())).toBeLessThanOrEqual(2);
  await movement.dispose();
  expect(await gap()).toBeGreaterThan(150);
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await expect(jump).toBeVisible();
  expect(await scroller.boundingBox()).toEqual(viewport);

  if (release === "clear selection") await page.evaluate(() => window.getSelection()?.removeAllRanges());
  else {
    await jump.click();
    await expect(scroller).toBeFocused();
    expect(await page.evaluate(() => window.getSelection()?.toString())).toBe("");
  }
  await expectFooterMarkerInViewport(scroller);
  await expect(jump).toHaveCount(0);
});
}

test("clearing selected text uses the current navigation intent", async ({ page }) => {
  const { scroller, gap, send, settle, output } = await openChat(page);
  const paragraph = scroller.getByText("Saved row 40", { exact: true });
  const select = () => paragraph.evaluate((element) => window.getSelection()!.selectAllChildren(element));
  const selection = () => page.evaluate(() => window.getSelection()?.toString());
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await select();
  await scroller.hover();
  await page.mouse.wheel(0, -240);
  await expect(jump).toBeVisible();
  expect(await selection()).toBe("Saved row 40");
  const reader = scroller.getByText("Saved row 34", { exact: true });
  await expect(reader).toBeInViewport({ ratio: 1 });
  const movement = await trackTop(reader);
  await page.evaluate(() => window.getSelection()?.removeAllRanges());
  send({ type: "assistant_delta", content: output("After clearing while reading") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("After clearing while reading line 12");
  await settle();
  expect(await movement.evaluate((tracker) => tracker.stop())).toBeLessThanOrEqual(2);
  await movement.dispose();

  await scroller.hover();
  await page.mouse.wheel(0, 10000);
  await expect.poll(gap).toBeLessThanOrEqual(2);
  const tail = scroller.locator(".msg-streaming-text");
  await tail.evaluate((element) => window.getSelection()!.selectAllChildren(element));
  await page.mouse.wheel(0, 200);
  await expect.poll(selection).toContain("After clearing while reading");
  const tailTop = (await tail.boundingBox())!.y;
  send({ type: "assistant_delta", content: output("After navigating with a selection") + "Static Footer final marker" });
  await expect(jump).toBeVisible();
  expect(Math.abs((await tail.boundingBox())!.y - tailTop)).toBeLessThanOrEqual(2);
  await page.evaluate(() => window.getSelection()?.removeAllRanges());
  await expectFooterMarkerInViewport(scroller);
});

test("appending to a live reply preserves the selected text offsets", async ({ page }) => {
  const { scroller, gap, send, settle } = await openChat(page);
  send({ type: "assistant_delta", content: "Read these words while the reply continues." });
  const live = scroller.locator(".msg-streaming-text");
  await expect(live).toBeInViewport({ ratio: 1 });
  await expect.poll(gap).toBeLessThanOrEqual(2);
  const selected = await live.evaluate((body) => {
    const range = document.createRange();
    range.setStart(body.firstChild!, 5);
    range.setEnd(body.firstChild!, 16);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
    return selection.toString();
  });
  expect(selected).toBe("these words");
  const movement = await trackTop(live);
  send({ type: "assistant_delta", content: "\nMore of the same reply.\n".repeat(24) + "Static Footer final marker" });
  await expect(live).toContainText("Static Footer final marker");
  await settle();
  expect(await page.evaluate(() => window.getSelection()?.toString())).toBe(selected);
  expect(await movement.evaluate((tracker) => tracker.stop())).toBeLessThanOrEqual(2);
  await movement.dispose();
});

test("saving the second native preview keeps its selection alongside existing history", async ({ page }) => {
  const { scroller, gap, send, settle } = await openChat(page);
  const first = identity("root", "first-preview");
  const second = identity("root", "second-preview");
  send({ type: "assistant_delta", content: "First native preview", runtime_metadata: first });
  send({ type: "assistant_delta", content: "The second native preview", runtime_metadata: second });
  const live = scroller.locator(".msg-streaming-text").filter({ hasText: "The second native preview" });
  await expect(live).toBeInViewport({ ratio: 1 });
  await expect.poll(gap).toBeLessThanOrEqual(2);
  await expect(scroller.getByText("Saved row 40", { exact: true })).toBeInViewport();
  const offset = await live.evaluate((body) => {
    const range = document.createRange();
    range.setStart(body.firstChild!, 4);
    range.setEnd(body.firstChild!, 17);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
    return range.getBoundingClientRect().top;
  });
  send({ type: "assistant_text", seq: 41, content: "The second native preview", runtime_metadata: second });
  await expect(scroller.getByText("The second native preview", { exact: true })).toBeInViewport();
  await expect(live).toHaveCount(0);
  await settle();
  const saved = await page.evaluate(() => {
    const selection = window.getSelection()!;
    return { text: selection.toString(), top: selection.rangeCount ? selection.getRangeAt(0).getBoundingClientRect().top : 0 };
  });
  expect(saved.text).toBe("second native");
  expect(Math.abs(saved.top - offset)).toBeLessThanOrEqual(2);
});

test("saving Markdown keeps the selected occurrence after a link destination disappears", async ({ page }) => {
  const { scroller, gap, send, settle } = await openChat(page);
  const source = "[link](https://example.invalid/token)\n\nFirst token to read.\n\nSecond token to read.";
  send({ type: "assistant_delta", content: source });
  send({ type: "assistant_delta", content: "Following native output\n".repeat(10), runtime_metadata: identity("root", "following-item") });
  const live = scroller.locator(".msg-streaming-text").filter({ hasText: "First token to read." });
  await expect(live).toBeInViewport();
  await expect.poll(gap).toBeLessThanOrEqual(2);
  const original = await live.evaluate((body) => {
    const text = body.firstChild!;
    const index = text.textContent!.indexOf("First token") + "First ".length;
    const range = document.createRange();
    range.setStart(text, index);
    range.setEnd(text, index + "token".length);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
    const viewport = body.closest<HTMLElement>(".chat-messages")!;
    viewport.scrollTop -= 64;
    return { top: range.getBoundingClientRect().top, scrollTop: viewport.scrollTop, maximum: viewport.scrollHeight - viewport.clientHeight };
  });
  send({ type: "assistant_text", seq: 41, content: source });
  await expect(scroller.getByText("First token to read.", { exact: true })).toBeInViewport();
  await settle();
  const saved = await page.evaluate(() => {
    const selection = window.getSelection()!;
    const viewport = document.querySelector<HTMLElement>(".chat-messages")!;
    return { text: selection.toString(), paragraph: selection.anchorNode?.parentElement?.closest("p")?.textContent,
      top: selection.rangeCount ? selection.getRangeAt(0).getBoundingClientRect().top : 0,
      scrollTop: viewport.scrollTop, maximum: viewport.scrollHeight - viewport.clientHeight };
  });
  expect(saved.text).toBe("token");
  expect(saved.paragraph).toBe("First token to read.");
  expect(Math.abs(saved.top - original.top), JSON.stringify({ original, saved })).toBeLessThanOrEqual(2);
});

async function selectLiveWords(body: Locator, pattern: RegExp) {
  return body.evaluate((element, source) => {
    const viewport = element.closest<HTMLElement>(".chat-messages")!;
    const bounds = viewport.getBoundingClientRect();
    const range = document.createRange();
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    for (let text = walker.nextNode(); text; text = walker.nextNode()) {
      for (const match of text.textContent!.matchAll(new RegExp(source, "g"))) {
        range.setStart(text, match.index!);
        range.setEnd(text, match.index! + match[0].length);
        const line = range.getBoundingClientRect();
        if (line.top < bounds.top + 20 || line.bottom > bounds.bottom - 20) continue;
        const selection = window.getSelection()!;
        selection.removeAllRanges();
        selection.addRange(range);
        return { text: match[0], top: line.top };
      }
    }
    throw new Error("No selectable words in the scrollport");
  }, pattern.source);
}

function textInScrollport(body: Locator, phrase: string) {
  return body.evaluate((element, phrase) => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    const bounds = element.closest(".chat-messages")!.getBoundingClientRect();
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const index = node.textContent!.indexOf(phrase);
      if (index < 0) continue;
      const range = document.createRange();
      range.setStart(node, index);
      range.setEnd(node, index + phrase.length);
      const line = range.getBoundingClientRect();
      if (line.height > 0 && line.top >= bounds.top && line.bottom <= bounds.bottom + 1) return true;
    }
    return false;
  }, phrase);
}

for (const format of ["quote", "fenced code", "task list"] as const) {
test(`saving a multi-line ${format} preserves the selected word and exposes new lines beyond the viewport`, async ({ page }) => {
  const { scroller, gap, send, settle } = await openChat(page);
  const line = format === "quote" ? "> " : format === "task list" ? "  " : "";
  const opening = format === "quote" ? "> " : format === "task list" ? "- [x] " : "```text\n";
  const preview = opening + "Opening line\n" + line + "Read this token here\n" + line + "Last seen line";
  send({ type: "assistant_delta", content: preview });
  send({ type: "assistant_delta", content: "Following native output\n".repeat(12), runtime_metadata: identity("root", "following-format-item") });
  const live = scroller.locator(".msg-streaming-text").filter({ hasText: "Read this token here" });
  await expect(live).toBeInViewport();
  await expect.poll(gap).toBeLessThanOrEqual(2);
  const original = await selectLiveWords(live, /this token/);
  const addition = ("\n" + line + "New content outside the current reading viewport.").repeat(45) + "\n" + line + "Last new line";
  send({ type: "assistant_text", seq: 41, content: preview + addition + (format === "fenced code" ? "\n```" : "") });
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  const saved = scroller.locator('[data-message-key="message:41"] .msg-content');
  await expect(live).toHaveCount(0);
  await expect(jump).toBeVisible();
  await settle();
  const selection = await page.evaluate(() => {
    const selection = window.getSelection()!;
    return { text: selection.toString(), top: selection.rangeCount ? selection.getRangeAt(0).getBoundingClientRect().top : 0 };
  });
  expect(selection.text).toBe(original.text);
  expect(Math.abs(selection.top - original.top)).toBeLessThanOrEqual(2);
  expect(await textInScrollport(saved, "Last new line")).toBe(false);
  await jump.click();
  await expect.poll(gap).toBeLessThanOrEqual(2);
  await expect.poll(() => textInScrollport(saved, "Last new line")).toBe(true);
});
}

for (const { expression, word, fraction } of [
  ...["x+y", "a^2+x", "x+\\alpha", "\\alpha+x+\\beta"].map((expression) => ({ expression, word: /x(?=[+$])/, fraction: undefined })),
  { expression: "x/y", word: /x\/y/, fraction: undefined },
  { expression: "x/y", word: /\//, fraction: undefined },
  { expression: String.raw`x/y+\alpha`, word: /x\/y/, fraction: undefined },
  { expression: "(x+y)", word: /\(x\+y\)/, fraction: undefined },
  { expression: "α+β", word: /α/, fraction: undefined },
  { expression: String.raw`\alpha+x`, word: /\+/, fraction: undefined },
  { expression: String.raw`\alpha=x`, word: /=/, fraction: undefined },
  { expression: String.raw`\frac{x}{x}`, word: /x(?=\}\{)/, fraction: "numerator" },
  { expression: String.raw`\frac{x}{x}`, word: /x(?=\}\$)/, fraction: "denominator" },
]) {
test(`saving a formula keeps the selected literal characters and their offset: ${expression} ${fraction ?? word.source}`, async ({ page }) => {
  let releaseFonts!: () => void;
  const fontsMayFinish = new Promise<void>((resolve) => { releaseFonts = resolve; });
  await page.route(/KaTeX_.*\.woff2$/, async (route) => {
    await fontsMayFinish;
    await route.continue();
  });
  const { scroller, gap, send, settle } = await openChat(page);
  const source = `Keep $${expression}$ beside the original line`;
  send({ type: "assistant_delta", content: source });
  send({ type: "assistant_delta", content: "Following native output\n".repeat(12), runtime_metadata: identity("root", "following-math-item") });
  const live = scroller.locator(".msg-streaming-text").filter({ hasText: "beside the original line" });
  await expect(live).toBeInViewport();
  await expect.poll(gap).toBeLessThanOrEqual(2);
  const original = await selectLiveWords(live, word);
  // Leave room for the alignment while the reader holds the selection.
  send({ type: "assistant_delta", content: "More following native output\n".repeat(4),
    runtime_metadata: identity("root", "following-math-item") });
  await expect.poll(gap).toBeGreaterThan(12);
  send({ type: "assistant_text", seq: 41, content: source });
  await expect(live).toHaveCount(0);
  await expect(scroller.locator(".katex-html")).toBeInViewport();
  await settle();
  releaseFonts();
  await page.evaluate(async () => { await document.fonts.ready; });
  await settle();
  const saved = await scroller.evaluate((viewport) => {
    const selection = window.getSelection()!;
    const first = selection.rangeCount ? selection.getRangeAt(0).getClientRects()[0] : undefined;
    return { text: selection.toString(), top: first?.top,
      scrollTop: viewport.scrollTop, maximum: viewport.scrollHeight - viewport.clientHeight };
  });
  expect(saved.text).toBe(original.text);
  expect(saved.top).toBeDefined();
  if (fraction) {
    const glyphs = await scroller.locator(".katex-html").evaluate((element) => {
      const range = document.createRange();
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      const tops = [];
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (node.textContent === "x") {
          range.selectNodeContents(node);
          tops.push(range.getBoundingClientRect().top);
        }
      }
      return tops.sort((a, b) => a - b);
    });
    expect(glyphs).toHaveLength(2);
    expect(glyphs[1] - glyphs[0]).toBeGreaterThan(8);
    expect(saved.top).toBe(glyphs[fraction === "numerator" ? 0 : 1]);
  }
  expect(Math.abs(saved.top! - original.top), JSON.stringify({ original, saved })).toBeLessThanOrEqual(2);
});
}

test("a later reply offers return while the selected formula is waiting for fonts", async ({ page }) => {
  let releaseFonts!: () => void;
  const fontsMayFinish = new Promise<void>((resolve) => { releaseFonts = resolve; });
  await page.route(/KaTeX_.*\.woff2$/, async (route) => {
    await fontsMayFinish;
    await route.continue();
  });
  const { scroller, gap, send, settle } = await openChat(page);
  const source = "Keep $x+y$ beside the original line";
  const following = identity("root", "following-math-item");
  send({ type: "assistant_delta", content: source });
  send({ type: "assistant_delta", content: "Following native output\n".repeat(12), runtime_metadata: following });
  const live = scroller.locator(".msg-streaming-text").filter({ hasText: "beside the original line" });
  await expect(live).toBeInViewport();
  await expect.poll(gap).toBeLessThanOrEqual(2);
  await selectLiveWords(live, /x(?=\+y)/);
  send({ type: "assistant_text", seq: 41, content: source });
  await expect(live).toHaveCount(0);
  await expect(scroller.locator(".katex-html")).toBeInViewport();
  await expect.poll(() => page.evaluate(() => document.fonts.status)).toBe("loading");
  await expect.poll(() => page.evaluate(() => window.getSelection()?.toString())).toBe("x");
  await settle();
  const latest = "New reply while math fonts load";
  send({ type: "assistant_delta", content: "\n".repeat(4) + latest, runtime_metadata: following });
  const tail = scroller.locator(".msg-streaming-text").filter({ hasText: latest });
  await expect(tail).toHaveCount(1);
  await expect.poll(() => textInScrollport(tail, latest)).toBe(false);
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await expect(jump).toBeVisible();
  await jump.click();
  await expect.poll(() => textInScrollport(tail, latest)).toBe(true);
  await expect.poll(gap).toBeLessThanOrEqual(2);
  releaseFonts();
  await page.evaluate(async () => { await document.fonts.ready; });
  await settle();
  await expect.poll(gap).toBeLessThanOrEqual(2);
  await expect.poll(() => textInScrollport(tail, latest)).toBe(true);
  expect(await page.evaluate(() => window.getSelection()?.toString())).toBe("");
});

test("saving a capped native preview preserves its selected prefix and offers return for the unseen tail", async ({ page }) => {
  const { scroller, gap, send } = await openChat(page);
  const runtime_metadata = identity("root", "capped-root");
  const content = Array.from({ length: 240 }, (_, index) =>
    `Block ${String(index).padStart(3, "0")} holds a unique marker and enough words to reach the preview limit.`).join("\n\n");
  send({ type: "assistant_delta", content, runtime_metadata });
  const live = scroller.locator(".msg-streaming-text");
  await expect(live).toHaveText(/…$/);
  await expect.poll(gap).toBeLessThanOrEqual(2);
  const selected = await selectLiveWords(live, /Block \d{3}/);
  send({ type: "assistant_text", seq: 41, content, runtime_metadata });
  await expect(live).toHaveCount(0);
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await expect(jump).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.getSelection()?.toString())).toBe(selected.text);
  await jump.click();
  await expect(scroller.getByText(/Block 239 holds/)).toBeInViewport();
});

test("footer spacing and saving an already seen live reply do not offer a return", async ({ page }) => {
  const { scroller, gap, send, settle } = await openChat(page, 4);
  const paragraph = scroller.getByText("Saved row 4", { exact: true });
  await paragraph.evaluate((element) => window.getSelection()!.selectAllChildren(element));
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  const live = scroller.locator(".msg-streaming-text");
  send({ type: "assistant_delta", content: "Short readable reply" });
  await expect(live).toBeInViewport({ ratio: 1 });
  const view = page.viewportSize()!;
  const slack = await live.evaluate((body) => {
    return body.closest(".chat-messages")!.getBoundingClientRect().bottom - body.getBoundingClientRect().bottom;
  });
  expect(slack).toBeGreaterThan(20);
  await page.setViewportSize({ width: view.width, height: view.height - Math.floor(slack) + 2 });
  await expect.poll(gap).toBeGreaterThan(2);
  send({ type: "assistant_delta", content: "!" });
  await expect(live).toHaveText("Short readable reply!");
  await expect(live).toBeInViewport({ ratio: 1 });
  await settle();
  await expect(jump).toHaveCount(0);

  // Moving the scrollport edge by resize is a layout change. Saving the
  // text now outside it must not recreate an output notification.
  await page.setViewportSize({ width: view.width, height: view.height - Math.floor(slack) - 28 });
  await expect(live).not.toBeInViewport();
  for (let repeat = 0; repeat < 2; repeat++) send({ type: "assistant_text", seq: 5, content: "Short readable reply!" });
  await expect(scroller.getByText("Short readable reply!", { exact: true })).toHaveCount(1);
  await settle();
  await expect(jump).toHaveCount(0);
  expect(await page.evaluate(() => window.getSelection()?.toString())).toBe("Saved row 4");
});

test("Markdown completion only measures the visible added source span", async ({ page }) => {
  await page.setViewportSize({ width: 1150, height: 450 });
  const { scroller, gap, send, settle } = await openChat(page);
  const preview = "**Previously read formatting** " + "Words that already belong to this reply. ".repeat(38) + "\n\nTail";
  send({ type: "assistant_delta", content: preview });
  await expect(scroller.locator(".msg-streaming-text")).toContainText("Tail");
  await expect.poll(gap).toBeLessThanOrEqual(2);
  const history = scroller.getByText("Saved row 40", { exact: true });
  await history.evaluate((body) => window.getSelection()!.selectAllChildren(body));
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await expect(jump).toHaveCount(0);

  send({ type: "assistant_text", seq: 41, content: preview + " has a readable addition." });
  await expect(scroller.getByText("Tail has a readable addition.", { exact: true })).toBeInViewport({ ratio: 1 });
  await expect(scroller.getByText("Previously read formatting", { exact: true })).not.toBeInViewport();
  await settle();
  expect(await page.evaluate(() => window.getSelection()?.toString())).toBe("Saved row 40");
  await expect(jump).toHaveCount(0);
});

test("decoded Markdown entities in offscreen added text offer a return", async ({ page }) => {
  const { scroller, send } = await openChat(page);
  const paragraph = scroller.getByText("Saved row 40", { exact: true });
  await paragraph.evaluate((body) => window.getSelection()!.selectAllChildren(body));
  send({ type: "assistant_delta", content: "Old &amp;" });
  await expect(scroller.locator(".msg-streaming-text")).toHaveText("Old &amp;");
  send({ type: "assistant_text", seq: 41, content: "Old &amp;" + " &copy;".repeat(1_000) + " &reg;" });
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await expect(jump).toBeVisible();
  await jump.click();
  await expect.poll(() => scroller.locator('[data-message-key="message:41"] .msg-content p').evaluate((body) => {
    const text = body.firstChild!;
    const range = document.createRange();
    range.setStart(text, text.textContent!.lastIndexOf("®"));
    range.setEnd(text, text.textContent!.length);
    return range.getBoundingClientRect().bottom <= body.closest(".chat-messages")!.getBoundingClientRect().bottom;
  })).toBe(true);
});

for (const outside of [false, true]) {
test(`a saved inline formula ${outside ? "beyond" : "inside"} the viewport ${outside ? "offers" : "omits"} the return`, async ({ page }) => {
  const { scroller, send, settle } = await openChat(page, 4);
  const old = "Text that has already been read";
  await scroller.getByText("Saved row 4", { exact: true }).evaluate((body) => window.getSelection()!.selectAllChildren(body));
  send({ type: "assistant_delta", content: old });
  const live = scroller.locator(".msg-streaming-text");
  await expect(live).toHaveText(old);
  await expect(live).toBeInViewport({ ratio: 1 });
  await settle();
  if (outside) {
    const view = page.viewportSize()!;
    const slack = await live.evaluate((body) => body.closest(".chat-messages")!.getBoundingClientRect().bottom - body.getBoundingClientRect().top);
    await page.setViewportSize({ width: view.width, height: view.height - Math.floor(slack) - 8 });
    await expect(live).not.toBeInViewport();
  }
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await expect(jump).toHaveCount(0);
  send({ type: "assistant_text", seq: 5, content: old + " $x + y = 42$" });
  const formula = scroller.locator(".katex-html");
  await expect(formula).toHaveCount(1);
  if (outside) {
    await expect(formula).not.toBeInViewport();
    await expect(jump).toBeVisible();
    await jump.click();
  }
  await expect(formula).toBeInViewport({ ratio: 1 });
  await expect(jump).toHaveCount(0);
});
}

for (const graphic of [false, true]) {
test(`offscreen ${graphic ? "painted math rule offers" : "transparent math spacer omits"} the return`, async ({ page }) => {
  const { scroller, send, settle } = await openChat(page, 4);
  const old = "Text that has already been read";
  await scroller.getByText("Saved row 4", { exact: true }).evaluate((body) => window.getSelection()!.selectAllChildren(body));
  send({ type: "assistant_delta", content: old });
  const live = scroller.locator(".msg-streaming-text");
  await expect(live).toHaveText(old);
  await expect(live).toBeInViewport({ ratio: 1 });
  await settle();
  const view = page.viewportSize()!;
  const slack = await live.evaluate((body) => body.closest(".chat-messages")!.getBoundingClientRect().bottom - body.getBoundingClientRect().top);
  await page.setViewportSize({ width: view.width, height: view.height - Math.floor(slack) - 8 });
  await expect(live).not.toBeInViewport();
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await expect(jump).toHaveCount(0);
  send({ type: "assistant_text", seq: 5, content: old + (graphic ? " $\\rule{25mm}{2mm}$" : " $\\phantom{hidden}$") });
  const formula = scroller.locator(".katex-html");
  await expect(formula).toHaveCount(1);
  await expect(formula).not.toBeInViewport();
  await settle();
  if (graphic) {
    const stroke = formula.locator(".katex-rule");
    expect(await stroke.evaluate((element) => Number.parseFloat(getComputedStyle(element).borderTopWidth))).toBeGreaterThan(0);
    await expect(jump).toBeVisible();
    await jump.click();
    await expect(stroke).toBeInViewport({ ratio: 1 });
  } else {
    const colors = await formula.evaluate((element) => [...element.querySelectorAll<HTMLElement>("span")]
      .filter((child) => child.firstChild instanceof Text && child.textContent?.trim()).map((child) => getComputedStyle(child).color));
    expect(colors.length).toBeGreaterThan(0);
    expect(new Set(colors)).toEqual(new Set(["rgba(0, 0, 0, 0)"]));
    await expect(jump).toHaveCount(0);
  }
});
}

for (const mounted of [true, false]) {
for (const painted of [true, false]) {
test(`a ${mounted ? "mounted" : "unmounted"} ${painted ? "stylesheet formula stroke offers" : "zero-width formula omits"} the return`, async ({ page }) => {
  const { scroller, gap, send, settle } = await openChat(page, 4);
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  const old = "Text that has already been read";
  if (mounted) {
    await scroller.getByText("Saved row 4", { exact: true }).evaluate((body) => window.getSelection()!.selectAllChildren(body));
    send({ type: "assistant_delta", content: old });
    const live = scroller.locator(".msg-streaming-text");
    await expect(live).toBeInViewport({ ratio: 1 });
    await settle();
    const view = page.viewportSize()!;
    const slack = await live.evaluate((body) => body.closest(".chat-messages")!.getBoundingClientRect().bottom - body.getBoundingClientRect().top);
    await page.setViewportSize({ width: view.width, height: view.height - Math.floor(slack) - 8 });
    await expect(live).not.toBeInViewport();
  } else {
    const reference = "/api/sessions/follow-output/messages/5";
    await page.route(`**${reference}?view=browser`, (route) => route.fulfill({ json: {
      role: "assistant", type: "text", seq: 5, content: "Expanded paragraph.\n\n".repeat(55),
    } }));
    send({ type: "assistant_text", seq: 5, content: "Long reply preview", body_state: "preview", detail_ref: reference });
    await scroller.getByRole("button", { name: "View full message" }).click();
    await expect.poll(gap).toBeGreaterThan(500);
  }
  await settle();
  await expect(jump).toHaveCount(0);
  const expression = painted ? String.raw`$\text{\sout{\phantom{x}}}$` : String.raw`$\rule{0mm}{2mm}$`;
  send({ type: "assistant_text", seq: mounted ? 5 : 6, content: mounted ? `${old} ${expression}` : expression });
  const stroke = scroller.locator(painted ? ".katex-sout" : ".katex-rule");
  if (mounted) {
    await expect(stroke).toHaveCount(1);
    await expect(stroke).not.toBeInViewport();
  } else {
    await settle();
    await expect(stroke).toHaveCount(0);
  }
  if (painted) {
    await expect(jump).toBeVisible();
    await jump.click();
    await expect(stroke).toBeInViewport({ ratio: 1 });
    expect(await stroke.evaluate((node) => parseFloat(getComputedStyle(node).borderBottomWidth))).toBeGreaterThan(0);
  } else {
    await settle();
    await expect(jump).toHaveCount(0);
    if (!mounted) await scroller.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    await expect.poll(() => stroke.evaluate((node) => node.getBoundingClientRect().width)).toBe(0);
  }
});
}
}

for (const kind of ["root reply", "first helper answer", "tool result"] as const) {
test(`a new saved ${kind} outside the mounted rows offers a return`, async ({ page }) => {
  const { scroller, gap, send, settle } = await openChat(page, 4);
  const reference = "/api/sessions/follow-output/messages/5";
  await page.route(`**${reference}?view=browser`, (route) => route.fulfill({ json: {
    role: "assistant", type: "text", seq: 5,
    content: Array.from({ length: 55 }, (_, index) => `Expanded paragraph ${index + 1}`).join("\n\n"),
  } }));
  send({ type: "assistant_text", seq: 5, content: "Preview of the long reply", body_state: "preview", detail_ref: reference });
  await scroller.getByRole("button", { name: "View full message" }).click();
  const paragraph = scroller.getByText("Expanded paragraph 1", { exact: true });
  await expect(paragraph).toBeInViewport({ ratio: 1 });
  await expect.poll(gap).toBeGreaterThan(500);
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await settle();
  await expect(jump).toHaveCount(0);
  const movement = await trackTop(paragraph);
  const reply = scroller.getByText("Newly saved response at the end", { exact: true });
  send({ type: "assistant_text", seq: 6, content: "[reference]: https://example.invalid" });
  await settle();
  await expect(jump).toHaveCount(0);
  if (kind === "tool result") send({ type: "tool_result", seq: 7, output: "Newly saved response at the end" });
  else send({ type: "assistant_text", seq: 7, content: "Newly saved response at the end",
    ...(kind === "first helper answer" ? { runtime_metadata: identity("new-helper", "answer") } : {}) });
  await expect(jump).toBeVisible();
  await expect(reply).toHaveCount(0);
  expect(await movement.evaluate((tracker) => tracker.stop())).toBeLessThanOrEqual(2);
  await movement.dispose();
  await jump.click();
  await expect(reply).toBeInViewport({ ratio: 1 });
  await expect.poll(gap).toBeLessThanOrEqual(2);
});
}

for (const { source, stationaryWheel } of [
  { source: "live", stationaryWheel: false },
  { source: "saved", stationaryWheel: false },
  { source: "live", stationaryWheel: true },
] as const) {
test(`an unmounted existing helper offers a return for a short ${source} continuation${stationaryWheel ? " during a zero-distance wheel" : ""}`, async ({ page }) => {
  const { scroller, gap, send, settle } = await openChat(page, 4);
  const reference = "/api/sessions/follow-output/messages/5";
  await page.route(`**${reference}?view=browser`, (route) => route.fulfill({ json: {
    role: "assistant", type: "text", seq: 5,
    content: "Expanded content shifts the helper outside the mounted range.\n\n".repeat(55),
  } }));
  send({ type: "assistant_text", seq: 5, content: "Short preview", body_state: "preview", detail_ref: reference });
  send({ type: "assistant_text", seq: 6, content: "Short first answer.", runtime_metadata: identity("new-helper", "first") });
  const answer = scroller.locator(".subagent-summary");
  await expect(answer).toBeInViewport();
  await scroller.getByRole("button", { name: "View full message" }).click();
  await expect.poll(gap).toBeGreaterThan(500);
  await expect(answer).toHaveCount(0);
  await settle();
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await expect(jump).toHaveCount(0);
  const continuation = { content: "Short continuation.", runtime_metadata: identity("new-helper", "second") };
  if (stationaryWheel) {
    await scroller.hover();
    const scrollTop = () => scroller.evaluate((viewport) => viewport.scrollTop);
    const readingTop = await scrollTop();
    const wheel = await scroller.evaluateHandle((viewport) => {
      let observed: { trusted: boolean; cancelable: boolean } | undefined;
      const read = (event: Event) => { observed = { trusted: event.isTrusted, cancelable: event.cancelable }; };
      viewport.addEventListener("wheel", read, { passive: false, once: true });
      return { observed: () => observed, stop: () => viewport.removeEventListener("wheel", read) };
    });
    const gate = await holdAnimationFrames(page);
    try {
      expect(await gate.evaluate((frames) => frames.count())).toBe(0);
      send({ type: "assistant_delta", ...continuation });
      await expect.poll(() => gate.evaluate((frames) => frames.count())).toBeGreaterThan(0);
      await page.mouse.wheel(0, 0.1);
      await expect.poll(() => wheel.evaluate((event) => event.observed())).toEqual({ trusted: true, cancelable: true });
    } finally {
      await gate.evaluate((frames) => frames.release());
      await gate.dispose();
      await wheel.evaluate((event) => event.stop());
      await wheel.dispose();
    }
    await settle();
    expect(await scrollTop()).toBe(readingTop);
  } else if (source === "live") send({ type: "assistant_delta", ...continuation });
  else send({ type: "assistant_text", seq: 7, ...continuation });
  await expect(jump).toBeVisible();
  await expect(answer).toHaveCount(0);
  await jump.click();
  await expect(answer).toHaveText("Short first answer. Short continuation.");
  await expect(answer).toBeInViewport({ ratio: 1 });
});
}

test("a helper answer revealed by preview eviction stays readable without a return offer", async ({ page }) => {
  const { scroller, gap, send, settle } = await openChat(page, 4);
  const nextHelper = { ...helper, card_id: "next-helper-card", task_id: "next-helper", native_thread_id: "next-helper",
    runtime_metadata: identity("next-helper", "item"), name: "Next helper" };
  send({ type: "subagent", ...helper, seq: 5 });
  send({ type: "subagent", ...nextHelper, seq: 6 });
  send({ type: "assistant_delta", runtime_metadata: helper.runtime_metadata,
    content: "The oldest helper preview fills the viewport.\n".repeat(90) });
  const first = scroller.getByRole("button", { name: /^Helper Reading/ });
  const next = scroller.getByRole("button", { name: /^Next helper Reading/ });
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await expect(scroller.locator(".subagent-summary")).toContainText("The oldest helper preview");
  await expect(next).toBeInViewport();
  await first.click();
  await expect.poll(gap).toBeGreaterThan(500);
  await expect(next).toHaveCount(0);
  const scrollTop = () => scroller.evaluate((element) => element.scrollTop);
  await settle();
  await expect(jump).toHaveCount(0);
  const readingTop = await scrollTop();
  const movement = await trackTop(first);

  // Leave the oldest preview in the expanded card until the next helper
  // receives the 33rd item. The other previews have no projected card.
  for (let index = 0; index < 31; index++) {
    send({ type: "assistant_delta", runtime_metadata: identity("unobserved", `item-${index}`), content: "Preview" });
  }
  send({ type: "assistant_delta", runtime_metadata: nextHelper.runtime_metadata, content: "New answer in view" });
  await expect(scroller.getByText("New answer in view", { exact: true })).toBeInViewport({ ratio: 1 });
  await expect(scroller.getByText(/The oldest helper preview/)).toHaveCount(0);
  await settle();
  expect(await scrollTop()).toBe(readingTop);
  expect(await movement.evaluate((tracker) => tracker.stop())).toBeLessThanOrEqual(2);
  await movement.dispose();
  await expect(jump).toHaveCount(0);
});

test("reconnect fills new output around a previously seen sequence without moving the reader", async ({ page }) => {
  const { scroller, send, settle, replaceOnReconnect } = await openChat(page, 4);
  const tool = "Tool detail open during reconnect";
  send({ type: "tool_result", seq: 5, output: tool });
  await scroller.locator(".msg-tool-result .tool-header").click();
  const reader = scroller.getByText(tool, { exact: true });
  await expect(reader).toBeInViewport();
  send({ type: "user_message", seq: 8, content: "Previously received from another tab" });
  await expect(scroller.getByText("Previously received from another tab", { exact: true })).toBeInViewport();
  await settle();
  const movement = await trackTop(reader);
  const rows: Message[] = Array.from({ length: 4 }, (_, index) => ({ role: "assistant", type: "text", seq: index + 1, content: `Saved row ${index + 1}` }));
  rows.push({ role: "tool", type: "tool_result", seq: 5, content: tool },
    { role: "assistant", type: "text", seq: 6, content: "New after reconnect.\n\n".repeat(22) },
    { role: "assistant", type: "text", seq: 7, content: "Recovered tail reply" },
    { role: "user", type: "text", seq: 8, content: "Previously received from another tab" });
  replaceOnReconnect({ messages: rows, next_message_seq: 9, oldest_loaded_seq: 1, has_more_messages: false });
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await expect(jump).toBeVisible();
  await expect(reader).toBeInViewport();
  expect(await movement.evaluate((tracker) => tracker.stop())).toBeLessThanOrEqual(2);
  await movement.dispose();
  await jump.click();
  await expect(scroller.getByText("Recovered tail reply", { exact: true })).toBeInViewport({ ratio: 1 });
  await expect(scroller.getByText("Previously received from another tab", { exact: true })).toHaveCount(1);
});

test("a reconnect matches the cleared preview to its own saved reply among several additions", async ({ page }) => {
  const { scroller, send, settle, replaceOnReconnect } = await openChat(page, 4);
  const tool = "Tool detail stays open";
  const earlier = "Earlier newly collected sentence";
  const alreadySeen = "Already seen preview before the reconnect";
  send({ type: "tool_result", seq: 5, output: tool });
  await scroller.locator(".msg-tool-result .tool-header").click();
  send({ type: "assistant_delta", content: alreadySeen });
  const live = scroller.getByText(alreadySeen, { exact: true });
  await expect(live).toBeInViewport({ ratio: 1 });
  await settle();
  const view = page.viewportSize()!;
  const slack = await live.evaluate((element) => element.closest(".chat-messages")!.getBoundingClientRect().bottom - element.getBoundingClientRect().bottom);
  await page.setViewportSize({ width: view.width, height: view.height - Math.floor(slack) + 12 });
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await expect(jump).toHaveCount(0);
  const rows: Message[] = Array.from({ length: 4 }, (_, index) => ({ role: "assistant", type: "text", seq: index + 1, content: `Saved row ${index + 1}` }));
  rows.push({ role: "tool", type: "tool_result", seq: 5, content: tool },
    { role: "assistant", type: "text", seq: 6, content: earlier },
    { role: "assistant", type: "text", seq: 7, content: alreadySeen });
  replaceOnReconnect({ messages: rows, next_message_seq: 8, oldest_loaded_seq: 1, has_more_messages: false });
  await expect(scroller.getByText(earlier, { exact: true })).toBeInViewport({ ratio: 1 });
  await expect(live).not.toBeInViewport();
  await settle();
  await expect(jump).toHaveCount(0);
});

for (const earlierGap of [false, true]) {
test(`reconnect preserves the first same-text completion and offers the unseen second occurrence (${earlierGap ? "older gap" : "contiguous"})`, async ({ page }) => {
  const { scroller, send, settle, replaceOnReconnect } = await openChat(page, 4);
  const content = "Two distinct replies can have the same words.";
  const earlier: Message = { role: "user", type: "text", seq: 6, content: "Visible before the preview started" };
  if (earlierGap) {
    send({ type: "user_message", seq: earlier.seq, content: earlier.content });
    await expect(scroller.getByText(earlier.content!, { exact: true })).toBeInViewport();
  }
  send({ type: "assistant_delta", content });
  const live = scroller.locator(".msg-streaming-text");
  await expect(live).toBeInViewport({ ratio: 1 });
  await selectLiveWords(live, /same words/);
  const size = page.viewportSize()!;
  const slack = await live.evaluate((element) => element.closest(".chat-messages")!.getBoundingClientRect().bottom - element.getBoundingClientRect().bottom);
  await page.setViewportSize({ width: size.width, height: size.height - Math.floor(slack) + 12 });
  const before = await page.evaluate(() => window.getSelection()!.getRangeAt(0).getBoundingClientRect().top);
  const messages: Message[] = Array.from({ length: 4 }, (_, index) => ({ role: "assistant", type: "text", seq: index + 1, content: `Saved row ${index + 1}` }));
  const completion = earlierGap ? 7 : 5;
  const reply = (seq: number): Message => ({ role: "assistant", type: "text", seq, content });
  if (earlierGap) messages.push(reply(5), earlier);
  messages.push(reply(completion), reply(completion + 1));
  replaceOnReconnect({ messages, next_message_seq: completion + 2, oldest_loaded_seq: 1, has_more_messages: false });
  await expect(scroller.getByText(content, { exact: true })).toHaveCount(earlierGap ? 3 : 2);
  await settle();
  const saved = await page.evaluate(() => {
    const selection = window.getSelection()!;
    return { text: selection.toString(), key: selection.anchorNode?.parentElement?.closest<HTMLElement>("[data-message-key]")?.dataset.messageKey,
      top: selection.rangeCount ? selection.getRangeAt(0).getBoundingClientRect().top : 0 };
  });
  expect(saved.text).toBe("same words");
  expect(saved.key).toBe(`message:${completion}`);
  expect(Math.abs(saved.top - before)).toBeLessThanOrEqual(2);
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await expect(jump).toBeVisible();
  await jump.click();
  await expect(scroller.locator(`[data-message-key="message:${completion + 1}"]`).getByText(content, { exact: true })).toBeInViewport({ ratio: 1 });
});
}

test("reconnect can fill a gap above the passage the reader is on", async ({ page }) => {
  const { scroller, gap, send, settle, replaceOnReconnect } = await openChat(page, 4);
  const readingText = Array.from({ length: 40 }, (_, index) => `Long reply paragraph ${index + 1}`).join("\n\n");
  const saved: Message = { role: "assistant", type: "text", seq: 8, content: readingText };
  send({ type: "assistant_text", seq: 8, content: readingText });
  await expect(scroller.getByText("Long reply paragraph 40", { exact: true })).toBeInViewport({ ratio: 1 });
  await scroller.hover();
  await page.mouse.wheel(0, -300);
  await expect.poll(gap).toBeGreaterThan(240);
  await settle();
  const reader = scroller.getByText("Long reply paragraph 25", { exact: true });
  await expect(reader).toBeInViewport({ ratio: 1 });
  const offset = (await reader.boundingBox())!.y;
  const rows: Message[] = Array.from({ length: 7 }, (_, index) => ({ role: "assistant", type: "text", seq: index + 1,
    content: index < 4 ? `Saved row ${index + 1}` : `Gap filled with reply ${index + 1}` }));
  const reread = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/sessions/follow-output");
  replaceOnReconnect({ messages: [...rows, saved], next_message_seq: 9, oldest_loaded_seq: 1, has_more_messages: false });
  await reread;
  await expect(page.locator(".conn-status.on")).toBeVisible();
  // Connection replaces the detail snapshot asynchronously, including rows
  // above the mounted range. The mounted saved passage must retain its offset.
  await settle();
  await expect(reader).toBeInViewport({ ratio: 1 });
  expect(Math.abs((await reader.boundingBox())!.y - offset)).toBeLessThanOrEqual(2);
});

test("departed transcript selection and composer selection cannot pause the returning view", async ({ page }) => {
  const { scroller, send, session } = await openChat(page);
  const paragraph = scroller.getByText("Saved row 40", { exact: true });
  await paragraph.evaluate((element) => window.getSelection()!.selectAllChildren(element));
  expect(await page.evaluate(() => window.getSelection()?.toString())).toBe("Saved row 40");
  await page.getByRole("button", { name: /^Schedules/ }).evaluate((element) => (element as HTMLElement).click());
  await expect(scroller).toBeHidden();
  await page.locator(".session-item", { hasText: session.name }).evaluate((element) => (element as HTMLElement).click());
  await expect(scroller).toBeVisible();
  expect(await page.evaluate(() => window.getSelection()?.toString())).toBe("");
  const input = page.locator(".chat-input-bar textarea");
  await input.fill("Selected in the composer");
  await input.selectText();
  send({ type: "assistant_delta", content: "Output after returning.\n".repeat(24) + "Static Footer final marker" });
  await expectFooterMarkerInViewport(scroller);
  await expect(page.getByRole("button", { name: "Jump to latest", exact: true })).toHaveCount(0);
  expect(await input.evaluate((element) => (element as HTMLTextAreaElement).selectionEnd)).toBe(24);
});

for (const reading of [true, false]) {
test(`ordinary send and receipts preserve ${reading ? "history reading" : "latest following"}`, async ({ page }) => {
  const { scroller, gap, send, settle, submissions } = await openChat(page);
  const input = page.locator(".chat-input-bar textarea");
  if (reading) {
    await scroller.hover();
    await page.mouse.wheel(0, -240);
    await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
  }
  const reader = scroller.getByText("Saved row 30", { exact: true });
  if (reading) await expect(reader).toBeInViewport({ ratio: 1 });
  const movement = reading ? await trackTop(reader) : null;
  await input.fill(" ");
  await input.press("Enter");
  expect(submissions).toHaveLength(0);

  await input.fill("Rejected ordinary message");
  await input.press("Enter");
  await expect.poll(() => submissions.length).toBe(1);
  send({ type: "submission", client_request_id: submissions[0].client_request_id, accepted: false, message: "Message validation failed" });
  await expect(page.getByText("Message validation failed", { exact: true })).toBeVisible();
  if (!reading) await expect.poll(gap).toBeLessThanOrEqual(2);

  await input.fill("My ordinary message");
  await input.press("Enter");
  await expect.poll(() => submissions.map(({ content }) => content)).toEqual(["Rejected ordinary message", "My ordinary message"]);
  send({ type: "submission", client_request_id: submissions[1].client_request_id, accepted: true });
  send({ type: "user_message", seq: 41, content: "My ordinary message" });
  send({ type: "user_message", seq: 42, content: "Message from another tab" });
  send({ type: "user_message", seq: 42, content: "Message from another tab" });
  const other = scroller.getByText("Message from another tab", { exact: true });
  await settle();
  if (movement) {
    await expect(reader).toBeInViewport({ ratio: 1 });
    await expect(other).not.toBeInViewport();
    expect(await movement.evaluate((tracker) => tracker.stop())).toBeLessThanOrEqual(2);
    await movement.dispose();
    await page.getByRole("button", { name: "Jump to latest", exact: true }).click();
  } else {
    await expect.poll(gap).toBeLessThanOrEqual(2);
  }
  await expect(other).toHaveCount(1);
  await expect(other).toBeInViewport({ ratio: 1 });
});
}

test("chat follows output after a card, preserves history reading and resumes at the bottom", async ({ page }) => {
  const { scroller, gap, send, settle, output } = await openChat(page);
  const jump = page.getByRole("button", { name: "Jump to latest" });
  await expect(jump).toHaveCount(0);
  send({ type: "assistant_text", seq: 41, content: "Choose the next step", tool_input: { native_item: {
    type: "agentMessage", id: "question", questions: [{ title: "Choose the next step", options: ["Continue", "Review"] }],
  } } });
  await expect(scroller.getByRole("region", { name: "Question for you" })).toBeVisible();
  await expect.poll(gap).toBeLessThanOrEqual(2);
  send({ type: "assistant_delta", content: output("First output") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("First output line 12");
  await settle();
  const firstGap = await gap();

  await scroller.hover();
  await page.mouse.wheel(0, 10000);
  await expect.poll(gap).toBeLessThanOrEqual(2);

  const viewport = await scroller.boundingBox();
  await scroller.hover();
  await page.mouse.wheel(0, -350);
  await expect.poll(gap).toBeGreaterThan(200);
  await expect(jump).toBeVisible();
  expect(await scroller.boundingBox()).toEqual(viewport);
  const top = await scroller.evaluate((element) => element.scrollTop);
  send({ type: "assistant_delta", content: output("While reading history") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("While reading history line 12");
  const drift = await scroller.evaluate(async (element, initialTop) => {
    let maximum = 0;
    for (let frame = 0; frame < 24; frame++) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      maximum = Math.max(maximum, Math.abs(element.scrollTop - initialTop));
    }
    return maximum;
  }, top);
  expect(drift).toBeLessThanOrEqual(2);

  await jump.press("Enter");
  await expect.poll(gap).toBeLessThanOrEqual(2);
  await expect(jump).toHaveCount(0);
  await expect(scroller).toBeFocused();
  send({ type: "assistant_delta", content: output("Resumed output") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("Resumed output line 12");
  await settle();
  const resumedGap = await gap();
  expect.soft(firstGap).toBeLessThanOrEqual(2);
  expect.soft(resumedGap).toBeLessThanOrEqual(2);
});

test("a cross-frame reply burst follows one continuous path to the latest output", async ({ page }, info) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  const { scroller, send, settle, gap } = await openChat(page);
  await page.evaluate(() => document.fonts.ready); await settle();
  const trace = await scroller.evaluateHandle((element) => {
    const samples: { at: number; top: number; bottom: number }[] = [];
    const writes: { at: number; before: number; after: number; gap: number }[] = [];
    const original = element.scrollTo;
    const startHeight = element.scrollHeight;
    element.scrollTo = (optionsOrX?: ScrollToOptions | number, y?: number) => {
      const before = element.scrollTop;
      const gap = element.scrollHeight - element.clientHeight - before;
      Reflect.apply(original, element, typeof optionsOrX === "number" ? [optionsOrX, y ?? 0] : [optionsOrX]);
      writes.push({ at: performance.now(), before, after: element.scrollTop, gap });
    };
    let frame = 0;
    const sample = () => {
      samples.push({ at: performance.now(), top: element.scrollTop, bottom: element.scrollHeight - element.clientHeight });
      frame = requestAnimationFrame(sample);
    };
    sample();
    return { snapshot: () => ({ samples, writes, growth: element.scrollHeight - startHeight }),
      stop: () => { cancelAnimationFrame(frame); element.scrollTo = original; } };
  });
  try {
    const start = Date.now();
    for (let index = 0; index < 3; index++) {
      const wait = start + index * 80 - Date.now();
      if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
      send({ type: "assistant_text", seq: 41 + index,
        content: `Short reply ${index}\n\n中文短段落保持自然阅读。\n\nA final short paragraph.` });
    }
    await expect(scroller.getByText("Short reply 2", { exact: true })).toBeAttached();
    await settle();
    await expect.poll(gap).toBeLessThanOrEqual(2);
    const result = await trace.evaluate((record) => record.snapshot());
    await info.attach("reply-motion.json", { contentType: "application/json", body: JSON.stringify(result) });
    expect(result.growth, "the fixed reply sample grows approximately 300px").toBeGreaterThanOrEqual(250);
    expect(result.growth).toBeLessThanOrEqual(400);
    const fullGapWrites = result.writes.filter((write) => write.gap > 20 && Math.abs(write.after - write.before - write.gap) <= 2);
    expect(fullGapWrites.length, "separate replies must not each produce a full-gap jump").toBeLessThanOrEqual(1);
    const progress = result.samples.filter((sample, index, all) => index > 0 &&
      sample.top > all[index - 1].top && sample.bottom - sample.top > 2);
    expect(progress.length, "the rendered viewport shows intermediate forward positions").toBeGreaterThanOrEqual(3);
    for (let index = 0; index < 3; index++) await expect(scroller.getByText(`Short reply ${index}`, { exact: true })).toHaveCount(1);
  } finally { await trace.evaluate((record) => record.stop()); await trace.dispose(); }
});

test("continuous and delayed output stays pinned while history reading stays parked", async ({ page }) => {
  const { scroller, gap, send, settle, output } = await openChat(page);
  for (let block = 0; block < 4; block++) {
    send({ type: "assistant_delta", content: output(`Bottom ${block}`) });
    await expect(scroller.getByTestId("streaming-text")).toContainText(`Bottom ${block} line 12`);
    await expect.poll(gap).toBeLessThanOrEqual(2);
    // Also exercise growth after the library's 100ms autoscroll observation.
    await settle();
  }
  await scroller.hover();
  await page.mouse.wheel(0, -400);
  await expect.poll(gap).toBeGreaterThan(300);
  const before = await scroller.evaluate((element) => element.scrollTop);
  for (let block = 0; block < 4; block++) {
    send({ type: "assistant_delta", content: output(`History ${block}`) });
    await expect(scroller.getByTestId("streaming-text")).toContainText(`History ${block} line 12`);
    await settle();
    expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - before)).toBeLessThanOrEqual(2);
  }
  await page.mouse.wheel(0, 100000);
  await expect.poll(gap).toBeLessThanOrEqual(2);
  send({ type: "assistant_delta", content: output("Following again") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("Following again line 12");
  await expect.poll(gap).toBeLessThanOrEqual(2);
});

test("manual helper expansion preserves reading and returning to the bottom follows native output", async ({ page }) => {
  const { scroller, gap, send, output, settle } = await openChat(page);
  send({ type: "subagent", ...helper, seq: 41 });
  const card = scroller.locator(".subagent-card");
  await expect(card).toHaveCount(1);
  await expect.poll(gap).toBeLessThanOrEqual(2);
  for (let opening = 0; opening < 4; opening++) {
    const header = card.locator(".subagent-summary-row");
    await settle();
    const movement = await trackTop(header);
    await header.click();
    await expect(card.getByRole("region", { name: "Saved helper details" })).toBeVisible();
    await expect(card.getByText("Helper history was not collected. Viewing it cannot recover missing content.")).toBeVisible();
    await settle();
    expect(await movement.evaluate((tracker) => tracker.stop())).toBeLessThanOrEqual(2);
    await movement.dispose();
    if (opening < 3) {
      await card.locator(".subagent-summary-row").click();
      await expect(card.getByRole("region", { name: "Saved helper details" })).toHaveCount(0);
      await expect.poll(gap).toBeLessThanOrEqual(2);
    }
  }
  await scroller.hover({ position: { x: 5, y: 20 } });
  await page.mouse.wheel(0, 100000);
  await expect.poll(gap).toBeLessThanOrEqual(2);
  await expect(scroller.locator('[data-item-index="40"] .subagent-card')).toHaveCount(1);
  const height = (await card.boundingBox())!.height;
  send({ type: "assistant_delta", content: output("Helper live"), runtime_metadata: identity("helper", "item") });
  await expect(card.locator(".subagent-summary")).toContainText("Helper live line 12");
  expect((await card.boundingBox())!.height - height).toBeGreaterThan(100);
  await expect(scroller.locator('[data-item-index="40"] .subagent-card')).toHaveCount(1);
  await expect.poll(gap).toBeLessThanOrEqual(2);
  send({ type: "assistant_text", seq: 42, content: output("Helper live"), runtime_metadata: identity("helper", "item") });
  await expect.poll(gap).toBeLessThanOrEqual(2);
  for (const block of ["Native first", "Native second"]) {
    send({ type: "assistant_delta", content: output(block), runtime_metadata: identity("root", "root-item") });
    await expect(scroller.getByTestId("streaming-text")).toContainText(`${block} line 12`);
    await expect.poll(gap).toBeLessThanOrEqual(2);
  }
  send({ type: "assistant_text", seq: 43, content: output("Native first") + output("Native second"), runtime_metadata: identity("root", "root-item") });
  await expect(scroller.getByTestId("streaming-text")).toHaveCount(0);
  await expect(scroller.locator('[data-message-key="message:43"]')).toContainText("Native second line 12");
  await expect.poll(gap).toBeLessThanOrEqual(2);
});

test("hiding chat preserves history intent and following resumes only after returning to the bottom", async ({ page }) => {
  const { scroller, gap, send, settle, output, session } = await openChat(page);
  await scroller.hover();
  await page.mouse.wheel(0, -350);
  await expect.poll(gap).toBeGreaterThan(200);
  const before = await scroller.evaluate((element) => element.scrollTop);
  await page.getByRole("button", { name: /^Schedules/ }).click();
  await expect(scroller).toBeHidden();
  send({ type: "assistant_delta", content: output("Hidden output") });
  await page.locator(".session-item", { hasText: session.name }).click();
  await expect(scroller).toBeVisible();
  await settle();
  expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - before)).toBeLessThanOrEqual(2);
  await scroller.hover();
  await page.mouse.wheel(0, 100000);
  await expect.poll(gap).toBeLessThanOrEqual(2);
  await page.getByRole("button", { name: /^Schedules/ }).click();
  await expect(scroller).toBeHidden();
  send({ type: "assistant_delta", content: output("Hidden while following") });
  await page.locator(".session-item", { hasText: session.name }).click();
  await expect(scroller).toBeVisible();
  await expect.poll(gap).toBeLessThanOrEqual(2);
});

for (const cadence of ["quick", "sustained", "held"] as const) {
test(`native touch panning returns to the bottom and resumes following after pointer cancellation (${cadence})`, async ({ page }, info) => {
  const { scroller, gap, send, settle, output } = await openChat(page);
  await scroller.hover();
  await page.mouse.wheel(0, cadence === "quick" ? -200 : -350);
  await settle();
  await expect.poll(gap).toBeGreaterThan(150);
  const trace = await scroller.evaluateHandle((element) => {
    const events: { type: string; trusted: boolean; top: number; at: number; y?: number }[] = [];
    const types = ["pointerdown", "pointermove", "pointercancel", "pointerup", "touchstart", "touchmove", "touchend", "touchcancel", "scroll", "scrollend"];
    const record = (event: Event) => events.push({ type: event.type, trusted: event.isTrusted,
      top: element.scrollTop, at: performance.now(), y: event instanceof PointerEvent ? event.clientY : undefined });
    for (const type of types) element.addEventListener(type, record, { capture: true });
    return { events: () => events, stop: () => {
      for (const type of types) element.removeEventListener(type, record, { capture: true });
    } };
  });
  const touch = await page.context().newCDPSession(page);
  try {
    await touch.send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 2 });
    const box = (await scroller.boundingBox())!;
    const x = box.x + box.width / 2, y = box.y + box.height - 40;
    await touch.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y, id: 1 }] });
    const distances = cadence === "sustained" ? Array.from({ length: 16 }, (_, index) => (index + 1) * 30) : [30, 100, 200, 350, 450];
    for (const distance of distances) {
      await touch.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y: y - distance, id: 1 }] });
      await scroller.evaluate(async (_element, frames) => {
        for (let frame = 0; frame < frames; frame++) await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      }, cadence === "quick" ? 1 : cadence === "held" && distance === 100 ? 40 : 4);
    }
    await touch.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await expect.poll(gap).toBeLessThanOrEqual(2);
    const events = await trace.evaluate((record) => record.events());
    const cancelled = events.find((event) => event.type === "pointercancel" && event.trusted);
    expect(cancelled, "the browser must actually take over native panning").toBeDefined();
    expect(events.some((event) => event.type === "scroll" && event.trusted &&
      event.at >= cancelled!.at && event.top > cancelled!.top)).toBe(true);
    if (cadence !== "quick") {
      const lastScroll = events.filter((event) => event.type === "scroll").at(-1)!;
      expect(lastScroll.at - cancelled!.at, "exercise active panning beyond the evidence inactivity window").toBeGreaterThan(500);
    }
    send({ type: "assistant_delta", content: output("After native touch return") });
    await expect(scroller.getByTestId("streaming-text")).toContainText("After native touch return line 12");
    await settle();
    await expect.poll(gap).toBeLessThanOrEqual(2);
  } finally {
    await info.attach("native-touch-events.json", { contentType: "application/json",
      body: JSON.stringify(await trace.evaluate((record) => record.events())) });
    await trace.evaluate((record) => record.stop());
    await trace.dispose();
    await touch.send("Emulation.setTouchEmulationEnabled", { enabled: false });
    await touch.detach();
  }
});
}

test("reversing native touch panning after reaching the bottom keeps reading paused", async ({ page }, info) => {
  const { scroller, gap, send, settle, output } = await openChat(page);
  await scroller.hover(); await page.mouse.wheel(0, -350); await settle();
  await expect.poll(gap).toBeGreaterThan(300);
  const trace = await scroller.evaluateHandle((element) => {
    const events: { type: string; trusted: boolean; top: number }[] = [];
    const record = (event: Event) => events.push({ type: event.type, trusted: event.isTrusted, top: element.scrollTop });
    for (const type of ["pointercancel", "touchmove", "touchend", "scroll"]) element.addEventListener(type, record);
    return { events: () => events, stop: () => {
      for (const type of ["pointercancel", "touchmove", "touchend", "scroll"]) element.removeEventListener(type, record);
    } };
  });
  const touch = await page.context().newCDPSession(page);
  try {
    await touch.send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 1 });
    const box = (await scroller.boundingBox())!;
    const x = box.x + box.width / 2, y = box.y + box.height - 40;
    await touch.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y, id: 1 }] });
    const move = async (distance: number) => {
      await touch.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y: y - distance, id: 1 }] });
      await scroller.evaluate(async () => {
        for (let frame = 0; frame < 4; frame++) await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      });
    };
    for (const distance of [30, 100, 300, 450]) await move(distance);
    await expect.poll(gap).toBeLessThanOrEqual(2);
    const held = await scroller.evaluate((element) => element.scrollTop);
    send({ type: "assistant_delta", content: output("While contact remains held") });
    await expect(scroller.getByTestId("streaming-text")).toContainText("While contact remains held line 12");
    await settle();
    expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - held)).toBeLessThanOrEqual(2);
    for (const distance of [350, 250, 150]) await move(distance);
    await touch.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await settle();
    const events = await trace.evaluate((record) => record.events());
    const cancellation = events.findIndex((event) => event.type === "pointercancel" && event.trusted);
    expect(cancellation).toBeGreaterThanOrEqual(0);
    expect(events.slice(cancellation + 1).some((event) => event.type === "touchmove" && event.trusted)).toBe(true);
    const parked = await scroller.evaluate((element) => element.scrollTop);
    expect(held - parked, "the same native gesture actually reversed the main list").toBeGreaterThan(200);
    send({ type: "assistant_delta", content: output("After reversing native touch") });
    await expect(scroller.getByTestId("streaming-text")).toContainText("After reversing native touch line 12");
    await settle();
    expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - parked)).toBeLessThanOrEqual(2);
  } finally {
    await info.attach("native-touch-reversal.json", { contentType: "application/json", body: JSON.stringify(await trace.evaluate((record) => record.events())) });
    await trace.evaluate((record) => record.stop()); await trace.dispose(); await touch.detach();
  }
});

test("touch release hands ongoing forward scroll evidence to the viewport until it reaches the bottom", async ({ page }) => {
  const { scroller, gap, send, settle, output } = await openChat(page);
  await scroller.hover(); await page.mouse.wheel(0, -350); await settle();
  await expect.poll(gap).toBeGreaterThan(300);
  // Controlled platform ordering, not a real fling: hold scrollend until the
  // whole progression completes. Programmatic scroll steps otherwise emit
  // separate scrollend events and do not model ongoing native inertia.
  const elapsed = await scroller.evaluate(async (element) => {
    const stopPrematureEnd = (event: Event) => { if (event.target === element && event.isTrusted) event.stopImmediatePropagation(); };
    document.addEventListener("scrollend", stopPrematureEnd, true);
    const point = (y: number, identifier = 71) => new Touch({ identifier, target: element, clientX: 400, clientY: y });
    const emit = (type: string, touches: Touch[], changedTouches = touches) => element.dispatchEvent(new TouchEvent(type, { bubbles: true, touches, targetTouches: touches, changedTouches }));
    const start = performance.now();
    try {
      element.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerId: 7, pointerType: "touch", isPrimary: true, clientY: 400 }));
      emit("touchstart", [point(400)]);
      emit("touchmove", [point(300)]);
      element.dispatchEvent(new PointerEvent("pointercancel", { bubbles: true, pointerId: 7, pointerType: "touch", isPrimary: true }));
      // An unrelated changed touch cannot release the current owner. Pointer
      // and Touch identifiers deliberately come from different number spaces.
      emit("touchend", [point(300)], [point(300, 72)]);
      emit("touchend", [], [point(300)]);
      const from = element.scrollTop;
      const bottom = element.scrollHeight - element.clientHeight;
      for (const fraction of [0.3, 0.65, 1]) {
        for (let frame = 0; frame < 20; frame++) await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
        element.scrollTop = from + (bottom - from) * fraction;
        element.dispatchEvent(new Event("scroll"));
      }
      element.dispatchEvent(new Event("scrollend"));
      return performance.now() - start;
    } finally { document.removeEventListener("scrollend", stopPrematureEnd, true); }
  });
  expect(elapsed, "ongoing progress outlives a single absolute evidence window").toBeGreaterThan(600);
  await expect.poll(gap).toBeLessThanOrEqual(2);
  send({ type: "assistant_delta", content: output("After released touch progression") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("After released touch progression line 12");
  await settle();
  await expect.poll(gap).toBeLessThanOrEqual(2);
});

test("a native pinch cannot restore paused following", async ({ page }, info) => {
  const { scroller, gap, send, settle, output } = await openChat(page);
  await scroller.hover(); await page.mouse.wheel(0, -200); await settle();
  await expect.poll(gap).toBeGreaterThan(150);
  const trace = await scroller.evaluateHandle((element) => {
    const events: { type: string; primary: boolean; trusted: boolean }[] = [];
    const record = (event: Event) => {
      const pointer = event as PointerEvent;
      events.push({ type: event.type, primary: pointer.isPrimary, trusted: event.isTrusted });
    };
    element.addEventListener("pointerdown", record); element.addEventListener("pointercancel", record);
    return { events: () => events, stop: () => {
      element.removeEventListener("pointerdown", record); element.removeEventListener("pointercancel", record);
    } };
  });
  const touch = await page.context().newCDPSession(page);
  try {
    await touch.send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 2 });
    const box = (await scroller.boundingBox())!;
    const x = box.x + box.width / 2, y = box.y + box.height / 2;
    const initialScale = await page.evaluate(() => visualViewport!.scale);
    await touch.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: x - 40, y, id: 1 }] });
    // A small first movement precedes the second contact, before native pan
    // takeover. It must not leave usable one-finger evidence for the pinch.
    await touch.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: x - 40, y: y - 5, id: 1 }] });
    await touch.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [
      { x: x - 40, y: y - 5, id: 1 }, { x: x + 40, y: y + 5, id: 2 },
    ] });
    for (const distance of [55, 75, 100]) {
      await touch.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [
        { x: x - distance, y: y - 5, id: 1 }, { x: x + distance, y: y + 5, id: 2 },
      ] });
      await scroller.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
    }
    await touch.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await expect.poll(() => page.evaluate(() => visualViewport!.scale)).toBeGreaterThan(initialScale + 0.1);
    const events = await trace.evaluate((record) => record.events());
    expect(events.some((event) => event.type === "pointerdown" && !event.primary && event.trusted)).toBe(true);
    expect(events.some((event) => event.type === "pointercancel" && event.trusted)).toBe(true);
    await settle();
    const parked = await scroller.evaluate((element) => element.scrollTop);
    send({ type: "assistant_delta", content: output("While pinching") });
    await expect(scroller.getByTestId("streaming-text")).toContainText("While pinching line 12");
    await settle();
    expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - parked)).toBeLessThanOrEqual(2);
    await expect.poll(gap).toBeGreaterThan(150);
  } finally {
    await info.attach("native-pinch-events.json", { contentType: "application/json",
      body: JSON.stringify(await trace.evaluate((record) => record.events())) });
    await trace.evaluate((record) => record.stop()); await trace.dispose();
    await touch.detach();
  }
});

test("native touch scrolling inside tool output leaves main chat reading paused", async ({ page }) => {
  const { scroller, send, settle, output } = await openChat(page);
  send({ type: "tool_result", seq: 41, output: Array.from({ length: 100 }, (_, index) => `Inner line ${index}`).join("\n") });
  const detail = scroller.locator(".msg-tool-result .tool-detail");
  await scroller.locator(".msg-tool-result .tool-header").click();
  await expect(detail).toBeVisible();
  await detail.scrollIntoViewIfNeeded(); await settle();
  const parked = await scroller.evaluate((element) => element.scrollTop);
  const innerTop = await detail.evaluate((element) => element.scrollTop);
  const touch = await page.context().newCDPSession(page);
  try {
    await touch.send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 1 });
    const box = (await detail.boundingBox())!;
    const x = box.x + box.width / 2, y = box.y + box.height - 20;
    await touch.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y, id: 1 }] });
    for (const distance of [30, 100, 180]) {
      await touch.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y: y - distance, id: 1 }] });
      await detail.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
    }
    await touch.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await expect.poll(() => detail.evaluate((element) => element.scrollTop)).toBeGreaterThan(innerTop + 100);
    await settle();
    expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - parked)).toBeLessThanOrEqual(2);
    send({ type: "assistant_delta", content: output("After inner touch") });
    await expect(scroller.getByTestId("streaming-text")).toContainText("After inner touch line 12");
    await settle();
    expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - parked)).toBeLessThanOrEqual(2);
  } finally { await touch.detach(); }
});

test("a cancelled non-navigating touch cannot reuse older downward navigation for a late scroll", async ({ page }) => {
  const { scroller, send, settle, output, gap } = await openChat(page);
  await scroller.hover(); await page.mouse.wheel(0, -200); await settle();
  await expect.poll(gap).toBeGreaterThan(150);
  // Controlled event ordering: the earlier wheel has no native displacement,
  // the later scroll stands for a delayed application/layout notification.
  await scroller.evaluate((element) => {
    element.dispatchEvent(new WheelEvent("wheel", { bubbles: true, deltaY: 100 }));
    element.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerId: 7, pointerType: "touch", isPrimary: true, clientY: 400 }));
    element.dispatchEvent(new PointerEvent("pointercancel", { bubbles: true, pointerId: 7, pointerType: "touch", isPrimary: true, clientY: 400 }));
    element.scrollTop = element.scrollHeight;
    element.dispatchEvent(new Event("scroll"));
  });
  await expect.poll(gap).toBeLessThanOrEqual(2);
  const parked = await scroller.evaluate((element) => element.scrollTop);
  send({ type: "assistant_delta", content: output("After cancelled tap") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("After cancelled tap line 12");
  await settle();
  expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - parked)).toBeLessThanOrEqual(2);
  await expect.poll(gap).toBeGreaterThan(150);
});

test("cancelling the Touch owner discards its direction before a late main-list scroll", async ({ page }) => {
  const { scroller, send, settle, output, gap } = await openChat(page);
  await scroller.hover(); await page.mouse.wheel(0, -200); await settle();
  await expect.poll(gap).toBeGreaterThan(150);
  // Controlled cancellation and delayed displacement. Unlike a native pan's
  // pointercancel, touchcancel ends the owned TouchEvent stream itself.
  await scroller.evaluate((element) => {
    const point = (y: number) => new Touch({ identifier: 71, target: element, clientX: 400, clientY: y });
    const emit = (type: string, touches: Touch[], changedTouches = touches) => element.dispatchEvent(new TouchEvent(type, { bubbles: true, touches, targetTouches: touches, changedTouches }));
    element.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerId: 7, pointerType: "touch", isPrimary: true, clientY: 400 }));
    emit("touchstart", [point(400)]);
    emit("touchmove", [point(300)]);
    element.dispatchEvent(new PointerEvent("pointercancel", { bubbles: true, pointerId: 7, pointerType: "touch", isPrimary: true }));
    emit("touchcancel", [], [point(300)]);
    element.scrollTop = element.scrollHeight;
    element.dispatchEvent(new Event("scroll"));
  });
  await expect.poll(gap).toBeLessThanOrEqual(2);
  const parked = await scroller.evaluate((element) => element.scrollTop);
  send({ type: "assistant_delta", content: output("After Touch cancellation") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("After Touch cancellation line 12");
  await settle();
  expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - parked)).toBeLessThanOrEqual(2);
  await expect.poll(gap).toBeGreaterThan(150);
});

for (const navigation of ["wheel", "PageUp", "scrollbar", "touch"] as const) {
test(`upward ${navigation} invalidates measured output work before its animation frame`, async ({ page }) => {
  const { scroller, gap, send, settle, output } = await openChat(page);
  const touch = navigation === "touch" ? await page.context().newCDPSession(page) : null;
  if (touch) {
    await touch.send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 1 });
    await settle();
  }
  if (navigation === "scrollbar") {
    await scroller.evaluate((element) => { element.style.scrollbarGutter = "stable"; });
    await settle();
  }
  if (navigation === "PageUp") await scroller.focus();
  await scroller.hover();
  const before = await scroller.evaluate((element) => element.scrollTop);
  // Delay browser frames after the real view has mounted. Flush one batch of
  // measurement notifications, then interrupt before their position writes.
  const frames = await page.evaluateHandle(() => {
    const request = window.requestAnimationFrame;
    const cancel = window.cancelAnimationFrame;
    let serial = 0;
    const pending = new Map<number, FrameRequestCallback>();
    let movement: { top: number; after: number } | null = null;
    let removeNavigationListener = () => {};
    const flush = () => {
      for (const id of [...pending.keys()]) {
        const callback = pending.get(id);
        pending.delete(id);
        callback?.(performance.now());
      }
    };
    window.requestAnimationFrame = (callback) => { const id = --serial; pending.set(id, callback); return id; };
    window.cancelAnimationFrame = (id) => { if (id < 0) pending.delete(id); else cancel(id); };
    return {
      count: () => pending.size,
      flush,
      watchNavigation: (element: HTMLElement, before: number) => {
        const onScroll = () => {
          if (element.scrollTop >= before) return;
          removeNavigationListener();
          const top = element.scrollTop;
          // Run pending writes at the first actual navigation event, before
          // keyboard animation can mask an unwanted return to the bottom.
          flush();
          movement = { top, after: element.scrollTop };
        };
        element.addEventListener("scroll", onScroll);
        removeNavigationListener = () => element.removeEventListener("scroll", onScroll);
      },
      movement: () => movement,
      release: () => {
        removeNavigationListener();
        window.requestAnimationFrame = request;
        window.cancelAnimationFrame = cancel;
        for (const callback of pending.values()) request(callback);
        pending.clear();
      },
    };
  });
  try {
    send({ type: "assistant_delta", content: output("Queued output") });
    await expect(scroller.getByTestId("streaming-text")).toContainText("Queued output line 12");
    await expect.poll(() => frames.evaluate((gate) => gate.count())).toBeGreaterThan(0);
    await frames.evaluate((gate) => gate.flush());
    await expect.poll(() => frames.evaluate((gate) => gate.count())).toBeGreaterThan(0);
    if (navigation !== "wheel") {
      const element = await scroller.elementHandle();
      await frames.evaluate((gate, args) => gate.watchNavigation(args.element! as HTMLElement, args.before), { element, before });
    }
    if (navigation === "wheel") await page.mouse.wheel(0, -350);
    else if (navigation === "PageUp") await page.keyboard.press("PageUp");
    else if (touch) {
      const box = (await scroller.boundingBox())!;
      const x = box.x + box.width / 2;
      const y = box.y + 80;
      await touch.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y, id: 1 }] });
      await touch.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y: y + 30, id: 1 }] });
      await touch.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y: y + 400, id: 1 }] });
      await touch.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    }
    else {
      const thumb = await scroller.evaluate(scrollbarThumb);
      expect(thumb.gutter).toBeGreaterThan(0);
      await page.mouse.move(thumb.x, thumb.y);
      await page.mouse.down();
      await page.mouse.move(thumb.x, thumb.y - 120);
      await page.mouse.up();
    }
    if (navigation !== "wheel") {
      await expect.poll(() => frames.evaluate((gate) => gate.movement())).not.toBeNull();
      const movement = await frames.evaluate((gate) => gate.movement());
      expect.soft(movement!.after - movement!.top).toBeLessThanOrEqual(2);
    }
    await expect.poll(() => scroller.evaluate((element) => element.scrollTop)).toBeLessThan(before - 200);
  } finally {
    await frames.evaluate((gate) => gate.release());
    await frames.dispose();
    if (touch) {
      await touch.send("Emulation.setTouchEmulationEnabled", { enabled: false });
      await touch.detach();
    }
  }
  if (navigation === "PageUp" || navigation === "touch") await settle();
  const jump = page.getByRole("button", { name: "Jump to latest" });
  await expect(jump).toBeVisible();
  const parked = await scroller.evaluate((element) => element.scrollTop);
  await settle();
  expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - parked)).toBeLessThanOrEqual(2);
  send({ type: "assistant_delta", content: output("After interruption") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("After interruption line 12");
  await settle();
  expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - parked)).toBeLessThanOrEqual(2);
  expect(await gap()).toBeGreaterThan(500);
  await scroller.hover();
  await page.mouse.wheel(0, 100000);
  await expect.poll(gap).toBeLessThanOrEqual(2);
  await expect(jump).toHaveCount(0);
  send({ type: "assistant_delta", content: output("After returning") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("After returning line 12");
  await expect.poll(gap).toBeLessThanOrEqual(2);
});
}

for (const readingHistory of [false, true]) {
test(`holding the native scrollbar during output preserves ${readingHistory ? "history reading" : "bottom following"} without dragging`, async ({ page }) => {
  const { scroller, gap, send, settle, output } = await openChat(page);
  if (readingHistory) {
    await scroller.hover();
    await page.mouse.wheel(0, -350);
    await expect.poll(gap).toBeGreaterThan(200);
  }
  const before = await scroller.evaluate((element) => element.scrollTop);
  const thumb = await scroller.evaluate(scrollbarThumb);
  expect(thumb.gutter).toBeGreaterThan(0);
  await page.mouse.move(thumb.x, thumb.y);
  await page.mouse.down();
  try {
    send({ type: "assistant_delta", content: output("While holding scrollbar") });
    await expect(scroller.getByTestId("streaming-text")).toContainText("While holding scrollbar line 12");
    await settle();
    await expect.poll(gap).toBeGreaterThan(100);
    expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - before)).toBeLessThanOrEqual(2);
    if (!readingHistory) await expect(page.getByRole("button", { name: "Jump to latest" })).toHaveCount(0);
  } finally {
    await page.mouse.up();
  }
  if (!readingHistory) await expect.poll(gap).toBeLessThanOrEqual(2);
  send({ type: "assistant_delta", content: output("After scrollbar press") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("After scrollbar press line 12");
  await settle();
  if (readingHistory) {
    expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - before)).toBeLessThanOrEqual(2);
  } else await expect.poll(gap).toBeLessThanOrEqual(2);
});
}

test("clicking above the native scrollbar thumb offers a return to the latest output", async ({ page }) => {
  const { scroller, gap, settle } = await openChat(page);
  await scroller.evaluate((element) => { element.style.scrollbarGutter = "stable"; });
  await settle();
  const thumb = await scroller.evaluate(scrollbarThumb);
  const trackY = (await scroller.boundingBox())!.y + 64;
  expect(thumb.gutter).toBeGreaterThan(0);
  await page.mouse.move(thumb.x, trackY);
  await page.mouse.down();
  try {
    await expect.poll(gap).toBeGreaterThan(200);
  } finally {
    await page.mouse.up();
  }
  await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
});

test("reversing a native scrollbar drag upward offers a return to the latest output", async ({ page }) => {
  const { scroller, gap, send, settle } = await openChat(page, 4);
  const jump = page.getByRole("button", { name: "Jump to latest" });
  await scroller.evaluate((element) => { element.style.scrollbarGutter = "stable"; });
  // Establish a stable scroll range while following. Output received after
  // expansion could independently offer the return before any thumb input.
  send({ type: "assistant_text", seq: 5, content: "Already saved before reading.\n\n".repeat(80) });
  send({ type: "tool_result", seq: 6, output: "Open this saved detail without scrolling." });
  const header = scroller.locator(".msg-tool-result .tool-header");
  await expect.poll(gap).toBeLessThanOrEqual(2);
  await expect(header).toBeInViewport();
  await header.click();
  await expect.poll(() => scroller.evaluate((element) => element.scrollHeight - element.clientHeight)).toBeGreaterThan(700);
  await scroller.evaluate((element) => {
    element.scrollTop = element.scrollHeight - element.clientHeight - 450;
  });
  await settle();
  await expect(jump).toHaveCount(0);

  const top = () => scroller.evaluate((element) => element.scrollTop);
  const before = await top();
  const thumb = await scroller.evaluate(scrollbarThumb);
  expect(thumb.gutter).toBeGreaterThan(0);
  await page.mouse.move(thumb.x, thumb.y);
  await page.mouse.down();
  try {
    await page.mouse.move(thumb.x, thumb.y + 24);
    await expect.poll(top).toBeGreaterThan(before + 40);
    await settle();
    await expect(jump).toHaveCount(0);
    await page.mouse.move(thumb.x, thumb.y - 80);
    await expect.poll(top).toBeLessThan(before - 100);
    await expect(jump).toBeVisible();
  } finally {
    await page.mouse.up();
  }
});

test("Home offers a return when layout grows before the first scroll notification", async ({ page }) => {
  const { scroller, gap } = await openChat(page);
  await scroller.evaluate((element) => {
    const original = element.scrollTo.bind(element);
    element.scrollTo = ((options: ScrollToOptions | number, y?: number) => {
      if (typeof options === "number") original(options, y!);
      else original(options);
      if (typeof options === "number" || options.top !== 0 || options.behavior !== "instant") return;
      const viewport = element.querySelector("[data-viewport-type]")!;
      const spacer = document.createElement("div");
      spacer.style.height = "120px";
      spacer.dataset.layoutAfterHome = String(element.scrollTop);
      viewport.appendChild(spacer);
    }) as typeof element.scrollTo;
  });
  await scroller.press("Home");
  await expect(scroller.locator("[data-layout-after-home]")).toHaveAttribute("data-layout-after-home", "0");
  await expect.poll(gap).toBeGreaterThan(200);
  await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
});

for (const intent of ["following", "reading"] as const) {
  test(`a cancelled native wheel preserves ${intent} through new output`, async ({ page }) => {
    const { scroller, gap, send, settle } = await openChat(page);
    await scroller.hover();
    if (intent === "reading") {
      await page.mouse.wheel(0, -350);
      await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
      // A layout/application write can reach bottom without restoring reading intent.
      await scroller.evaluate((element) => { element.scrollTop = element.scrollHeight; });
      await settle();
      await expect.poll(gap).toBeLessThanOrEqual(2);
    }
    const before = await scroller.evaluate((element) => {
      element.addEventListener("wheel", (event) => {
        event.preventDefault();
        event.stopPropagation();
        element.dataset.wheelResult = `${event.isTrusted}/${event.defaultPrevented}`;
      }, { once: true, passive: false });
      return element.scrollTop;
    });
    await page.mouse.wheel(0, intent === "following" ? -120 : 120);
    await expect(scroller).toHaveAttribute("data-wheel-result", "true/true");
    send({ type: "assistant_delta", content: ("New lines of output.\n").repeat(24) + "Static Footer final marker" });
    await expect(scroller.getByTestId("streaming-text")).toContainText("Static Footer final marker");
    await settle();
    if (intent === "following") {
      await expectFooterMarkerInViewport(scroller);
      await expect.poll(gap).toBeLessThanOrEqual(2);
    } else {
      expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - before)).toBeLessThanOrEqual(2);
      expect(await gap()).toBeGreaterThan(100);
    }
  });
}

test("touch and pen direction without main-list movement preserve the readable tail", async ({ page }) => {
  const { scroller, send, settle } = await openChat(page);
  const movement = await scroller.evaluate((element) => {
    const before = element.scrollTop;
    const tap = (clientY: number) => new Touch({ identifier: 71, target: element, clientX: 400, clientY });
    const touch = (type: string, next: Touch, touches = [next]) => {
      element.dispatchEvent(new TouchEvent(type, { bubbles: true, touches, changedTouches: [next] }));
    };
    touch("touchstart", tap(400));
    touch("touchmove", tap(405));
    touch("touchend", tap(405), []);
    for (const [type, clientY] of [["pointerdown", 400], ["pointermove", 405], ["pointercancel", 405]] as const) {
      element.dispatchEvent(new PointerEvent(type, { bubbles: true, pointerId: 7, pointerType: "pen", isPrimary: true, clientY }));
    }
    return element.scrollTop - before;
  });
  expect(movement).toBe(0);
  send({ type: "assistant_delta", content: ("New output after contact.\n").repeat(24) + "Static Footer final marker" });
  await expect(scroller.getByTestId("streaming-text")).toContainText("Static Footer final marker");
  await settle();
  await expectFooterMarkerInViewport(scroller);
});

for (const source of ["touch", "pen"] as const) {
  test(`a zero-movement ${source} release does not borrow a queued scroll into the bottom`, async ({ page }) => {
    const { scroller, gap, send, settle } = await openChat(page);
    await scroller.hover();
    await page.mouse.wheel(0, -350);
    await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
    await settle();
    const parked = await scroller.evaluate((element, input) => {
      // The application's write precedes contact, but its scroll event follows it.
      element.scrollTop = element.scrollHeight;
      const top = element.scrollTop;
      if (input === "touch") {
        for (const [type, clientY] of [["touchstart", 400], ["touchmove", 395], ["touchend", 395]] as const) {
          const point = new Touch({ identifier: 71, target: element, clientX: 400, clientY });
          element.dispatchEvent(new TouchEvent(type, { bubbles: true,
            touches: type === "touchend" ? [] : [point], changedTouches: [point] }));
        }
      } else {
        for (const [type, clientY] of [["pointerdown", 400], ["pointermove", 395], ["pointerup", 395]] as const) {
          element.dispatchEvent(new PointerEvent(type, { bubbles: true, pointerId: 7, pointerType: "pen", isPrimary: true, clientY }));
        }
      }
      return top;
    }, source);
    await settle();
    send({ type: "assistant_delta", content: ("The reading position stays here.\n").repeat(24) });
    await expect(scroller.getByTestId("streaming-text")).toContainText("The reading position stays here.");
    await settle();
    expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - parked)).toBeLessThanOrEqual(2);
    expect(await gap()).toBeGreaterThan(100);
  });
}

for (const scenario of ["following", "reading", "layout clamp"] as const) {
  const title = scenario === "layout clamp"
    ? "a touch preserves following when a layout clamp restores the original range"
    : `touch direction preserves ${scenario} intent when another source moved the list`;
  test(title, async ({ page }) => {
    const { scroller, gap, send, settle } = await openChat(page);
    if (scenario === "reading") {
      await scroller.hover();
      await page.mouse.wheel(0, -350);
      await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
    }
    if (scenario === "layout clamp") {
      await scroller.evaluate((viewport) => {
        viewport.style.overflowAnchor = "none";
        const spacer = document.createElement("div");
        spacer.dataset.touchLayoutSpacer = "";
        spacer.style.height = "120px";
        viewport.querySelector("[data-viewport-type]")!.append(spacer);
        viewport.scrollTop = viewport.scrollHeight;
      });
      await settle();
      await expect.poll(gap).toBeLessThanOrEqual(2);
    }
    const touch = await controlledTouch(scroller);
    let movement: { before: number; after: number; rangeRestored: boolean };
    try {
      movement = await touch.evaluate(({ viewport, emit }, scenario) => {
        const before = viewport.scrollTop;
        const maximum = viewport.scrollHeight - viewport.clientHeight;
        emit("touchstart", 400);
        if (scenario === "layout clamp") {
          emit("touchmove", 405);
          const spacer = viewport.querySelector<HTMLElement>("[data-touch-layout-spacer]")!;
          spacer.style.height = "0px";
          const after = viewport.scrollTop;
          spacer.style.height = "120px";
          viewport.dispatchEvent(new Event("scroll"));
          emit("touchend", 405);
          return { before, after, rangeRestored: maximum === viewport.scrollHeight - viewport.clientHeight };
        }
        viewport.scrollTop = scenario === "reading" ? maximum : before - 120;
        viewport.dispatchEvent(new Event("scroll"));
        const y = scenario === "reading" ? 395 : 405;
        emit("touchmove", y);
        emit("touchend", y);
        return { before, after: viewport.scrollTop, rangeRestored: true };
      }, scenario);
    } finally { await touch.dispose(); }
    expect(movement.rangeRestored).toBe(true);
    expect(Math.abs(movement.before - movement.after)).toBeGreaterThan(60);
    send({ type: "assistant_delta", content: ("New output after the touch.\n").repeat(24) + "Static Footer final marker" });
    await expect(scroller.getByTestId("streaming-text")).toContainText("Static Footer final marker");
    await settle();
    if (scenario === "reading") {
      expect(Math.abs(await scroller.evaluate((viewport) => viewport.scrollTop) - movement.after)).toBeLessThanOrEqual(2);
      expect(await gap()).toBeGreaterThan(100);
    } else {
      await expect(page.getByRole("button", { name: "Jump to latest" })).toHaveCount(0);
      await expectFooterMarkerInViewport(scroller);
    }
  });
}

for (const boundary of ["top before the next move", "hidden text change", "a reversal before scroll reports", "3px from bottom"] as const) {
  test(`touch navigation survives ${boundary}`, async ({ page }) => {
    const { scroller, gap, send, settle } = await openChat(page);
    const down = boundary === "3px from bottom";
    if (down) {
      await scroller.hover();
      await page.mouse.wheel(0, -350);
      await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
      await settle();
    }
    const touch = await controlledTouch(scroller);
    try {
      const movement = await touch.evaluate(({ viewport, emit }, boundary) => {
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        const body = document.createElement("div");
        if (boundary === "hidden text change") {
          summary.textContent = "Visible summary";
          details.append(summary, body);
          viewport.append(details);
        }
        const maximum = viewport.scrollHeight - viewport.clientHeight;
        if (boundary === "3px from bottom") viewport.scrollTop = maximum - 3;
        const from = viewport.scrollTop;
        emit("touchstart", 400);
        if (boundary === "3px from bottom") {
          emit("touchmove", 395);
          emit("touchend", 395);
          viewport.scrollTop = maximum;
          viewport.dispatchEvent(new Event("scroll"));
        } else {
          emit("touchmove", 425);
          if (boundary === "top before the next move") {
            viewport.scrollTop = 0;
            emit("touchmove", 450);
          } else if (boundary === "a reversal before scroll reports") {
            viewport.scrollTop -= 280;
            emit("touchmove", 395);
            viewport.scrollTop += 40;
          } else {
            viewport.scrollTop -= 240;
            body.textContent = "A hidden text update alongside an actual scroll";
          }
          viewport.dispatchEvent(new Event("scroll"));
          emit("touchend", 450);
        }
        return { from, to: viewport.scrollTop, range: viewport.scrollHeight - viewport.clientHeight };
      }, boundary);
      if (down) expect(movement.to - movement.from).toBe(3);
      else {
        expect(movement.from - movement.to).toBeGreaterThan(100);
        await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
      }
      send({ type: "assistant_delta", content: ("New text after the touch.\n").repeat(24) + "Static Footer final marker" });
      await expect(scroller.getByTestId("streaming-text")).toContainText("Static Footer final marker");
      await settle();
      if (down) await expectFooterMarkerInViewport(scroller);
      else {
        expect(await gap()).toBeGreaterThan(200);
        if (boundary === "top before the next move") await expect(scroller.getByText("Saved row 1", { exact: true })).toBeInViewport();
      }
    } finally { await touch.dispose(); }
  });
}

test("touch navigation survives physical progress past the rounded range under CSS zoom", async ({ page }) => {
  const { scroller, send, settle } = await openChat(page);
  await scroller.hover();
  await page.mouse.wheel(0, -350);
  await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
  await scroller.evaluate((viewport) => { viewport.style.zoom = "2"; });
  await settle();
  const touch = await controlledTouch(scroller);
  try {
    const movement = await touch.evaluate(({ viewport, emit }) => {
      const spacer = document.createElement("div");
      viewport.querySelector("[data-viewport-type]")!.append(spacer);
      for (let step = 0; step < 16; step++) {
        spacer.style.height = `${12 + step * 0.25}px`;
        const range = viewport.scrollHeight - viewport.clientHeight;
        viewport.scrollTop = viewport.scrollHeight;
        const physical = viewport.scrollTop;
        if (physical <= range) continue;
        viewport.scrollTop = range;
        if (viewport.scrollTop !== range) continue;
        emit("touchstart", 400);
        emit("touchmove", 390);
        emit("touchend", 390);
        viewport.scrollTop = physical;
        viewport.dispatchEvent(new Event("scroll"));
        return { from: range, to: viewport.scrollTop, rangeAfter: viewport.scrollHeight - viewport.clientHeight };
      }
      return null;
    });
    expect(movement).not.toBeNull();
    expect(movement!.rangeAfter).toBe(movement!.from);
    expect(movement!.to - movement!.from).toBeGreaterThan(0);
    expect(movement!.to - movement!.from).toBeLessThanOrEqual(1);
    send({ type: "assistant_delta", content: ("New text after the zoomed return.\n").repeat(24) + "Static Footer final marker" });
    await expect(scroller.getByTestId("streaming-text")).toContainText("Static Footer final marker");
    await expectFooterMarkerInViewport(scroller);
  } finally { await touch.dispose(); }
});

for (const source of ["root text", "native item"] as const) {
  test(`touch reading survives a same-line ${source} update before the scroll report`, async ({ page }) => {
    const { scroller, gap, send, settle } = await openChat(page);
    const runtime = source === "native item" ? { runtime_metadata: identity("root", "root-item") } : {};
    const preview = scroller.getByTestId("streaming-text");
    send({ type: "assistant_delta", content: "Short line", ...runtime });
    await expect(preview.getByText("Short line", { exact: true })).toBeVisible();
    await expect.poll(gap).toBeLessThanOrEqual(2);
    await settle();
    const touch = await controlledTouch(scroller);
    const scrollGate = await holdNativeScroll(scroller);
    try {
      const old = await touch.evaluate(({ viewport, emit }) => {
        emit("touchstart", 400);
        emit("touchmove", 450);
        const before = viewport.scrollTop;
        viewport.scrollTop = before - 160;
        return { before, top: viewport.scrollTop, range: viewport.scrollHeight - viewport.clientHeight };
      });
      expect(old.before - old.top).toBeGreaterThan(100);
      send({ type: "assistant_delta", content: "!", ...runtime });
      await expect(preview.getByText("Short line!", { exact: true })).toBeVisible();
      const updated = await touch.evaluate(({ viewport, emit }) => {
        const state = { top: viewport.scrollTop, range: viewport.scrollHeight - viewport.clientHeight };
        viewport.dispatchEvent(new Event("scroll"));
        emit("touchend", 450);
        return state;
      });
      expect(updated).toEqual({ top: old.top, range: old.range });
      await scrollGate.evaluate((gate) => gate.release());
      await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
      await settle();
      const parked = await scroller.evaluate((viewport) => viewport.scrollTop);
      send({ type: "assistant_delta", content: "\n" + "Additional output.\n".repeat(24), ...runtime });
      await expect(preview).toContainText("Additional output.");
      await settle();
      expect(Math.abs(await scroller.evaluate((viewport) => viewport.scrollTop) - parked)).toBeLessThanOrEqual(2);
      expect(await gap()).toBeGreaterThan(200);
    } finally {
      await scrollGate.evaluate((gate) => gate.release());
      await scrollGate.dispose();
      await touch.dispose();
    }
  });
}

for (const publication of ["running", "idle", "subagent card"] as const) {
  test(`touch reading survives ${publication} publication before the scroll report`, async ({ page }) => {
    const { scroller, gap, send, settle } = await openChat(page);
    const loading = scroller.locator(".msg-loading");
    const description = scroller.locator(".subagent-description");
    const card = { type: "subagent", task_id: "legacy-task", tool_call_id: "legacy-tool", name: "Helper", status: "running" };
    if (publication === "running") {
      send({ type: "status", status: "idle" });
      await expect(loading).toHaveCount(0);
    } else if (publication === "subagent card") {
      send({ type: "tool_use", seq: 41, tool: "Task", input: { description: "Inspect the issue" },
        runtime_metadata: { request_id: "request", attempt_id: "attempt", tool_call_id: "legacy-tool" } });
      send({ ...card, description: "Working" });
      await expect(description).toHaveText("Working");
    }
    await settle();
    const touch = await controlledTouch(scroller);
    const scrollGate = await holdNativeScroll(scroller);
    try {
      const movement = await touch.evaluate(({ viewport, emit }) => {
        const before = viewport.scrollTop;
        emit("touchstart", 400);
        emit("touchmove", 450);
        viewport.scrollTop = before - 120;
        return { before, top: viewport.scrollTop, range: viewport.scrollHeight - viewport.clientHeight };
      });
      expect(movement.before - movement.top).toBe(120);
      if (publication === "subagent card") {
        send({ ...card, description: "Working!" });
        await expect(description).toHaveText("Working!");
      } else {
        send({ type: "status", status: publication });
        await expect(loading).toHaveCount(publication === "running" ? 1 : 0);
      }
      const after = await touch.evaluate(({ viewport, emit }) => {
        const sample = { top: viewport.scrollTop, range: viewport.scrollHeight - viewport.clientHeight };
        viewport.dispatchEvent(new Event("scroll"));
        emit("touchend", 450);
        return sample;
      });
      expect(after.top).toBe(movement.top);
      if (publication === "subagent card") expect(Math.abs(after.range - movement.range)).toBeLessThanOrEqual(2);
      else expect(after.range).not.toBe(movement.range);
      await scrollGate.evaluate((gate) => gate.release());
      await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
      await settle();
      const parked = await scroller.evaluate((viewport) => viewport.scrollTop);
      if (publication === "idle") send({ type: "status", status: "running" });
      send({ type: "assistant_delta", content: ("Output after the state update.\n").repeat(24) });
      await expect(scroller.getByTestId("streaming-text")).toContainText("Output after the state update.");
      await settle();
      expect(Math.abs(await scroller.evaluate((viewport) => viewport.scrollTop) - parked)).toBeLessThanOrEqual(2);
      expect(await gap()).toBeGreaterThan(200);
    } finally {
      await scrollGate.evaluate((gate) => gate.release());
      await scrollGate.dispose();
      await touch.dispose();
    }
  });
}

test("touch reading survives a stationary range change before the next scroll", async ({ page }) => {
  const { scroller, gap, send, session, settle } = await openChat(page);
  const touch = await controlledTouch(scroller);
  try {
    const result = await touch.evaluate(async ({ viewport, emit }, sessionId) => {
      // Use the production event handler synchronously to keep the publication
      // and following movement in the same browser task.
      const modulePath = "/src/hooks/useWebSocket.ts";
      const { handleWsMessage }: typeof import("../src/hooks/useWebSocket") = await import(modulePath);
      const before = viewport.scrollTop;
      const range = viewport.scrollHeight - viewport.clientHeight;
      emit("touchstart", 400);
      emit("touchmove", 450);
      // This parent is outside the observed scroll subtree. ResizeObserver
      // can only report after this task, after the store publication and move.
      viewport.parentElement!.style.maxHeight = `${viewport.clientHeight - 80}px`;
      const resized = { top: viewport.scrollTop, range: viewport.scrollHeight - viewport.clientHeight };
      handleWsMessage({ type: "status", status: "idle", session_id: sessionId });
      viewport.scrollTop -= 120;
      viewport.dispatchEvent(new Event("scroll"));
      emit("touchend", 450);
      return { before, range, resized, after: viewport.scrollTop };
    }, session.id);
    expect(result.resized.top).toBe(result.before);
    expect(result.resized.range - result.range).toBeGreaterThan(40);
    expect(result.before - result.after).toBe(120);
    await expect(scroller.locator(".msg-loading")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
    await settle();
    const parked = await scroller.evaluate((viewport) => viewport.scrollTop);
    send({ type: "status", status: "running" });
    send({ type: "assistant_delta", content: "Output after the resized touch.\n".repeat(24) });
    await expect(scroller.getByTestId("streaming-text")).toContainText("Output after the resized touch.");
    await settle();
    expect(Math.abs(await scroller.evaluate((viewport) => viewport.scrollTop) - parked)).toBeLessThanOrEqual(2);
    expect(await gap()).toBeGreaterThan(200);
  } finally { await touch.dispose(); }
});

for (const notification of ["before", "after"] as const) {
test(`touch reading survives a scroll reported ${notification} history page merge`, async ({ page }) => {
  const { scroller, settle, pages, replaceOnReconnect } = await openChat(page);
  const row = (seq: number): Message => ({ role: "assistant", type: "text", seq, content: `History ${seq}` });
  replaceOnReconnect({ messages: Array.from({ length: 40 }, (_, index) => row(index + 200)),
    next_message_seq: 240, oldest_loaded_seq: 200, has_more_messages: true });
  await expect(scroller.getByText("History 239", { exact: true })).toBeInViewport();
  await scroller.evaluate((viewport) => {
    viewport.style.overflowAnchor = "none";
    viewport.scrollTop = 320;
  });
  await settle();
  await scroller.getByRole("button", { name: "Load earlier messages" }).evaluate((button) => (button as HTMLButtonElement).click());
  await expect.poll(() => pages.length).toBe(1);
  await expect(page.getByRole("button", { name: "Jump to latest" })).toHaveCount(0);
  const touch = await controlledTouch(scroller);
  const scrollGate = await holdNativeScroll(scroller);
  try {
    const moved = await touch.evaluate(({ viewport, emit }, notification) => {
      const before = viewport.scrollTop;
      emit("touchstart", 400);
      emit("touchmove", 500);
      viewport.scrollTop = 145;
      if (notification === "before") viewport.dispatchEvent(new Event("scroll"));
      return before - viewport.scrollTop;
    }, notification);
    expect(moved).toBeGreaterThan(100);
    await pages[0].fulfill({ json: { messages: Array.from({ length: 15 }, (_, index) => row(index + 185)),
      oldest_loaded_seq: 185, has_more_messages: false } });
    await expect(scroller.getByRole("button", { name: /Load earlier|Loading earlier/ })).toHaveCount(0);
    await settle();
    const readingTop = await touch.evaluate(({ viewport, emit }, notification) => {
      const top = viewport.scrollTop;
      if (notification === "after") viewport.dispatchEvent(new Event("scroll"));
      emit("touchend", 500);
      return top;
    }, notification);
    await scrollGate.evaluate((gate) => gate.release());
    await settle();
    expect(Math.abs(await scroller.evaluate((viewport) => viewport.scrollTop) - readingTop)).toBeLessThanOrEqual(2);
    await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
  } finally {
    await scrollGate.evaluate((gate) => gate.release());
    await scrollGate.dispose();
    await touch.dispose();
  }
});
}

for (const destination of ["top", "tail", "tail after measurement", "tail after a quantized correction"] as const) {
  test(`touch navigation to the ${destination} takes over a queued replacement window correction`, async ({ page }) => {
    const { scroller, gap, send, settle, pages, replaceOnReconnect } = await openChat(page);
    const row = (seq: number, content = `History ${seq}`): Message => ({ role: "assistant", type: "text", seq, content });
    replaceOnReconnect({ messages: Array.from({ length: 40 }, (_, index) => row(index + 200)),
      next_message_seq: 240, oldest_loaded_seq: 200, has_more_messages: false });
    await expect(scroller.getByText("History 239", { exact: true })).toBeInViewport();
    await scroller.hover();
    await page.mouse.wheel(0, -350);
    await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
    await settle();
    const readingSeq = await scroller.evaluate((viewport) => {
      const top = viewport.getBoundingClientRect().top;
      const rows = [...viewport.querySelectorAll<HTMLElement>("[data-message-key]")];
      return Number(rows.find((element) => element.getBoundingClientRect().top >= top)!.dataset.messageKey!.split(":")[1]);
    });
    const gate = await holdAnimationFrames(page);
    const touch = await controlledTouch(scroller);
    try {
      const quantized = destination === "tail after a quantized correction";
      const measured = destination === "tail after measurement" || quantized;
      const oldest = measured ? 200 : 190;
      // Removing a row after the reading anchor replaces the window without
      // shifting that anchor. Its positioning frame should only measure.
      replaceOnReconnect({ messages: Array.from({ length: measured ? 39 : 50 }, (_, index) => {
        const seq = index + oldest;
        return row(seq, seq === readingSeq ? "Replacement reading anchor" : undefined);
      }), next_message_seq: 240, oldest_loaded_seq: oldest, has_more_messages: true });
      await expect(scroller.getByText("Replacement reading anchor", { exact: true })).toBeVisible();
      expect(await gate.evaluate((frames) => frames.count())).toBeGreaterThan(0);
      const positionWrites = quantized ? await scroller.evaluateHandle((viewport) => {
        const bounds = viewport.getBoundingClientRect();
        for (const row of viewport.querySelectorAll<HTMLElement>("[data-message-key]")) {
          const rect = row.getBoundingClientRect();
          if (rect.bottom > bounds.top && rect.top < bounds.bottom) row.style.translate = "0 1.25px";
        }
        const native = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop")!;
        const targets: number[] = [];
        const observe = () => Object.defineProperty(viewport, "scrollTop", { configurable: true,
          get: () => native.get!.call(viewport) as number,
          set: (target: number) => {
            targets.push(target);
            const actual = native.get!.call(viewport) as number;
            native.set!.call(viewport, actual + Math.round((target - actual) / 4) * 4);
          },
        });
        return { observe, targets: () => targets, release: () => Reflect.deleteProperty(viewport, "scrollTop") };
      }) : null;
      const before = await touch.evaluate(({ viewport, emit }, destination) => {
        const before = viewport.scrollTop;
        emit("touchstart", 400);
        const y = destination === "top" ? 500 : 300;
        emit("touchmove", y);
        if (destination !== "top") emit("touchend", y);
        return before;
      }, destination);
      if (measured) {
        try {
          await positionWrites?.evaluate((writes) => { writes.observe(); });
          await gate.evaluate((frames) => { frames.flush(); frames.flush(); });
          expect(Math.abs(await scroller.evaluate((viewport) => viewport.scrollTop) - before)).toBeLessThanOrEqual(1);
          if (positionWrites) {
            expect((await positionWrites.evaluate((writes) => writes.targets())).some((target) => Math.abs(target - before) > 1)).toBe(true);
          }
        } finally {
          await positionWrites?.evaluate((writes) => { writes.release(); });
          await positionWrites?.dispose();
        }
      }
      const moved = await touch.evaluate(({ viewport, emit }, { before, destination }) => {
        viewport.scrollTop = destination === "top" ? 0 : viewport.scrollHeight;
        viewport.dispatchEvent(new Event("scroll"));
        if (destination === "top") emit("touchend", 500);
        return Math.abs(viewport.scrollTop - before);
      }, { before, destination });
      expect(moved).toBeGreaterThan(40);
      await gate.evaluate((frames) => frames.release());
      if (destination === "top") {
        await expect.poll(() => pages.length).toBe(1);
        expect(new URL(pages[0].request().url()).searchParams.get("before_seq")).toBe("190");
        await pages[0].fulfill({ json: { messages: [], oldest_loaded_seq: 190, has_more_messages: false } });
        await expect(scroller.getByText("History 190", { exact: true })).toBeInViewport();
      } else {
        send({ type: "assistant_delta", content: ("New output after late touch arrival.\n").repeat(24) + "Static Footer final marker" });
        await expect(scroller.getByTestId("streaming-text")).toContainText("Static Footer final marker");
        await expectFooterMarkerInViewport(scroller);
        await expect.poll(gap).toBeLessThanOrEqual(2);
        expect(pages).toHaveLength(0);
      }
    } finally {
      await touch.dispose();
      await gate.evaluate((frames) => frames.release());
      await gate.dispose();
    }
  });
}

for (const input of ["cancelled wheel", "stationary thumb"] as const) {
test(`a ${input} and restored layout range do not offer a jump`, async ({ page }) => {
  const { scroller, gap, settle } = await openChat(page);
  await scroller.evaluate((element) => {
    const spacer = document.createElement("div");
    spacer.dataset.testLayoutSpacer = "";
    spacer.style.height = "120px";
    const content = element.querySelector("[data-viewport-type]");
    if (!content) throw new Error("Missing Virtuoso layout viewport");
    content.appendChild(spacer);
    element.style.overflowAnchor = "none";
    element.style.scrollbarGutter = "stable";
    element.scrollTop = element.scrollHeight;
  });
  await settle();
  await scroller.evaluate((element, input) => {
    const spacer = element.querySelector<HTMLElement>("[data-test-layout-spacer]")!;
    const changeLayout = (event: Event) => {
      if (input === "cancelled wheel") event.preventDefault();
      const before = element.scrollTop;
      const height = element.scrollHeight;
      spacer.style.height = "0px";
      const clamped = element.scrollTop;
      spacer.style.height = "120px";
      const restored = element.scrollHeight;
      spacer.dataset.movement = JSON.stringify({ before, clamped, height, restored, cancelled: event.defaultPrevented });
    };
    element.addEventListener(input === "cancelled wheel" ? "wheel" : "pointerdown", changeLayout,
      { once: true, passive: false });
  }, input);
  if (input === "cancelled wheel") {
    await scroller.hover();
    await page.mouse.wheel(0, -120);
  } else {
    const thumb = await scroller.evaluate(scrollbarThumb);
    expect(thumb.gutter).toBeGreaterThan(0);
    await page.mouse.move(thumb.x, thumb.y);
    await page.mouse.down();
  }
  try {
    const spacer = scroller.locator("[data-test-layout-spacer]");
    await expect(spacer).toHaveAttribute("data-movement", /cancelled/);
    const movement = JSON.parse((await spacer.getAttribute("data-movement"))!);
    if (input === "cancelled wheel") expect(movement.cancelled).toBe(true);
    expect(movement.restored).toBe(movement.height);
    expect(movement.before - movement.clamped).toBeGreaterThan(80);
    if (input === "stationary thumb") await expect.poll(gap).toBeGreaterThan(60);
    await settle();
    await expect(page.getByRole("button", { name: "Jump to latest" })).toHaveCount(0);
  } finally {
    if (input === "stationary thumb") await page.mouse.up();
  }
  await expect(page.getByRole("button", { name: "Jump to latest" })).toHaveCount(0);
});
}

test("output during a native scrollbar drag keeps history parked on release", async ({ page }) => {
  const { scroller, gap, send, settle, output } = await openChat(page);
  const thumb = await scroller.evaluate(scrollbarThumb);
  expect(thumb.gutter).toBeGreaterThan(0);
  await page.mouse.move(thumb.x, thumb.y);
  await page.mouse.down();
  try {
    // Establish real navigation before changing the native thumb's layout;
    // Chromium can stop tracking a held thumb when its dimensions change.
    const before = await scroller.evaluate((element) => element.scrollTop);
    await page.mouse.move(thumb.x, thumb.y - 120);
    await expect.poll(() => scroller.evaluate((element) => element.scrollTop)).toBeLessThan(before - 200);
    send({ type: "assistant_delta", content: output("During thumb drag") });
    await expect(scroller.getByTestId("streaming-text")).toContainText("During thumb drag line 12");
    await settle();
  } finally {
    await page.mouse.up();
  }
  const parked = await scroller.evaluate((element) => element.scrollTop);
  send({ type: "assistant_delta", content: output("After thumb drag") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("After thumb drag line 12");
  await settle();
  expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - parked)).toBeLessThanOrEqual(2);
  expect(await gap()).toBeGreaterThan(500);
});

test("an upward wheel with no scrollable history preserves following as output grows", async ({ page }) => {
  const { scroller, gap, send, settle, output } = await openChat(page, 1);
  await expect.poll(gap).toBe(0);
  await scroller.hover();
  await page.mouse.wheel(0, -350);
  await settle();
  for (let block = 0; block < 4; block++) {
    send({ type: "assistant_delta", content: output(`Short history ${block}`) });
    await expect(scroller.getByTestId("streaming-text")).toContainText(`Short history ${block} line 12`);
    await settle();
  }
  await expect.poll(gap).toBeLessThanOrEqual(2);
});

test("scrolling inside tool output preserves main chat following", async ({ page }) => {
  const { scroller, gap, send, settle, output } = await openChat(page);
  send({ type: "tool_result", seq: 41, output: Array.from({ length: 100 }, (_, index) => `Tool line ${index}`).join("\n") });
  const result = scroller.locator(".msg-tool-result");
  await result.locator(".tool-header").click();
  const detail = result.locator(".tool-detail");
  await expect(detail).toBeVisible();
  // Opening is a reading action. Explicitly return to the main list's bottom
  // before verifying that navigation consumed by its inner pane leaves it there.
  await scroller.hover({ position: { x: 5, y: 20 } });
  await page.mouse.wheel(0, 100000);
  await expect.poll(gap).toBeLessThanOrEqual(2);
  await detail.evaluate((element) => { element.scrollTop = 500; });
  const before = await detail.evaluate((element) => element.scrollTop);
  await detail.hover();
  await page.mouse.wheel(0, -150);
  await expect.poll(() => detail.evaluate((element) => element.scrollTop)).toBeLessThan(before - 100);
  await detail.focus();
  await expect(detail).toBeFocused();
  const beforeKey = await detail.evaluate((element) => element.scrollTop);
  await page.keyboard.press("PageUp");
  await expect.poll(() => detail.evaluate((element) => element.scrollTop)).toBeLessThan(beforeKey - 100);
  await expect.poll(gap).toBeLessThanOrEqual(2);
  send({ type: "assistant_delta", content: output("After inner scroll") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("After inner scroll line 12");
  await settle();
  await expect.poll(gap).toBeLessThanOrEqual(2);
});

for (const activation of ["mouse", "keyboard"] as const) {
test(`expanding a saved tool with ${activation} at the bottom preserves reading through new output`, async ({ page }) => {
  const { scroller, send, settle, output, gap } = await openChat(page);
  send({ type: "tool_result", seq: 41, output: Array.from({ length: 100 }, (_, index) => `Reading line ${index}`).join("\n") });
  const result = scroller.locator(".msg-tool-result");
  const header = result.locator(".tool-header");
  await expect(header).toBeVisible();
  await settle();
  const movement = await trackTop(header);
  if (activation === "mouse") await header.click();
  else { await header.focus(); await page.keyboard.press("Enter"); }
  await expect(result.locator(".tool-detail")).toBeVisible();
  await settle();
  expect(await movement.evaluate((tracker) => tracker.stop())).toBeLessThanOrEqual(2);
  await movement.dispose();
  const position = await scroller.evaluate((element) => element.scrollTop);
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await expect(jump).toHaveCount(0);
  send({ type: "assistant_delta", content: output("While reading tool details") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("While reading tool details line 12");
  await settle();
  expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - position)).toBeLessThanOrEqual(2);
  await expect(jump).toBeVisible();
  await scroller.hover({ position: { x: 5, y: 20 } });
  await page.mouse.wheel(0, 100000);
  await expect.poll(gap).toBeLessThanOrEqual(2);
  send({ type: "assistant_delta", content: output("Following after reading tool details") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("Following after reading tool details line 12");
  await expect.poll(gap).toBeLessThanOrEqual(2);
});
}

test("loading full reply details preserves reading while the response and new output arrive", async ({ page }) => {
  const { scroller, send, settle, output } = await openChat(page);
  let release!: () => void;
  const body = new Promise<void>((resolve) => { release = resolve; });
  const reference = "/api/sessions/follow-output/messages/41";
  await page.route(`**${reference}?view=browser`, async (route) => {
    await body;
    await route.fulfill({ json: { role: "assistant", type: "text", seq: 41,
      content: Array.from({ length: 40 }, (_, index) => `Complete reply paragraph ${index}`).join("\n\n") } });
  });
  try {
    send({ type: "assistant_text", seq: 41, content: "Preview of the saved reply", body_state: "preview", detail_ref: reference });
    const row = scroller.locator('[data-message-key="message:41"]');
    await expect(row).toContainText("Preview of the saved reply");
    await settle();
    const movement = await trackTop(row);
    await row.getByRole("button", { name: "View full message" }).click();
    await expect(row.getByRole("status")).toHaveText("Loading full message…");
    send({ type: "assistant_delta", content: output("While loading details") });
    await expect(scroller.getByTestId("streaming-text")).toContainText("While loading details line 12");
    await settle();
    release();
    await expect(row).toContainText("Complete reply paragraph 39");
    await settle();
    expect(await movement.evaluate((tracker) => tracker.stop())).toBeLessThanOrEqual(2);
    await movement.dispose();
  } finally { release(); }
});

test("only readable new text from a helper answer offers a return", async ({ page }) => {
  const { scroller, send, settle } = await openChat(page, 4);
  send({ type: "tool_result", seq: 5, output: "Tool output to read" });
  send({ type: "subagent", ...helper, seq: 6 });
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  const toggle = scroller.getByRole("button", { name: /Helper Reading/ });
  const workingAnswer = scroller.locator(".subagent-summary");
  const runtime_metadata = identity("helper", "item");
  await scroller.locator(".msg-tool-result .tool-header").click();
  await expect(scroller.getByText("Tool output to read", { exact: true })).toBeInViewport();

  send({ type: "assistant_delta", runtime_metadata, content: "Helper answer starts here.\n".repeat(35) });
  const secondItem = identity("helper", "second-item");
  send({ type: "assistant_delta", runtime_metadata: secondItem, content: "Another live helper item" });
  await expect(workingAnswer).toContainText("Helper answer starts here.");
  await expect(workingAnswer).toContainText("Another live helper item");
  await expect(workingAnswer).toBeInViewport({ ratio: 1 });
  await settle();
  await expect(jump).toHaveCount(0);

  await toggle.click();
  const movement = await trackTop(toggle);
  await settle();
  await expect(jump).toHaveCount(0);
  send({ type: "assistant_text", seq: 7, runtime_metadata: secondItem, content: "Another live helper item" });
  await expect(workingAnswer).toHaveText(/^Another live helper item.*Helper answer starts here/s);
  await settle();
  await expect(jump).toHaveCount(0);
  send({ type: "assistant_delta", runtime_metadata, content: "The new helper conclusion" });
  await expect(workingAnswer).toContainText("The new helper conclusion");
  await expect(jump).toBeVisible();
  expect(await movement.evaluate((tracker) => tracker.stop())).toBeLessThanOrEqual(2);
  await movement.dispose();
  await jump.click();
  await expect(scroller).toBeFocused();
  await expect.poll(() => workingAnswer.evaluate((body) => {
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node && !node.textContent?.includes("The new helper conclusion")) node = walker.nextNode();
    if (!node) return false;
    const range = document.createRange();
    range.setStart(node, node.textContent!.lastIndexOf("The new helper conclusion"));
    range.setEnd(node, node.textContent!.length);
    const line = range.getBoundingClientRect();
    const view = body.closest(".chat-messages")!.getBoundingClientRect();
    return line.height > 0 && line.top >= view.top && line.bottom <= view.bottom;
  })).toBe(true);
});

for (const item of ["first", "second"] as const) {
test(`a helper keeps its selected ${item} item when the first grows and the second is saved`, async ({ page }) => {
  const { scroller, send, settle } = await openChat(page, 4);
  const first = identity("helper", "first-answer");
  const second = identity("helper", "second-answer");
  send({ type: "subagent", ...helper, seq: 5 });
  await scroller.getByRole("button", { name: /Helper Reading/ }).click();
  send({ type: "assistant_delta", runtime_metadata: first, content: "The first helper text" });
  send({ type: "assistant_delta", runtime_metadata: second, content: "The second helper text" });
  const answer = scroller.locator(".subagent-summary");
  await expect(answer).toContainText("The second helper text");
  const phrase = `${item} helper`;
  await answer.evaluate((element, phrase) => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const index = node.textContent!.indexOf(phrase);
      if (index < 0) continue;
      const range = document.createRange();
      range.setStart(node, index);
      range.setEnd(node, index + phrase.length);
      const selection = window.getSelection()!;
      selection.removeAllRanges();
      selection.addRange(range);
      return;
    }
    throw new Error("Helper output was not mounted");
  }, phrase);
  const selected = () => page.evaluate(() => window.getSelection()?.toString());
  expect(await selected()).toBe(phrase);
  send({ type: "assistant_delta", runtime_metadata: first, content: " grows." });
  await expect(answer).toContainText("The first helper text grows.");
  expect(await selected()).toBe(phrase);
  send({ type: "assistant_text", seq: 6, runtime_metadata: second, content: "The second helper text" });
  await expect(answer).toHaveText(/^The second helper text.*The first helper text grows\./s);
  await settle();
  expect(await selected()).toBe(phrase);
});
}

test("an expanded Claude helper offers a return for its final summary and keeps it on view switch", async ({ page }) => {
  const { scroller, send, settle, session } = await openChat(page, 4);
  const runtime_metadata = { request_id: "request", attempt_id: "attempt", tool_call_id: "claude-tool" };
  send({ type: "tool_use", seq: 5, tool: "Task", input: { description: "Read" }, runtime_metadata });
  send({ type: "subagent", task_id: "claude-helper", tool_call_id: "claude-tool", status: "running", name: "Claude helper", description: "Reading" });
  await scroller.getByRole("button", { name: /Claude helper Reading/ }).click();
  const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
  await expect(jump).toHaveCount(0);
  send({ type: "subagent", task_id: "claude-helper", tool_call_id: "claude-tool", status: "completed",
    summary: "Claude generated an answer.\n".repeat(55) + "Final Claude line" });
  await expect(scroller.locator(".subagent-summary")).toContainText("Final Claude line");
  await expect(jump).toBeVisible();
  await page.getByRole("button", { name: /^Schedules/ }).click();
  await expect(scroller).toBeHidden();
  await page.locator(".session-item", { hasText: session.name }).click();
  await expect(scroller).toBeVisible();
  await expect(jump).toBeVisible();
  await settle();
  await jump.click();
  await expect(jump).toHaveCount(0);
});

test("saving a capped helper item preserves its selected prefix and visible passage", async ({ page }) => {
  const { scroller, send, settle } = await openChat(page, 4);
  send({ type: "subagent", ...helper, seq: 5 });
  await scroller.getByRole("button", { name: /Helper Reading/ }).click();
  const runtime_metadata = identity("helper", "capped-answer");
  const content = Array.from({ length: 225 }, (_, index) => `Helper block ${index} contains a unique marker and enough words to fill the preview.`).join("\n\n");
  send({ type: "assistant_delta", content, runtime_metadata });
  const answer = scroller.locator(".subagent-detail .subagent-summary");
  await expect(answer).toHaveText(/…$/);
  const selected = await selectLiveWords(answer, /Helper block \d+/);
  send({ type: "assistant_text", seq: 6, runtime_metadata, content });
  await expect(answer).toContainText("Helper block 224");
  await settle();
  const saved = await page.evaluate(() => {
    const selection = window.getSelection()!;
    return { text: selection.toString(), top: selection.rangeCount ? selection.getRangeAt(0).getBoundingClientRect().top : 0 };
  });
  expect(saved.text).toBe(selected.text);
  expect(Math.abs(saved.top - selected.top)).toBeLessThanOrEqual(2);
  await expect(page.getByRole("button", { name: "Jump to latest", exact: true })).toBeVisible();
});

test("collapsing a tool clamps the viewport without restoring following", async ({ page }) => {
  const { scroller, send, settle, output, gap } = await openChat(page);
  send({ type: "tool_result", seq: 41, output: Array.from({ length: 100 }, (_, index) => `Detail line ${index}`).join("\n") });
  const result = scroller.locator(".msg-tool-result");
  const header = result.locator(".tool-header");
  await expect(header).toBeVisible();
  await header.click();
  await expect(result.locator(".tool-detail")).toBeVisible();
  await settle();
  const expandedTop = await scroller.evaluate((element) => element.scrollTop);
  await scroller.hover({ position: { x: 5, y: 20 } });
  await page.mouse.wheel(0, 24);
  await expect.poll(() => scroller.evaluate((element) => element.scrollTop)).toBeGreaterThan(expandedTop + 8);
  await expect.poll(gap).toBeGreaterThan(10);
  await header.click();
  await expect(result.locator(".tool-detail")).toHaveCount(0);
  await settle();
  const collapsedTop = await scroller.evaluate((element) => element.scrollTop);
  send({ type: "assistant_delta", content: output("Output after collapse") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("Output after collapse line 12");
  await settle();
  expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - collapsedTop)).toBeLessThanOrEqual(2);
});

test("a touch tap on bottom text preserves paused reading after collapsing a tool", async ({ page }) => {
  const { scroller, send, settle, output, gap } = await openChat(page, 1);
  send({ type: "tool_result", seq: 2, output: "Short tool details" });
  const result = scroller.locator(".msg-tool-result");
  const header = result.locator(".tool-header");
  await header.click();
  await expect(result.locator(".tool-detail")).toBeVisible();
  await header.click();
  await expect(result.locator(".tool-detail")).toHaveCount(0);
  await settle();
  await expect.poll(gap).toBeLessThanOrEqual(2);
  const touch = await page.context().newCDPSession(page);
  try {
    await touch.send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 1 });
    const text = await scroller.getByText("Saved row 1", { exact: true }).boundingBox();
    const x = text!.x + text!.width / 2, y = text!.y + text!.height / 2;
    await touch.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y, id: 1 }] });
    await touch.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    const before = await scroller.evaluate((element) => element.scrollTop);
    send({ type: "assistant_delta", content: output("Output after a non-navigating tap").repeat(4) });
    await expect(scroller.getByTestId("streaming-text")).toContainText("Output after a non-navigating tap line 12");
    await settle();
    expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - before)).toBeLessThanOrEqual(2);
  } finally { await touch.detach(); }
});

test("loading an outside-window question pauses following through new output", async ({ page }) => {
  const question: PendingQuestion = { question_id: "outside", request_id: "request", attempt_id: "attempt",
    body_state: "preview", message_seq: 0, detail_ref: "/api/sessions/follow-output/messages/0", questions: [] };
  const { scroller, send, settle, output } = await openChat(page, 40, [question]);
  let release!: () => void;
  const body = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/sessions/follow-output/messages/0?view=browser", async (route) => {
    await body;
    await route.fulfill({ json: { role: "assistant", type: "question_request", seq: 0, tool_use_id: "outside",
      tool_input: { question_id: "outside", request_id: "request", attempt_id: "attempt",
        questions: [{ question: "Read the complete question", options: [{ label: "Continue" }] }] } } });
  });
  try {
    const outside = page.getByLabel("Questions outside the history window");
    await outside.getByRole("button", { name: "Load full question" }).click();
    await expect(outside.getByRole("status")).toHaveText("Loading full question…");
    await settle();
    const before = await scroller.evaluate((element) => element.scrollTop);
    send({ type: "assistant_delta", content: output("While loading outside question") });
    await expect(scroller.getByTestId("streaming-text")).toContainText("While loading outside question line 12");
    await settle();
    expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - before)).toBeLessThanOrEqual(2);
    release();
    await expect(outside).toContainText("Read the complete question");
  } finally { release(); }
});

test("resizing the current viewport retains bottom following without new output", async ({ page }) => {
  const { gap } = await openChat(page);
  const viewport = page.viewportSize()!;
  await page.setViewportSize({ ...viewport, height: viewport.height - 180 });
  await expect.poll(gap).toBeLessThanOrEqual(2);
});

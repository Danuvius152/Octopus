import { expect, test, type Page, type WebSocketRoute } from "./test";
import { enterAccountCredentials } from "./current-account";
import type { Message, PendingQuestion } from "../src/stores/sessionStore";
import type { RuntimeMetadata } from "../src/api";
import type { Locator, Route } from "@playwright/test";

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

async function openChat(page: Page, count = 40, pending: PendingQuestion[] = [], beforeSelect?: () => Promise<void>) {
  const session = { id: "follow-output", agent_id: "follow-agent", name: "Following output", backend: "codex",
    status: "running", archived: false, working_dir: "/tmp", created_at: "2026-11-12T00:00:00Z" };
  const messages: Message[] = Array.from({ length: count }, (_, index) => ({
    role: "assistant", type: "text", seq: index + 1, content: `Saved row ${index + 1}`,
  }));
  let snapshot = { messages, next_message_seq: count + 1, oldest_loaded_seq: 1, has_more_messages: false };
  const pages: Route[] = [];
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
  await page.routeWebSocket("**/ws?*", (ws) => { socket = ws; });
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
  if (!beforeSelect) {
    await expect(scroller.getByText(`Saved row ${count}`, { exact: true })).toBeVisible();
    await expect.poll(gap).toBeLessThanOrEqual(2);
  }
  const send = (event: Record<string, unknown>) => socket.send(JSON.stringify({ ...event, session_id: session.id }));
  const output = (label: string) => Array.from({ length: 12 }, (_, index) => `${label} line ${index + 1}`).join("\n") + "\n";
  const replaceOnReconnect = (next: typeof snapshot) => {
    snapshot = next;
    socket.close({ code: 1012, reason: "Read a replacement transcript window" });
  };
  return { scroller, gap, send, settle, output, session, pages, replaceOnReconnect };
}

test("a nonempty replacement window uses the current list and keeps history usable", async ({ page }) => {
  const { scroller, gap, send, settle, pages, replaceOnReconnect } = await openChat(page);
  const original = await scroller.elementHandle();
  await scroller.press("Home");
  await expect(scroller.getByText("Saved row 1", { exact: true })).toBeVisible();
  const row = (seq: number): Message => ({ role: "assistant", type: "text", seq, content: `Replacement row ${seq}` });
  replaceOnReconnect({ messages: Array.from({ length: 60 }, (_, index) => row(index + 200)),
    next_message_seq: 260, oldest_loaded_seq: 200, has_more_messages: true });
  await expect(scroller.getByText("Replacement row 259", { exact: true })).toBeVisible();
  await expect.poll(gap).toBeLessThanOrEqual(2);
  expect(await scroller.evaluate((element, old) => element === old, original)).toBe(true);
  send({ type: "assistant_text", seq: 260, content: "After the replacement" });
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
    const reading = await scroller.evaluate((element) => {
      element.dispatchEvent(new KeyboardEvent("keydown", { key: "PageDown", bubbles: true }));
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

test("chat follows output after a card, preserves history reading and resumes at the bottom", async ({ page }) => {
  const { scroller, gap, send, settle, output } = await openChat(page);
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

  await scroller.hover();
  await page.mouse.wheel(0, -350);
  await expect.poll(gap).toBeGreaterThan(200);
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

  await page.mouse.wheel(0, 10000);
  await expect.poll(gap).toBeLessThanOrEqual(2);
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
  send({ type: "assistant_delta", content: output("While reading tool details") });
  await expect(scroller.getByTestId("streaming-text")).toContainText("While reading tool details line 12");
  await settle();
  expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - position)).toBeLessThanOrEqual(2);
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

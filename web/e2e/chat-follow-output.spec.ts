import { expect, test, type Page, type WebSocketRoute } from "./test";
import { enterAccountCredentials } from "./current-account";
import type { Message, PendingQuestion } from "../src/stores/sessionStore";
import type { RuntimeMetadata } from "../src/api";
import type { Locator } from "@playwright/test";

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

async function openChat(page: Page, count = 40, pending: PendingQuestion[] = []) {
  const session = { id: "follow-output", agent_id: "follow-agent", name: "Following output", backend: "codex",
    status: "running", archived: false, working_dir: "/tmp", created_at: "2026-11-12T00:00:00Z" };
  const messages: Message[] = Array.from({ length: count }, (_, index) => ({
    role: "assistant", type: "text", seq: index + 1, content: `Saved row ${index + 1}`,
  }));
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
    else if (path.startsWith(`/api/sessions/${session.id}/native-details/`)) data = {
      anchor_seq: 41, child: helper, availability: "not_collected", messages: [], next_after_seq: null, has_more: false,
    };
    else if (path === `/api/sessions/${session.id}`) data = { ...session,
      active_attempt: { request_id: "request", attempt_id: "attempt" },
      messages, next_message_seq: count + 1, oldest_loaded_seq: 1, has_more_messages: false, pending_questions: pending, subagents: [],
      dispatch_state: { generation: "follow-service", revision: 1, paused: false, active_request_id: "request", active_operation: "message", items: [] },
    };
    await route.fulfill({ json: data });
  });
  await page.routeWebSocket("**/ws?*", (ws) => { socket = ws; });
  await page.goto("/");
  await enterAccountCredentials(page);
  await page.locator(".agent-item", { hasText: "Octo" }).click();
  await page.locator(".session-item", { hasText: session.name }).click();
  await expect(page.locator(".conn-status.on")).toBeVisible();
  const scroller = page.locator(".chat-messages");
  const gap = () => scroller.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop);
  const settle = () => scroller.evaluate(async () => {
    for (let frame = 0; frame < 24; frame++) await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
  });
  await expect(scroller.getByText(`Saved row ${count}`, { exact: true })).toBeVisible();
  await expect.poll(gap).toBeLessThanOrEqual(2);
  const send = (event: Record<string, unknown>) => socket.send(JSON.stringify({ ...event, session_id: session.id }));
  const output = (label: string) => Array.from({ length: 12 }, (_, index) => `${label} line ${index + 1}`).join("\n") + "\n";
  return { scroller, gap, send, settle, output, session };
}

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

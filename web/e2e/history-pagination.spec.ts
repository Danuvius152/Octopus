import { expect, test, type WebSocketRoute } from "./test";
import type { Route } from "@playwright/test";
import { enterAccountCredentials } from "./current-account";
import type { Message } from "../src/stores/sessionStore";
import type { RuntimeMetadata } from "../src/api";

function holdAnimationFrames() {
  const request = window.requestAnimationFrame, cancel = window.cancelAnimationFrame;
  let serial = 0;
  const pending = new Map<number, FrameRequestCallback>();
  window.requestAnimationFrame = (callback) => { const id = --serial; pending.set(id, callback); return id; };
  window.cancelAnimationFrame = (id) => { if (id < 0) pending.delete(id); else cancel(id); };
  return {
    flush: () => { for (const id of [...pending.keys()]) { const callback = pending.get(id); pending.delete(id); callback?.(performance.now()); } },
    release: () => { window.requestAnimationFrame = request; window.cancelAnimationFrame = cancel;
      for (const callback of pending.values()) request(callback); pending.clear(); },
  };
}

test("native cards retain content, order and saved references across a long history boundary", async ({ page }) => {
  const session = { id: "long-native-history", agent_id: "history-agent", name: "Native history",
    backend: "codex", status: "idle", archived: false, working_dir: "/tmp", created_at: "2026-11-02T00:00:00Z" };
  const run = (thread: string, name: string, status: "completed" | "running" = "completed") => ({
    task_id: thread, card_id: `card-${thread}`, native_thread_id: thread, native_turn_id: "turn",
    name, status, description: "", prompt: "", summary: "", steps: [], runtime_metadata: {
      request_id: "request", attempt_id: "attempt", native: { thread_id: thread, turn_id: "turn",
        root_thread_id: "root", root_turn_id: "turn" },
    },
  });
  const reader = run("reader", "Saved reader");
  const sibling = run("sibling", "Saved sibling");
  const detached = run("uncaptured", "Snapshot only", "running");
  const row = (seq: number, content = `History row ${seq}`): Message => ({ role: "assistant", type: "text", seq, content });
  const status = ({ runtime_metadata, ...tool_input }: ReturnType<typeof run>, seq: number): Message => ({
    role: "system", type: "native_subagent", seq, runtime_metadata, tool_input,
  });
  const messages = Array.from({ length: 200 }, (_, index) => row(200 + index));
  messages[0] = { ...row(200, "Recent reader body"), runtime_metadata: reader.runtime_metadata };
  messages[2] = status(sibling, 202);
  messages[3] = { ...row(203, "Sibling body"), runtime_metadata: sibling.runtime_metadata };
  const earlier = Array.from({ length: 200 }, (_, index) => row(index));
  earlier[198] = status(reader, 198);
  earlier[199] = { ...row(199, "Earlier reader body"), runtime_metadata: reader.runtime_metadata };
  const pages: Route[] = [];
  const detailReads: number[] = [];
  const prefix = `/api/sessions/${session.id}`;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith("/api/")) { await route.continue(); return; }
    let data: unknown = [];
    if (path === "/api/auth/state") data = { accounts_exist: true };
    else if (path === "/api/auth/login") data = { token: "history-token" };
    else if (path === "/api/auth/identity") data = { user_id: "history-user", label: "Reader", is_admin: false };
    else if (path === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend: "codex", pinned: true, is_system: true }];
    else if (path === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
    else if (path === "/api/sessions") data = [session];
    else if (path === `${prefix}/messages`) {
      expect(url.searchParams.get("before_seq")).toBe("200");
      pages.push(route);
      return;
    } else if (path.startsWith(`${prefix}/native-details/`)) {
      const anchor_seq = Number(path.split("/").at(-1));
      detailReads.push(anchor_seq);
      data = { anchor_seq, child: reader, availability: "available", messages: [], next_after_seq: null, has_more: false };
    } else if (path === prefix) data = { ...session, messages, next_message_seq: 400, oldest_loaded_seq: 200,
      has_more_messages: true, pending_questions: [], subagents: [detached],
      dispatch_state: { generation: "native-history-service", revision: 1, paused: false,
        active_request_id: null, active_operation: null, items: [] } };
    await route.fulfill({ json: data });
  });
  await page.routeWebSocket("**/ws?*", () => {});
  await page.goto("/");
  await enterAccountCredentials(page);
  await page.locator(".agent-item", { hasText: "Octo" }).click();
  await page.locator(".session-item", { hasText: session.name }).click();
  const scroller = page.locator(".chat-messages");
  const snapshot = scroller.locator(".subagent-card", { hasText: detached.name });
  await snapshot.getByRole("button").click();
  await expect(snapshot.getByText(/No saved reference/)).toBeVisible();
  expect(detailReads).toEqual([]);

  await scroller.press("Home");
  await expect.poll(() => pages.length).toBe(1);
  const names = scroller.locator(".subagent-name");
  const summaries = scroller.locator(".subagent-summary");
  await expect(names).toHaveText(["agent reader", sibling.name]);
  await expect(summaries).toHaveText(["Recent reader body", "Sibling body"]);
  const observedCard = scroller.getByRole("button", { name: /agent reader/ });
  await observedCard.click();
  await expect.poll(() => [...new Set(detailReads)]).toEqual([200]);
  await observedCard.click();

  await pages[0].fulfill({ json: { messages: earlier, oldest_loaded_seq: 0, has_more_messages: false } });
  await expect(scroller.getByText("Loading earlier messages…")).toHaveCount(0);
  await expect(names).toHaveText([reader.name, sibling.name]);
  await expect(summaries).toHaveText(["Earlier reader body Recent reader body", "Sibling body"]);
  const savedCard = scroller.getByRole("button", { name: /Saved reader/ });
  await savedCard.click();
  await expect.poll(() => [...new Set(detailReads)]).toEqual([200, 198]);
  expect(pages).toHaveLength(1);
});

for (const scenario of ["mixed", "child-only", "interrupted", "deep-mixed", "deep-child-only", "live-footer", "jump-latest"] as const) {
const childOnly = scenario === "child-only" || scenario === "deep-child-only";
const deep = scenario.startsWith("deep-");
test(scenario === "interrupted" ? "navigation after history merge retires queued positioning" :
  scenario === "live-footer" ? "history prepend keeps the live paragraph reached while waiting for the older page" :
  scenario === "jump-latest" ? "jump to latest keeps following when a pending older page arrives" :
  `history prepend keeps the visible ${deep ? "paragraph" : "row"} and offset across ${scenario} regrouping and live tail appends`, async ({ page }) => {
  const session = { id: "history-window", agent_id: "history-agent", name: "Reading history", backend: "codex",
    status: "idle", archived: false, working_dir: "/tmp", created_at: "2026-11-02T00:00:00Z" };
  const identity = (thread: string): RuntimeMetadata => ({ request_id: "request", attempt_id: "attempt", native: {
    thread_id: thread, turn_id: "turn", item_id: "item", root_thread_id: "root", root_turn_id: "turn",
  } });
  const row = (seq: number): Message => ({ role: "assistant", type: "text", seq, content: `History row ${seq}` });
  const messages = Array.from({ length: 61 }, (_, index) => row(index + 200));
  if (deep) messages[3] = { ...row(203), content: Array.from({ length: 60 }, (_, index) =>
    `### Paragraph ${index + 1}\n\nA saved explanation remains readable while new output arrives. ` +
    "这是一段需要连续阅读的历史正文，旧页合并后仍应保持同一段落。\n\n").join("") };
  messages[1] = { ...row(201), runtime_metadata: identity("helper"), content: "Saved child body" };
  const helper = { card_id: "helper-card", task_id: "helper", native_thread_id: "helper", native_turn_id: "turn",
    runtime_metadata: identity("helper"), status: "running", name: "Helper", description: "Reading", prompt: "", summary: "", steps: [] };
  const pages: Route[] = [];
  let socket!: WebSocketRoute;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith("/api/")) { await route.continue(); return; }
    let data: unknown = [];
    if (path === "/api/auth/state") data = { accounts_exist: true };
    else if (path === "/api/auth/login") data = { token: "history-token" };
    else if (path === "/api/auth/identity") data = { user_id: "history-user", label: "Reader", is_admin: false };
    else if (path === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend: "codex", pinned: true, is_system: true }];
    else if (path === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
    else if (path === "/api/sessions") data = [session];
    else if (path === `/api/sessions/${session.id}/messages`) {
      pages.push(route);
      expect(url.searchParams.get("before_seq")).toBe("200");
      return;
    } else if (path === `/api/sessions/${session.id}`) data = { ...session,
      messages, next_message_seq: 261, oldest_loaded_seq: 200, has_more_messages: true, pending_questions: [], subagents: [helper],
      dispatch_state: { generation: "history-service", revision: 1, paused: false, active_request_id: null, active_operation: null, items: [] },
    };
    await route.fulfill({ json: data });
  });
  await page.routeWebSocket("**/ws?*", (ws) => { socket = ws; });
  if (scenario === "jump-latest") await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");
  await enterAccountCredentials(page);
  await page.locator(".agent-item", { hasText: "Octo" }).click();
  await page.locator(".session-item", { hasText: session.name }).click();
  const scroller = page.locator(".chat-messages");
  await expect(scroller.getByText("History row 260", { exact: true })).toBeVisible();
  if (deep) await page.evaluate(() => document.fonts.ready);
  // Reach history through native navigation, which carries reading intent;
  // layout-driven position changes deliberately do not retire following.
  await scroller.press("Home");
  await expect.poll(() => pages.length).toBe(1);
  if (scenario === "jump-latest") {
    const jump = page.getByRole("button", { name: "Jump to latest" });
    const gap = () => scroller.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop);
    await expect(jump).toBeVisible();
    const gate = await page.evaluateHandle(holdAnimationFrames);
    try {
      await jump.press("Enter");
      await expect(jump).toHaveCount(0);
      expect(await gap()).toBeGreaterThan(1000);

      const before = await scroller.evaluate((element) => element.scrollHeight);
      await pages[0].fulfill({ json: { messages: Array.from({ length: 40 }, (_, index) => row(160 + index)),
        oldest_loaded_seq: 160, has_more_messages: true } });
      await expect(scroller.getByRole("button", { name: "Load earlier messages" })).toBeAttached();
      await expect.poll(() => scroller.evaluate((element) => element.scrollHeight)).toBeGreaterThan(before + 100);
      expect(await gap()).toBeGreaterThan(1000);
      const remaining = await gate.evaluate((queued) => {
        queued.flush();
        const element = document.querySelector(".chat-messages")!;
        return element.scrollHeight - element.clientHeight - element.scrollTop;
      });
      expect(remaining).toBeLessThanOrEqual(2);
    } finally {
      await gate.evaluate((queued) => queued.release());
      await gate.dispose();
    }

    socket.send(JSON.stringify({ type: "status", session_id: session.id, status: "running" }));
    socket.send(JSON.stringify({ type: "assistant_delta", session_id: session.id, content: "Live while returning to latest" }));
    await expect(scroller.getByTestId("streaming-text")).toContainText("Live while returning to latest");
    await expect.poll(gap).toBeLessThanOrEqual(2);
    await expect(scroller.getByTestId("streaming-text")).toBeInViewport();
    await expect(pages).toHaveLength(1);
    return;
  }
  if (scenario === "live-footer") {
    socket.send(JSON.stringify({ type: "status", session_id: session.id, status: "running" }));
    socket.send(JSON.stringify({ type: "assistant_delta", session_id: session.id,
      content: Array.from({ length: 160 }, (_, index) => `Live paragraph ${index + 1}.\n\n`).join("") }));
    await expect(scroller.getByTestId("streaming-text")).toContainText("Live paragraph 160.");
    await scroller.hover();
    const gap = () => scroller.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop);
    await page.mouse.wheel(0, 100000);
    await expect.poll(gap).toBeLessThanOrEqual(2);
    await page.mouse.wheel(0, -1000);
    await expect.poll(gap).toBeGreaterThan(950);
    const track = await scroller.evaluateHandle((viewport) => {
      function passage(marker: string) {
        const text = viewport.querySelector(".msg-streaming-text")?.firstChild;
        if (!(text instanceof Text)) return undefined;
        const index = text.data.indexOf(marker);
        if (index === -1) return undefined;
        const range = document.createRange();
        range.setStart(text, index); range.setEnd(text, index + marker.length);
        return range.getBoundingClientRect();
      }
      const view = viewport.getBoundingClientRect();
      const anchor = Array.from({ length: 160 }, (_, index) => `Live paragraph ${index + 1}.`).find((marker) => {
        const box = passage(marker);
        return !!box && box.top > view.top + 30 && box.bottom < view.bottom;
      });
      if (!anchor) throw new Error("A live passage did not reach the reading viewport");
      const offset = passage(anchor)!.top - view.top;
      const savedRowInView = Array.from(viewport.querySelectorAll("[data-message-key]")).some((row) => {
        const box = row.getBoundingClientRect();
        return box.top < view.bottom && box.bottom > view.top;
      });
      const samples: (number | null)[] = [];
      let frame = 0;
      const sample = () => {
        const box = passage(anchor);
        const view = viewport.getBoundingClientRect();
        samples.push(box && box.top >= view.top && box.bottom <= view.bottom ? box.top - view.top - offset : null);
        frame = requestAnimationFrame(sample);
      };
      sample();
      return { savedRowInView, stop: () => { cancelAnimationFrame(frame); return samples; } };
    });
    try {
      expect(await track.evaluate((reading) => reading.savedRowInView)).toBe(false);
      const before = await scroller.evaluate((element) => element.scrollHeight);
      await pages[0].fulfill({ json: { messages: Array.from({ length: 40 }, (_, index) => row(160 + index)),
        oldest_loaded_seq: 160, has_more_messages: false } });
      await expect(scroller.getByText("Loading earlier messages…")).toHaveCount(0);
      await expect.poll(() => scroller.evaluate((element) => element.scrollHeight)).toBeGreaterThan(before + 100);
      await scroller.evaluate(async () => {
        for (let frame = 0; frame < 12; frame++) await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      });
      const offsets = await track.evaluate((reading) => reading.stop());
      expect(offsets.filter((offset) => offset === null || Math.abs(offset) > 2)).toEqual([]);
    } finally {
      await track.evaluate((reading) => reading.stop());
      await track.dispose();
    }
    expect(pages).toHaveLength(1);
    return;
  }
  const anchor = deep ? scroller.getByRole("heading", { name: "Paragraph 50", exact: true }) :
    scroller.getByText("History row 200", { exact: true });
  if (deep) {
    await expect(anchor).toBeAttached();
    await scroller.press("PageDown");
    await scroller.evaluate(async () => {
      for (let frame = 0; frame < 18; frame++) await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    });
    await anchor.evaluate((element) => element.scrollIntoView({ block: "center" }));
    await expect.poll(async () => {
      const paragraph = await anchor.boundingBox(), viewport = await scroller.boundingBox();
      return !!paragraph && !!viewport && paragraph.y >= viewport.y && paragraph.y + paragraph.height <= viewport.y + viewport.height;
    }).toBe(true);
    expect(await scroller.locator('[data-message-key="message:203"]').evaluate((element) =>
      element.getBoundingClientRect().top - element.closest(".chat-messages")!.getBoundingClientRect().top)).toBeLessThan(-2000);
  }
  await expect(anchor).toBeVisible();
  const original = deep ? await scroller.locator('[data-message-key="message:203"]').elementHandle() : null;
  const offset = async () => (await anchor.boundingBox())!.y - (await scroller.boundingBox())!.y;
  const before = await offset();
  for (const seq of [261, 262]) socket.send(JSON.stringify({ ...row(seq), type: "assistant_text", session_id: session.id }));
  // Hold browser scheduling, not product helpers. This exposes a reader's
  // navigation between a merge's first position and its queued correction.
  const gate = scenario === "interrupted" ? await page.evaluateHandle(holdAnimationFrames) : null;
  try {
  const oldest = deep ? 120 : 196;
  await pages[0].fulfill({ json: {
    messages: childOnly ? Array.from({ length: 200 - oldest }, (_, index) => ({ ...row(index + oldest), runtime_metadata: identity("helper") })) :
    [{ role: "system", type: "native_subagent", seq: oldest,
      runtime_metadata: identity("helper"), tool_input: { ...helper, status: "completed" } },
    ...Array.from({ length: 199 - oldest }, (_, index) => row(index + oldest + 1))],
    oldest_loaded_seq: oldest, has_more_messages: false,
  } });
  await expect(scroller.getByText("Loading earlier messages…")).toHaveCount(0);
  if (gate) {
    await expect.poll(async () => {
      const top = await scroller.evaluate((element) => element.scrollTop);
      if (top > 100) return top;
      await gate.evaluate((queued) => queued.flush());
      return scroller.evaluate((element) => element.scrollTop);
    }).toBeGreaterThan(100);
    // Controlled wheel signal and synchronous displacement avoid mistaking
    // Chromium's own animated key motion for a stale application write.
    const reading = await scroller.evaluate((element) => {
      element.dispatchEvent(new WheelEvent("wheel", { deltaY: -80, bubbles: true }));
      element.scrollTop -= 80;
      return element.scrollTop;
    });
    for (let step = 0; step < 4; step++) await gate.evaluate((queued) => queued.flush());
    expect(Math.abs(await scroller.evaluate((element) => element.scrollTop) - reading)).toBeLessThanOrEqual(2);
  } else {
  await expect(anchor).toBeVisible();
  await expect.poll(async () => Math.abs(await offset() - before)).toBeLessThanOrEqual(2);
  if (deep) {
    if (!childOnly) expect(await original!.evaluate((element) => element.isConnected), "large prepend must exercise a remounted anchor").toBe(false);
    const drift = await anchor.evaluate(async (element, initial) => {
      let maximum = 0;
      for (let frame = 0; frame < 30; frame++) {
        await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
        maximum = Math.max(maximum, Math.abs(element.getBoundingClientRect().top - initial));
      }
      return maximum;
    }, (await scroller.boundingBox())!.y + before);
    expect(drift, "the calibrated paragraph remains stable after the merge").toBeLessThanOrEqual(2);
    await scroller.press("Home");
  }
  }
  await expect(scroller.locator(".subagent-card")).toHaveCount(1);
  expect(pages).toHaveLength(1);
  } finally { if (gate) { await gate.evaluate((queued) => queued.release()); await gate.dispose(); } }
});
}

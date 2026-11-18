import { accountToken, enterAccountCredentials } from "./current-account";
import { capturePaint } from "./paint-capture";
import { expect, test } from "./test";

const API = "http://localhost:8765/api";

for (const [scenario, title, initialHeight] of [
  ["handoff", "archive waits for the successor's readable layout before changing the foreground"],
  ["reopened", "a late archive keeps the reading position of the reopened source"],
  ...[360, 280].map((height) => ["short", `archive keeps the reading position at ${height}px viewport height`, height] as const),
] as const) {
test(title, async ({ page, request }, info) => {
  const reopened = scenario === "reopened";
  const headers = { Authorization: `Bearer ${accountToken()}` };
  const imported = await request.post(`${API}/sessions/import`, { headers, data: {
    name: "Archive layout source", messages: [
      { role: "user", type: "text", content: "Review the source" },
      { role: "assistant", type: "text", content: "Earlier text in the source.\n\n".repeat(80) + "Keep reading the source" },
    ],
  } });
  expect(imported.ok(), await imported.text()).toBeTruthy();
  const source: { id: string; name: string } = await imported.json();
  const otherName = "Read another conversation";
  if (reopened) {
    const other = await request.post(`${API}/sessions/import`, { headers, data: {
      name: otherName, messages: [{ role: "user", type: "text", content: otherName }],
    } });
    expect(other.ok(), await other.text()).toBeTruthy();
  }
  const original = page.locator(".chat-messages").getByText("Keep reading the source", { exact: true });
  const tail = "Successor final paragraph";
  let writes = 0, successorId = "", detailReads = 0, navigations = 0;
  const sends: string[] = [];
  page.on("websocket", (socket) => socket.on("framesent", ({ payload }) => {
    const frame = JSON.parse(String(payload));
    if (frame.type === "send_message" && frame.content === "Continue with the successor") sends.push(frame.session_id);
  }));
  let release!: () => void;
  const detailGate = new Promise<void>((resolve) => { release = resolve; });
  let releaseWrite!: () => void;
  const writeGate = new Promise<void>((resolve) => { releaseWrite = resolve; });
  if (!reopened) releaseWrite();
  await page.route((url) => url.pathname === `/api/sessions/${source.id}`, async (route) => {
    const response = await route.fetch();
    const detail = await response.json();
    await route.fulfill({ response, json: { ...detail, dispatch_state: { ...detail.dispatch_state, paused: true } } });
  });
  await page.route((url) => url.pathname === `/api/sessions/${source.id}/archive`, async (route) => {
    writes++;
    await writeGate;
    const response = await route.fetch();
    expect(response.status()).toBe(201);
    successorId = (await response.json()).id;
    await route.fulfill({ response });
  });
  await page.route((url) => Boolean(successorId) && url.pathname === `/api/sessions/${successorId}`, async (route) => {
    detailReads++;
    await detailGate;
    if (scenario === "short") return route.abort();
    const response = await route.fetch();
    const detail = await response.json();
    await route.fulfill({ response, json: { ...detail, name: "Prepared successor", message_count: 3,
      messages: [{ role: "user", type: "text", seq: 1, content: "Successor first message" }, {
        role: "assistant", type: "text", seq: 2, content: "A long paragraph in the successor.\n\n".repeat(80),
      }, { role: "user", type: "text", seq: 3, content: tail }],
      oldest_loaded_seq: 1, next_message_seq: 4, has_more_messages: false,
      pending_questions: [{ question_id: "outside", request_id: "current-request", attempt_id: "current-attempt",
        body_state: "preview", message_seq: 0, detail_ref: `/api/sessions/${successorId}/messages/0`, questions: [] }],
    } });
  });
  try {
    await page.goto("/");
    await enterAccountCredentials(page);
    // Keep the normal sidebar entry while testing the compact chat column.
    await page.addStyleTag({ content: ".chat-view { max-width: 390px; }" });
    if (initialHeight) await page.setViewportSize({ width: page.viewportSize()!.width, height: initialHeight });
    const agent = page.locator(".agent-item", { hasText: "Octo" });
    if (!(await agent.locator(".agent-fold.rotate-90").count())) await agent.click();
    await page.locator(".session-item", { hasText: source.name }).click();
    await expect(original).toBeInViewport();
    await expect(page.locator(".conn-status.on")).toBeVisible();
    await expect(page.getByText("Paused · 0 pending")).toBeVisible();
    page.on("framenavigated", (frame) => { if (frame === page.mainFrame()) navigations++; });
    const editor = page.locator(".chat-input-bar textarea");
    const submit = async () => {
      await editor.fill("/archive ");
      await page.keyboard.press("Enter");
      await expect.poll(() => writes).toBe(1);
    };
    const model = page.getByRole("button", { name: "Model, reasoning and speed settings" });
    if (reopened) {
      await submit();
      await page.locator(".session-item", { hasText: otherName }).click();
      await expect(page.locator(".chat-messages").getByText(otherName, { exact: true })).toBeInViewport();
      await page.locator(".session-item", { hasText: source.name }).click();
      await expect(original).toBeInViewport();
      await expect(page.getByText("Paused · 0 pending")).toBeVisible();
    }
    const sourceViewport = page.locator(`[data-chat-session="${source.id}"] .chat-messages`);
    const height = await sourceViewport.evaluate((element) => element.clientHeight);
    const bottomGap = () => sourceViewport.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop);
    await expect.poll(bottomGap).toBeLessThanOrEqual(1);
    await sourceViewport.hover();
    await page.mouse.wheel(0, -12);
    await expect.poll(bottomGap).toBeGreaterThan(1);
    expect(await bottomGap()).toBeLessThan(20);
    const sourceTop = await sourceViewport.evaluate((element) => element.scrollTop);
    const originalOffset = (await original.boundingBox())!.y - (await sourceViewport.boundingBox())!.y;
    if (reopened) {
      await model.focus();
      const receipt = page.waitForResponse((response) => response.request().method() === "POST" &&
        new URL(response.url()).pathname === `/api/sessions/${source.id}/archive`);
      releaseWrite();
      const response = await receipt;
      expect(response.status()).toBe(201);
      await response.finished();
    } else {
      await submit();
      await expect.poll(() => detailReads).toBeGreaterThan(0);
    }
    await expect(original).toBeInViewport();
    await expect(page.locator(".chat-archived-banner")).toBeVisible();
    await expect(page.getByRole("textbox")).toHaveCount(0);
    const archivedHeight = await sourceViewport.evaluate((element) => element.clientHeight);
    const archivedTop = await sourceViewport.evaluate((element) => element.scrollTop);
    const archivedOffset = (await original.boundingBox())!.y - (await sourceViewport.boundingBox())!.y;
    await info.attach("source-reading.json", { contentType: "application/json", body: JSON.stringify({
      before: { height, top: sourceTop, offset: originalOffset },
      archived: { height: archivedHeight, top: archivedTop, offset: archivedOffset },
    }) });
    expect(archivedHeight).toBe(height);
    expect(archivedTop).toBe(sourceTop);
    expect(Math.abs(archivedOffset - originalOffset)).toBeLessThanOrEqual(1);
    const openSuccessor = page.getByRole("button", { name: "Open successor" });
    if (reopened) {
      await expect(openSuccessor).toBeVisible();
      await expect(model).toBeFocused();
      expect(await page.locator("[data-chat-session]:not([inert])").getAttribute("data-chat-session")).toBe(source.id);
      expect(writes).toBe(1);
      expect(detailReads).toBe(0);
      expect(navigations).toBe(0);
      return;
    }
    await expect(sourceViewport).toBeFocused();
    if (scenario === "short") {
      await expect(openSuccessor).toBeInViewport();
      expect(writes).toBe(1);
      expect(detailReads).toBe(1);
      expect(navigations).toBe(0);
      return;
    }
    const standardViewport = page.viewportSize()!;
    await page.setViewportSize({ width: standardViewport.width, height: 240 });
    try {
      await expect.poll(() => page.locator(".app-layout").evaluate((element) => element.clientHeight)).toBe(240);
      const shortView = await sourceViewport.evaluate((element) => ({
        height: element.clientHeight, scrollable: element.scrollHeight > element.clientHeight,
      }));
      expect(shortView.height).toBeGreaterThanOrEqual(48);
      expect(shortView.scrollable).toBe(true);
      await openSuccessor.scrollIntoViewIfNeeded();
      const box = (await openSuccessor.boundingBox())!;
      expect(box.y).toBeGreaterThanOrEqual(0);
      expect(box.y + box.height).toBeLessThanOrEqual(240);
      await info.attach("short-archive-viewport.json", { contentType: "application/json", body: JSON.stringify(shortView) });
    } finally { await page.setViewportSize(standardViewport); }
    await expect.poll(() => sourceViewport.evaluate((element) => element.clientHeight)).toBe(height);
    const layoutGate = await page.addStyleTag({ content:
      `[data-chat-session="${successorId}"][inert] .chat-messages { height: 0 !important; flex: none !important; }` });

    const tracker = await page.evaluateHandle(({ marker, successorId }) => {
      let measured: Element | undefined;
      let preparedHeight: number | undefined;
      let preparedTailHeight: number | undefined;
      let first: { sameNode: boolean; sameHeight: boolean; sameTailHeight: boolean; tailInView: boolean;
        titleInView: boolean; ownedComposer: boolean; height: number } | undefined;
      const transitions: string[] = [];
      const sample = () => {
        const visible = document.querySelector<HTMLElement>("[data-chat-session]:not([inert])");
        const id = visible?.dataset.chatSession;
        if (id && id !== transitions.at(-1)) transitions.push(id);
        const rendered = Array.from(document.querySelectorAll(".chat-messages")).find((node) => node.textContent?.includes(marker));
        const content = Array.from(rendered?.querySelectorAll(".msg-content") ?? []).find((node) => node.textContent === marker);
        const row = content?.closest("[data-message-key]");
        const rowHeight = row?.getBoundingClientRect().height;
        if (rendered?.closest("[data-chat-session][inert]") && rendered.clientHeight) {
          measured = rendered;
          preparedHeight = rendered.clientHeight;
          preparedTailHeight = rowHeight;
        }
        if (id !== successorId || first) return;
        const heading = visible!.querySelector(".chat-header");
        const title = heading?.querySelector<HTMLElement>(".crumb-current");
        const glyphs = document.createRange();
        if (title) glyphs.selectNodeContents(title);
        const textRect = glyphs.getBoundingClientRect(), titleRect = title?.getBoundingClientRect();
        const titleInView = !!titleRect && title?.textContent === "Prepared successor" &&
          textRect.width > 0 && textRect.left >= titleRect.left && textRect.right <= titleRect.right + 1 &&
          title.contains(document.elementFromPoint(textRect.x + textRect.width / 2, textRect.y + textRect.height / 2));
        const rect = content?.getBoundingClientRect(), view = rendered?.getBoundingClientRect();
        first = { sameNode: measured === rendered && !!measured,
          sameHeight: preparedHeight !== undefined && preparedHeight === rendered?.clientHeight,
          sameTailHeight: preparedTailHeight !== undefined && preparedTailHeight === rowHeight,
          tailInView: !!rect && !!view && getComputedStyle(content!).visibility === "visible" && rect.top >= view.top && rect.bottom <= view.bottom + 1,
          titleInView, ownedComposer: Boolean(visible!.querySelector('.chat-input-bar textarea:not([readonly])')),
          height: rendered?.clientHeight ?? 0 };
      };
      const observer = new MutationObserver(sample);
      observer.observe(document.body, { attributes: true, childList: true, subtree: true });
      sample();
      return { read: () => first, transitions: () => transitions, stop: () => observer.disconnect() };
    }, { marker: tail, successorId });
    try {
      await capturePaint(page, info, async (frames) => {
        release();
        const preparing = page.locator(`[data-chat-session="${successorId}"][inert] .chat-messages`);
        await expect(preparing).toHaveCount(1);
        expect(await preparing.evaluate((element) => element.clientHeight)).toBe(0);
        await expect(original).toBeInViewport();
        const stagedSourceTop = await sourceViewport.evaluate((element) => element.scrollTop);
        expect(stagedSourceTop).toBe(sourceTop);
        await page.keyboard.press("PageUp");
        await expect.poll(() => sourceViewport.evaluate((element) => element.scrollTop)).toBeLessThan(stagedSourceTop - 150);
        await layoutGate.evaluate((element) => element.parentNode?.removeChild(element));
        await expect(page.getByText(tail, { exact: true })).toBeInViewport();
        await expect(page.getByLabel("Questions outside the history window")).toBeVisible();
        const first = await tracker.evaluate((record) => record.read());
        await info.attach("archive-handoff.json", { contentType: "application/json", body: JSON.stringify({ first, frames }) });
        expect(first).toMatchObject({
          sameNode: true, sameHeight: true, sameTailHeight: true, tailInView: true, titleInView: true, ownedComposer: true,
        });
        expect(first!.height).not.toBe(height);
        await expect(original).toHaveCount(0);
      });
      await page.locator(".chat-input-bar textarea").fill("Continue with the successor");
      await page.getByRole("button", { name: "Send message" }).click();
      await expect.poll(() => sends).toEqual([successorId]);
      expect(await tracker.evaluate((record) => record.transitions())).toEqual([source.id, successorId]);
      expect(writes).toBe(1);
      expect(detailReads).toBe(1);
      expect(navigations).toBe(0);
    } finally { await tracker.evaluate((record) => record.stop()); await tracker.dispose(); }
  } finally { releaseWrite(); release(); }
});
}

for (const destination of ["same opening", "Monitor", "reopened source"] as const) {
  test(`sidebar archive completion respects ${destination}`, async ({ page, request }) => {
    const headers = { Authorization: `Bearer ${accountToken()}` };
    const create = async (name: string, content: string): Promise<{ id: string; name: string }> => {
      const response = await request.post(`${API}/sessions/import`, { headers, data: {
        name, messages: [{ role: "user", type: "text", content }],
      } });
      expect(response.ok(), await response.text()).toBeTruthy();
      return response.json();
    };
    const source = await create("Sidebar archive source", "Sidebar original readable content");
    const other = destination === "reopened source" ? await create("Sidebar other chat", "Other readable content") : null;
    let release!: () => void, releaseReply!: () => void;
    const delayed = new Promise<void>((resolve) => { release = resolve; });
    const reply = new Promise<void>((resolve) => { releaseReply = resolve; });
    let writes = 0;
    await page.route((url) => url.pathname === `/api/sessions/${source.id}/archive`, async (route) => {
      writes++;
      await delayed;
      const committed = await route.fetch();
      expect(committed.status()).toBe(204);
      await reply;
      await route.fulfill({ response: committed });
    });
    if (destination === "Monitor") await page.route("**/api/auth/identity", async (route) => {
      // Enable the Monitor navigation in this isolated account. The archive
      // still exercises the normal account's session and request boundary.
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...await response.json(), is_admin: true } });
    });
    try {
      await page.goto("/");
      await enterAccountCredentials(page);
      const agent = page.locator(".agent-item", { hasText: "Octo" });
      if (!(await agent.locator(".agent-fold.rotate-90").count())) await agent.click();
      await expect(page.locator(".conn-status.on")).toBeVisible();
      const row = page.locator(".session-item", { hasText: source.name });
      const content = page.locator(".chat-messages .msg-content", { hasText: "Sidebar original readable content" });
      await row.click();
      await expect(content).toBeVisible();
      let navigations = 0;
      page.on("framenavigated", (frame) => { if (frame === page.mainFrame()) navigations++; });
      const archive = row.getByRole("button", { name: `Archive ${source.name}` });
      await archive.click();
      await expect(archive).toHaveAttribute("aria-busy", "true");
      await expect.poll(() => writes).toBe(1);
      if (destination === "Monitor") {
        await page.locator(".btn-manage-monitor").click();
        await expect(page.locator(".monitor-page")).toBeVisible();
      } else if (other) {
        await page.locator(".session-item", { hasText: other.name }).click();
        await expect(page.locator(".chat-header")).toContainText(other.name);
        await row.click();
        await expect(content).toBeVisible();
      }
      release();
      await expect(row).toHaveCount(0);
      if (other) await expect(page.locator(".chat-archived-banner")).toBeVisible();
      // The real broadcast precedes the held receipt. Wait for the resulting
      // inclusive confirmation before checking which view owns the screen.
      const confirmed = page.waitForResponse((response) => new URL(response.url()).searchParams.has("include_archived") &&
        new URL(response.url()).pathname === "/api/sessions" && response.ok());
      releaseReply();
      await (await confirmed).finished();
      await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      if (destination === "Monitor") await expect(page.locator(".monitor-page")).toBeVisible();
      else if (other) {
        await expect(content).toBeVisible();
        await expect(page.locator(".chat-archived-banner")).toBeVisible();
      } else await expect(page.locator(".chat-empty")).toBeVisible();
      expect(writes).toBe(1);
      expect(navigations).toBe(0);
    } finally { release(); releaseReply(); }
  });
}

for (const outcome of ["refusal", "lost-reply", "partial-failure"] as const) {
  test(`archive command gives visible ${outcome} feedback without a second write`, async ({ page, request }) => {
    const headers = { Authorization: `Bearer ${accountToken()}` };
    const response = await request.post(`${API}/sessions/import`, { headers, data: {
      name: `Archive feedback ${outcome}`,
      messages: [{ role: "user", type: "text", content: "Original archive feedback history" }],
    } });
    expect(response.ok(), await response.text()).toBeTruthy();
    const source = await response.json();
    const pattern = `**/api/sessions/${source.id}/archive`;
    let writes = 0;
    let successor: string | undefined;
    let confirmationReads = 0;
    page.on("request", (sent) => {
      const url = new URL(sent.url());
      if (writes && sent.method() === "GET" && url.pathname === "/api/sessions" && url.searchParams.has("include_archived")) confirmationReads++;
    });
    await page.route(pattern, async (route) => {
      writes++;
      if (outcome === "refusal") {
        await route.fulfill({ status: 409, json: { detail: "Archive deliberately refused" } });
      } else {
        const committed = await route.fetch();
        expect(committed.status()).toBe(201);
        successor = (await committed.json()).id;
        if (outcome === "lost-reply") await route.abort("failed");
        else {
          // Exercise the generated partial-result wire consumer after a real
          // archive effect. Backend tests inject the actual storage failure.
          await route.fulfill({ status: 500, json: { old_session_id: source.id, archived: true,
            new_session_id: successor, failure_stage: "successor_configuration",
            message: "Inherited configuration could not be saved" } });
        }
      }
    });
    try {
      await page.goto("/");
      await enterAccountCredentials(page);
      const agent = page.locator(".agent-item", { hasText: "Octo" });
      if (!(await agent.locator(".agent-fold.rotate-90").count())) await agent.click();
      await page.locator(".session-item", { hasText: source.name }).click();
      await expect(page.locator(".msg-user .msg-content")).toContainText("Original archive feedback history");
      await expect(page.locator(".conn-status.on")).toBeVisible();
      await page.locator(".chat-input-bar textarea").fill("/archive");
      await page.getByRole("button", { name: "Send message" }).click();
      if (outcome === "refusal") {
        await expect(page.getByRole("alert")).toContainText("Archive deliberately refused");
        await page.locator(".chat-input-bar textarea").fill("New draft after refusal");
        await expect(page.locator(".chat-input-bar textarea")).toHaveValue("New draft after refusal");
        expect((await (await request.get(`${API}/sessions/${source.id}`, { headers })).json()).archived).toBe(false);
      } else if (outcome === "partial-failure") {
        await expect(page.getByRole("alert")).toContainText("Archive committed");
        await expect(page.getByRole("alert")).toContainText("Inherited configuration could not be saved");
        await expect(page.getByRole("button", { name: "Open successor" })).toBeVisible();
        await expect(page.locator(".msg-user .msg-content")).toContainText("Original archive feedback history");
        const old = await request.get(`${API}/sessions/${source.id}`, { headers });
        expect((await old.json()).archived).toBe(true);
        await page.getByRole("button", { name: "Open successor" }).click();
        await expect(page.locator(".msg-user .msg-content")).toHaveCount(0);
      } else {
        await expect.poll(() => confirmationReads).toBeGreaterThan(0);
        await expect(page.locator(".chat-input-bar textarea")).toBeVisible();
        await expect(page.locator(".msg-user .msg-content")).toHaveCount(0);
        const old = await request.get(`${API}/sessions/${source.id}`, { headers });
        expect((await old.json()).archived).toBe(true);
        const live = await (await request.get(`${API}/sessions`, { headers })).json();
        expect(live.some((row: { id: string }) => row.id === successor)).toBe(true);
      }
      expect(writes).toBe(1);
    } finally {
      await page.unroute(pattern);
    }
  });
}

test("an archive header deadline keeps a manual GET-only confirmation in the real browser", async ({ page, request }) => {
  const headers = { Authorization: `Bearer ${accountToken()}` };
  const imported = await request.post(`${API}/sessions/import`, { headers, data: {
    name: "Archive header deadline", messages: [{ role: "user", type: "text", content: "Header deadline original history" }],
  } });
  expect(imported.ok(), await imported.text()).toBeTruthy();
  const source = await imported.json();
  const pattern = `**/api/sessions/${source.id}/archive`;
  let writes = 0, reads = 0;
  let release!: () => void;
  const blocked = new Promise<void>((resolve) => { release = resolve; });
  await page.route(pattern, async (route) => {
    writes++;
    await blocked;
    try { await route.abort("failed"); }
    catch (error) { if (!route.request().failure()) throw error; }
  });
  await page.route("**/api/sessions?include_archived=true", async (route) => {
    if (!writes) return route.fallback();
    reads++;
    await route.fulfill({ status: 503, json: { detail: "Confirmation unavailable" } });
  });
  try {
    await page.goto("/"); await enterAccountCredentials(page);
    const agent = page.locator(".agent-item", { hasText: "Octo" });
    if (!(await agent.locator(".agent-fold.rotate-90").count())) await agent.click();
    await page.locator(".session-item", { hasText: source.name }).click();
    await expect(page.locator(".msg-user .msg-content")).toContainText("Header deadline original history");
    await expect(page.locator(".conn-status.on")).toBeVisible();
    await page.clock.install();
    await page.locator(".chat-input-bar textarea").fill("/archive");
    await page.getByRole("button", { name: "Send message" }).click();
    await expect.poll(() => writes).toBe(1);
    // Virtual time verifies the browser's actual fetch/timer/UI handoff;
    // this does not claim a 30-second wall-clock observation.
    await page.clock.fastForward(30_000);
    await expect(page.getByRole("alert").filter({ hasText: "Archive result is still unknown" })).toBeVisible();
    const before = reads;
    await page.getByRole("button", { name: "Confirm original archive" }).click();
    await expect.poll(() => reads).toBeGreaterThan(before);
    await expect(page.getByRole("button", { name: "Confirm original archive" })).toBeEnabled();
    expect(writes).toBe(1);
    expect((await (await request.get(`${API}/sessions/${source.id}`, { headers })).json()).archived).toBe(false);
  } finally {
    release();
    await page.unroute(pattern);
  }
});

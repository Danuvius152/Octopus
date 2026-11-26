import { openAsCurrentAccount, accountToken } from "./current-account";
import { systemAgentId } from "./system-agent";
import { expect, test } from "./test";

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

test("mobile creation acknowledges one submission and shows history before auxiliary data", async ({ page, request }) => {
  const headers = { Authorization: `Bearer ${accountToken()}` };
  const endpoint = "http://localhost:8765/api/sessions";
  const agentId = await systemAgentId(request, endpoint, headers);
  let releaseCreate!: () => void;
  let releaseBackground!: () => void;
  const creation = new Promise<void>((resolve) => { releaseCreate = resolve; });
  const background = new Promise<void>((resolve) => { releaseBackground = resolve; });
  let createdId: string | null = null;
  let submissions = 0;
  let backgroundEntered = false;
  let backgroundReleased = false;
  await page.route(`**/api/agents/${agentId}/sessions`, async (route) => {
    submissions++;
    await creation;
    const response = await route.fetch();
    const row = await response.json();
    createdId = row.id;
    await route.fulfill({ response, json: row });
  });
  await page.route("**/api/sessions/*/bg-task-summaries", async (route) => {
    if (!createdId || !route.request().url().includes(`/${createdId}/`)) return route.fallback();
    backgroundEntered = true;
    await background;
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/sessions/*", async (route) => {
    if (new URL(route.request().url()).pathname !== `/api/sessions/${createdId}`) return route.fallback();
    const response = await route.fetch();
    const detail = await response.json();
    await route.fulfill({ response, json: { ...detail, message_count: 1,
      messages: [{ role: "assistant", type: "text", content: "History available while task details are pending", seq: 0 }],
      next_message_seq: 1, oldest_loaded_seq: 0, has_more_messages: false } });
  });
  try {
    await openAsCurrentAccount(page);
    await page.getByRole("button", { name: "Toggle sidebar", exact: true }).click();
    await page.locator(".agent-item", { has: page.locator(`[aria-label="New session for Octo"]`) }).click();
    await page.getByRole("button", { name: "New session for Octo", exact: true }).tap();
    await page.getByPlaceholder("Session name").fill("E2E mobile responsive creation");
    const button = page.getByRole("button", { name: "Create session", exact: true });
    const rect = await button.boundingBox();
    expect(rect).not.toBeNull();
    await button.tap();
    await expect(page.getByText("Creating session…", { exact: true })).toBeVisible();
    await expect(button).toBeDisabled();
    await page.touchscreen.tap(rect!.x + rect!.width / 2, rect!.y + rect!.height / 2);
    await expect.poll(() => submissions).toBe(1);
    releaseCreate();
    await expect(page.locator(".chat-header")).toContainText("E2E mobile responsive creation");
    await expect(page.getByText("History available while task details are pending", { exact: true })).toBeVisible();
    expect(backgroundEntered).toBe(true);
    expect(backgroundReleased).toBe(false);
    expect(submissions).toBe(1);
    expect(await page.evaluate(() => matchMedia("(hover: none)").matches)).toBe(true);
  } finally {
    releaseCreate(); releaseBackground(); backgroundReleased = true;
    await page.unrouteAll({ behavior: "wait" });
    if (createdId) await request.delete(`${endpoint}/${createdId}`, { headers });
  }
});

test("folded background cards wait for the list and keep the reading position through an exact retry", async ({ page, request }) => {
  const headers = { Authorization: `Bearer ${accountToken()}` };
  const endpoint = "http://localhost:8765/api/sessions";
  const agentId = await systemAgentId(request, endpoint, headers);
  const created = await request.post(endpoint, { headers, data: { agent_id: agentId, name: "Background summaries" } });
  expect(created.ok()).toBe(true);
  const { id: sessionId } = await created.json();
  const ids = Array.from({ length: 200 }, (_, index) => `recent-${index}`);
  const missingId = "outside-window";
  const messages = [...ids, missingId].map((id, seq) => ({
    role: "assistant", type: "tool_use", content: "", seq, tool_name: "mcp__bg__run", tool_use_id: id,
    tool_input: { command: "echo read" },
    runtime_metadata: { request_id: "summary-request", attempt_id: "summary-attempt",
      tool_call_id: id, receipt: { kind: "bg", task_id: id } },
  }));
  const summary = (id: string) => ({
    id, session_id: sessionId, command: "echo read", description: id, status: "completed",
    started_at: "2026-11-20T00:00:00Z", completed_at: "2026-11-20T00:01:00Z", exit_code: 0,
    output_preview: id === missingId ? "Recovered preview" : "", has_output: id === missingId,
    preview_truncated: id === missingId,
  });
  let finishList!: () => void;
  let finishMissing!: () => void;
  const heldList = new Promise<void>((resolve) => { finishList = resolve; });
  const heldMissing = new Promise<void>((resolve) => { finishMissing = resolve; });
  const requests: string[] = [];
  const base = `/api/sessions/${sessionId}`;
  const summaries = `${base}/bg-task-summaries`;
  const exact = `${summaries}/${missingId}`;
  await page.route(`**${base}?view=browser`, async (route) => {
    const response = await route.fetch();
    const detail = await response.json();
    await route.fulfill({ response, json: {
      ...detail, messages: [...messages, { role: "assistant", type: "text", content: "Reading after the task", seq: messages.length }],
      next_message_seq: messages.length + 1, oldest_loaded_seq: 0, has_more_messages: false,
    } });
  });
  await page.route(`**${base}/bg-task**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = [];
    requests.push(path);
    if (path === summaries) { await heldList; data = ids.map(summary); }
    else if (path === exact) {
      if (requests.filter((url) => url === exact).length === 1) {
        await heldMissing;
        await route.fulfill({ status: 503, json: { detail: "Unavailable" } });
        return;
      }
      data = summary(missingId);
    }
    await route.fulfill({ json: data });
  });
  try {
    await openAsCurrentAccount(page);
    await page.getByRole("button", { name: "Toggle sidebar", exact: true }).click();
    await page.locator(".agent-item", { has: page.locator('[aria-label="New session for Octo"]') }).click();
    await page.locator(`.session-item[data-session-id="${sessionId}"]`).click();

    const anchor = page.getByText("Reading after the task", { exact: true });
    await expect(anchor).toBeVisible();
    const card = page.locator(".octo-bgtask-chip").last();
    await expect(card).toContainText("Output preview not read");
    expect(requests).toEqual([summaries]);
    const loading = await card.boundingBox();
    const top = (await anchor.boundingBox())!.y;
    finishList();
    await expect.poll(() => requests).toEqual([summaries, exact]);
    finishMissing();
    await expect(card).toContainText("Could not read summary");
    expect((await card.boundingBox())!.height).toBe(loading!.height);
    expect(Math.abs((await anchor.boundingBox())!.y - top)).toBeLessThanOrEqual(2);
    const retry = page.getByRole("button", { name: "Retry task summary" });
    const scroller = page.locator(".chat-messages");
    const viewport = (await scroller.boundingBox())!;
    await page.mouse.move(viewport.x + viewport.width / 2, viewport.y + viewport.height / 2);
    await page.mouse.wheel(0, -4000);
    await expect(retry).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Retry background task details" })).toHaveCount(0);
    const jump = page.getByRole("button", { name: "Jump to latest", exact: true });
    await jump.click();
    await expect(jump).toHaveCount(0);
    await expect.poll(() => scroller.evaluate((node) => Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop))).toBeLessThanOrEqual(4);
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(retry).toBeVisible();
    expect(requests).toEqual([summaries, exact]);
    const retryTop = (await anchor.boundingBox())!.y;
    await retry.click();
    await expect(card).toContainText("Recovered preview");
    await expect(card).toContainText("Preview truncated");
    expect((await card.boundingBox())!.height).toBe(loading!.height);
    expect(Math.abs((await anchor.boundingBox())!.y - retryTop)).toBeLessThanOrEqual(2);
    expect(requests).toEqual([summaries, exact, exact]);
  } finally {
    finishList();
    finishMissing();
    await page.unrouteAll({ behavior: "wait" });
    await request.delete(`${endpoint}/${sessionId}`, { headers });
  }
});

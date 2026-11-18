import { accountToken, enterAccountCredentials } from "./current-account";
import { expect, test } from "./test";

const API = "http://localhost:8765/api";

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

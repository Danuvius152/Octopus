import { accountToken, enterAccountCredentials } from "./current-account";
import { expect, test } from "./test";

const API = "http://localhost:8765/api";

for (const outcome of ["refusal", "lost-reply"] as const) {
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
        await route.abort("failed");
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

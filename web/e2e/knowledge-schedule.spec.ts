import { expect, test } from "@playwright/test";

const API = "http://localhost:8765/api";
const headers = { Authorization: "Bearer changeme" };

test("schedule text retains read permission until a human chooses a new control", async ({ page, request }) => {
  const agents = await (await request.get(`${API}/agents`, { headers })).json();
  const name = `Knowledge schedule ${Date.now()}`;
  const created = await request.post(`${API}/schedules`, {
    headers, data: { agent_id: agents[0].id, name, prompt: "/wiki Explain X", interval_seconds: 3600 },
  });
  expect(created.ok()).toBeTruthy();
  const schedule = await created.json();
  try {
    const edited = await request.patch(`${API}/schedules/${schedule.id}`, {
      headers, data: { prompt: "/wiki maintain Library/Y.md", enabled: false },
    });
    expect(edited.ok()).toBeTruthy();
    expect((await edited.json()).wiki_mode).toBe("read");

    await page.goto("/");
    await page.locator('input[type="password"]').fill("changeme");
    await page.locator(".btn-login").click();
    await expect(page.locator(".agent-list-header")).toBeVisible();
    await page.locator(".btn-manage-schedules").click();
    const row = page.locator(".schedule-item", { hasText: name });
    const control = row.getByLabel(`Wiki maintenance for ${name}`);
    await expect(control).toHaveValue("read");
    const saved = page.waitForResponse((response) =>
      response.url().endsWith(`/api/schedules/${schedule.id}`) && response.request().method() === "PATCH"
    );
    await control.selectOption("auto");
    expect((await saved).ok()).toBeTruthy();
    await expect(control).toHaveValue("auto");
    await expect(row.getByRole("switch")).toHaveAttribute("aria-checked", "false");
    const rows = await (await request.get(`${API}/schedules`, { headers })).json();
    const stored = rows.find((item: { id: string }) => item.id === schedule.id);
    expect(stored.wiki_mode).toBe("auto");
    expect(stored.enabled).toBe(false);
  } finally {
    await request.delete(`${API}/schedules/${schedule.id}`, { headers });
  }
});

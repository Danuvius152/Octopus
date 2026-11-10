import { TEST_BEARER, enterAccountCredentials } from "./current-account";
import { test, expect } from "./test";

const agentsUrl = "http://localhost:8765/api/agents";
const headers = { Authorization: `Bearer ${TEST_BEARER}` };

test("a saved Agent remains saved while its delayed receipt leaves the reopened draft alone", async ({ page, request }) => {
  const created = await request.post(agentsUrl, { headers, data: { name: "E2E Form lifetime" } });
  expect(created.ok()).toBe(true);
  const agent = await created.json();
  let release!: () => void;
  const receipt = new Promise<void>((resolve) => { release = resolve; });
  let committed!: () => void;
  const saved = new Promise<void>((resolve) => { committed = resolve; });
  await page.route(`**/api/agents/${agent.id}`, async (route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    const response = await route.fetch();
    expect(response.ok()).toBe(true);
    committed();
    await receipt;
    await route.fulfill({ response });
  });
  try {
    await page.goto("/");
    await enterAccountCredentials(page);
    const row = page.locator(".agent-item", { hasText: agent.name });
    await row.dblclick();
    await page.getByLabel("One-line role").fill("Saved original edit");
    await page.getByRole("button", { name: "Save Agent", exact: true }).click();
    await saved;
    await page.locator(".btn-agent-cancel").click();
    await row.dblclick();
    await expect(page.getByRole("button", { name: "Save Agent", exact: true })).toBeEnabled();
    await page.getByLabel("One-line role").fill("Current unsent edit");
    const received = page.waitForResponse((response) => response.url().endsWith(`/api/agents/${agent.id}`)
      && response.request().method() === "PATCH");
    release();
    await received;
    await expect(page.getByLabel("One-line role")).toHaveValue("Current unsent edit");
    await expect(page.getByRole("button", { name: "Save Agent", exact: true })).toBeEnabled();
    const stored = await request.get(`${agentsUrl}/${agent.id}`, { headers });
    expect(stored.ok()).toBe(true);
    expect((await stored.json()).description).toBe("Saved original edit");
  } finally {
    release();
    await request.post(`${agentsUrl}/${agent.id}/archive`, { headers });
  }
});

test("late connector reads and failed toggles stay with their original Agent opening", async ({ page, request }) => {
  const created = await request.post(agentsUrl, { headers, data: { name: "E2E Connector draft lifetime" } });
  expect(created.ok()).toBe(true);
  const agent = await created.json();
  let releaseRead!: () => void;
  const firstRead = new Promise<void>((resolve) => { releaseRead = resolve; });
  let releaseWrite!: () => void;
  const firstWrite = new Promise<void>((resolve) => { releaseWrite = resolve; });
  let opening = 0;
  const reads = [0, 0, 0];
  let writes = 0;
  await page.route(/\/api\/connectors$/, (route) => route.fulfill({ json: ["first", "second"].map((id) => ({
    id, kind: "github", label: `${id} account`, auth_type: "oauth", scopes: [], needs_reconnect: false,
    external_account_id: id, enable_by_default: false, created_at: "2026-11-10T00:00:00Z",
  })) }));
  await page.route(`**/api/agents/${agent.id}/connectors`, async (route) => {
    // The development browser uses StrictMode and may repeat mount effects.
    // Hold reads by opening, independently of that development-only count.
    reads[opening]++;
    const initial = opening === 0;
    if (initial) await firstRead;
    await route.fulfill({ json: { installation_ids: initial ? ["first"] : [] } });
  });
  await page.route(`**/api/agents/${agent.id}/connectors/*`, async (route) => {
    if (++writes === 1) {
      await firstWrite;
      await route.fulfill({ status: 503 });
    } else await route.fulfill({ status: 204 });
  });
  try {
    await page.goto("/");
    await enterAccountCredentials(page);
    const row = page.locator(".agent-item", { hasText: agent.name });
    await row.dblclick();
    await expect.poll(() => reads[0]).toBeGreaterThan(0);
    opening = 1;
    await page.locator(".btn-agent-cancel").click();
    await row.dblclick();
    await expect.poll(() => reads[1]).toBeGreaterThan(0);
    const readReceived = page.waitForResponse((response) => response.url().endsWith(`/api/agents/${agent.id}/connectors`));
    releaseRead();
    await readReceived;
    const first = page.getByRole("checkbox", { name: /first account/ });
    const second = page.getByRole("checkbox", { name: /second account/ });
    await expect(first).not.toBeChecked();
    await first.click();
    await expect(first).toBeDisabled();
    await expect(second).toBeEnabled();
    await second.click();
    await expect(second).toBeChecked();
    opening = 2;
    await page.locator(".btn-agent-cancel").click();
    await row.dblclick();
    await expect.poll(() => reads[2]).toBeGreaterThan(0);
    const received = page.waitForResponse((response) => response.url().endsWith(`/connectors/first`));
    releaseWrite();
    await received;
    await expect(first).not.toBeChecked();
    await expect(first).toBeEnabled();
    await expect(second).not.toBeChecked();
    expect(writes).toBe(2);
  } finally {
    releaseRead();
    releaseWrite();
    await request.post(`${agentsUrl}/${agent.id}/archive`, { headers });
  }
});

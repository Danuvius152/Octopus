import { TEST_BEARER, enterAccountCredentials } from "./current-account";
import { deferred } from "../src/test-support/deferred";
import { test, expect, type Page } from "./test";
import type { components } from "../src/api/contracts";

const API = "http://localhost:8765/api";
const headers = { Authorization: `Bearer ${TEST_BEARER}` };
async function openArchive(page: Page) {
  await page.locator(".btn-account").click();
  await page.getByRole("menuitem", { name: "Archived sessions" }).click();
}

test("reload preserves an unregistered restore identity, seals its delayed POST, and permits a new explicit restore", async ({ page, request }) => {
  const created = await request.post(`${API}/sessions/import`, { headers, data: {
    name: "Browser restore identity", messages: [{ role: "user", type: "text", content: "Preserved archived history" }],
  } });
  expect(created.ok(), await created.text()).toBeTruthy();
  const session = await created.json();
  const path = `${API}/sessions/${session.id}`;
  expect((await request.post(`${path}/archive?replace=false`, { headers })).ok()).toBeTruthy();
  let identity: components["schemas"]["SessionRestoreRequest"] | undefined;
  let writes = 0;
  const release = deferred<void>();
  const pattern = `**/api/sessions/${session.id}/unarchive`;
  await page.route(pattern, async route => {
    writes++;
    identity = route.request().postDataJSON();
    await release.promise;
    await route.abort().catch(() => {});
  });
  try {
    await page.goto("/");
    await enterAccountCredentials(page);
    await expect(page.locator(".btn-account")).toBeVisible();
    await openArchive(page);
    let row = page.locator(".archived-session-row", { hasText: session.name });
    await row.getByRole("button", { name: "Unarchive", exact: true }).click();
    await expect.poll(() => writes).toBe(1);
    await expect(row.getByRole("button", { name: "Unarchive", exact: true })).toBeDisabled();
    await page.getByRole("button", { name: "Close", exact: true }).click();
    await openArchive(page);
    await expect(row.getByRole("button", { name: "Unarchive", exact: true })).toBeDisabled();
    expect(writes).toBe(1);
    const original = identity!;
    await page.reload();
    await expect(page.locator(".btn-account")).toBeVisible();
    await openArchive(page);
    row = page.locator(".archived-session-row", { hasText: session.name });
    await expect(row.getByRole("button", { name: "Unarchive", exact: true })).toBeDisabled();
    await row.getByRole("button", { name: "Confirm restoration", exact: true }).click();
    await expect(row.getByRole("button", { name: "Unarchive", exact: true })).toBeEnabled();
    const sealed = await request.get(`${path}/restoration`, { headers, params: original });
    expect(await sealed.json()).toMatchObject({ state: "sealed", operation_id: original.operation_id });
    const late = await request.post(`${path}/unarchive`, { headers, data: original });
    expect(late.status()).toBe(409);
    expect((await (await request.get(path, { headers })).json()).archived).toBe(true);
    expect(writes).toBe(1);
    release.resolve();
    await page.unroute(pattern);
    await row.getByRole("button", { name: "Unarchive", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "Archived sessions" })).toBeHidden();
    await expect(page.getByText("Preserved archived history", { exact: true })).toBeVisible();
    // Reload folds pinned agents. Navigation opens the chat without changing
    // the user's sidebar folds, so explicitly reveal the restored session.
    const agent = page.locator(".agent-item", { hasText: "Octo" });
    if (!(await agent.locator(".agent-fold.rotate-90").count())) await agent.click();
    await expect(page.locator(".session-item", { hasText: session.name })).toBeVisible();
    expect((await (await request.get(path, { headers })).json()).archived).toBe(false);
  } finally {
    release.resolve();
    await page.unroute(pattern);
    await request.delete(path, { headers });
  }
});

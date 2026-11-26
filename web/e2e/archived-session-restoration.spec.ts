import { accountToken, enterAccountCredentials } from "./current-account";
import fileChange from "../../tests/fixtures/codex-file-change-result.json" with { type: "json" };
import { deferred } from "../src/test-support/deferred";
import { test, expect, type Page } from "./test";
import type { components } from "../src/api/contracts";

const API = "http://localhost:8765/api";
const headers = { get Authorization() { return `Bearer ${accountToken()}`; } };
async function openArchive(page: Page) {
  await page.locator(".btn-account").click();
  await page.getByRole("menuitem", { name: "Archived sessions" }).click();
}

test("archived results format verified file changes and keep unrelated imported output", async ({ page, request }) => {
  const shortItem = fileChange.item;
  const longItem = { ...shortItem, id: "long-patch", changes: shortItem.changes.map(change => ({ ...change,
    diff: `${"+added line\n".repeat(3000)}+end of saved diff`,
  })) };
  const messages = [shortItem, longItem].map(item => ({
    role: "tool", type: "tool_result", tool_name: "fileChange", tool_use_id: item.id,
    content: JSON.stringify({ ...fileChange, item }), tool_input: { native_item: item },
    runtime_metadata: { request_id: "request", attempt_id: "attempt", native: {
      thread_id: fileChange.threadId, root_thread_id: fileChange.threadId,
      turn_id: fileChange.turnId, root_turn_id: fileChange.turnId, item_id: item.id,
    } },
  }));
  const ordinary = {
    ...messages[0], tool_name: undefined, native_item_type: "fileChange",
    tool_input: { native_item: { type: "fileChange" }, native_item_type: "fileChange", metadata: "x".repeat(18 * 1024) },
  };
  const created = await request.post(`${API}/sessions/import`, { headers, data: {
    name: "Archived file changes", messages: [...messages, ordinary],
  } });
  expect(created.ok(), await created.text()).toBeTruthy();
  const session = await created.json();
  const path = `${API}/sessions/${session.id}`;
  expect((await request.post(`${path}/archive?replace=false`, { headers })).ok()).toBeTruthy();
  const detailPath = new URL(`${path}/messages/${session.messages[1].seq}`).pathname;
  const detailRequests: string[] = [];
  page.on("request", sent => {
    const url = new URL(sent.url());
    if (url.pathname === detailPath) detailRequests.push(url.search);
  });
  await page.goto("/");
  await enterAccountCredentials(page);
  await openArchive(page);
  const sessionPath = new URL(path).pathname;
  const snapshot = page.waitForResponse(response => {
    const url = new URL(response.url());
    return url.pathname === sessionPath && url.searchParams.get("view") === "browser";
  });
  await page.locator(".archived-session-row", { hasText: session.name }).getByRole("button", { name: "View", exact: true }).click();
  await expect(page.locator(".chat-archived-banner")).toBeVisible();
  const browserMessages = (await (await snapshot).json()).messages;
  expect(browserMessages[2]).toHaveProperty("body_state", "preview");
  expect(browserMessages[2]).not.toHaveProperty("native_item_type");
  const results = page.locator(".msg-tool-result");
  const short = results.nth(0);
  const long = results.nth(1);
  await expect(short.locator(".tool-preview")).toContainText('"path": "a.py", "diff": "+native patch"');
  await short.locator(".tool-header").click();
  const shortDetail = short.locator(".tool-detail");
  await expect(shortDetail).toContainText('"move_path": "moved.py"');
  await expect(shortDetail).not.toContainText("threadId");
  await expect(long.locator(".tool-preview")).toHaveText(messages[1].content.slice(0, 120));
  expect(detailRequests).toEqual([]);
  await long.locator(".tool-header").click();
  const longDetail = long.locator(".tool-detail");
  await expect(longDetail).toContainText("+end of saved diff");
  await expect(longDetail).not.toContainText("threadId");
  await long.locator(".tool-header").click();
  await expect(long.locator(".tool-preview")).toContainText('"path": "a.py", "diff": "+added line');
  expect(detailRequests).toEqual(["?view=browser"]);

  const unverified = results.nth(2);
  await expect(unverified.locator(".tool-preview")).toHaveText(ordinary.content.slice(0, 120));
  const full = page.waitForResponse(r => new URL(r.url()).pathname === `${sessionPath}/messages/${session.messages[2].seq}`);
  await unverified.locator(".tool-header").click();
  expect((await full).ok()).toBeTruthy();
  await expect(unverified.locator(".tool-detail")).toHaveText(ordinary.content);
  await unverified.locator(".tool-header").click();
  await expect(unverified.locator(".tool-preview")).toHaveText(ordinary.content.slice(0, 120));
});

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

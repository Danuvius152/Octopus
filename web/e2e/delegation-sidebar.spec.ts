import { ACCOUNT_API } from "./account-resources";
import { accountToken, enterAccountCredentials } from "./current-account";
import { systemAgentId } from "./system-agent";
import { expect, test } from "./test";

test("new delegations appear in the open sidebar without reloading sessions", async ({ page, request }) => {
  const headers = { Authorization: `Bearer ${accountToken()}` };
  const sessions = `${ACCOUNT_API}/sessions`;
  const agentId = await systemAgentId(request, sessions, headers);
  const createSession = async (name: string) => {
    const response = await request.post(sessions, { headers, data: { agent_id: agentId, name } });
    expect(response.status()).toBe(201);
    return await response.json() as { id: string };
  };
  const parent = await createSession("Delegation caller");
  const foreground = await createSession("Unrelated conversation");
  const worker = "Sidebar delegate";
  const workerResponse = await request.post(`${ACCOUNT_API}/agents`, { headers, data: { name: worker } });
  expect(workerResponse.status()).toBe(201);
  const longNamePrefix = "Long-named delegate ";
  const longName = longNamePrefix + "长".repeat(90_000);
  const longWorker = await request.post(`${ACCOUNT_API}/agents`, { headers, data: { name: longName } });
  expect(longWorker.status()).toBe(201);

  await page.goto("/");
  await enterAccountCredentials(page);
  await expect(page.locator(".conn-status.on")).toBeVisible();
  await page.locator(".agent-item", { hasText: "Octo" }).click();
  const activeRow = page.locator(`.session-item[data-session-id="${foreground.id}"]`);
  const isListRead = (url: string, method: string) => method === "GET" && new URL(url).pathname === "/api/sessions";
  // Establish the server generation while leaving the caller's result cards unmounted.
  await Promise.all([
    page.waitForResponse((response) => isListRead(response.url(), response.request().method())),
    activeRow.click(),
  ]);
  await expect(page.locator(".chat-header .crumb-current")).toHaveText("Unrelated conversation");
  const agentRow = page.locator(".agent-item", { hasText: worker });
  await agentRow.click();
  const hiddenPill = agentRow.locator("..").getByTitle("Show delegation sessions");
  await expect(hiddenPill).toHaveCount(0);

  const sessionReads: string[] = [];
  page.on("request", (outbound) => {
    const path = new URL(outbound.url()).pathname;
    if (outbound.method() === "GET" && (path === "/api/sessions" || path.startsWith("/api/sessions/"))) {
      sessionReads.push(path);
    }
  });
  const start = async (agentName = worker) => {
    const response = await request.post(`${sessions}/${parent.id}/delegations`, { headers, data: {
      agent_name: agentName, request: "Read an offline test marker",
    } });
    expect(response.status()).toBe(201);
    const { sub_session_id } = await response.json() as { sub_session_id: string };
    return page.locator(`.session-item[data-session-id="${sub_session_id}"]`);
  };

  const first = await start();
  await expect(hiddenPill).toHaveText(/1 delegation/, { timeout: 5_000 });
  await expect(first).toHaveCount(0);
  await hiddenPill.click();
  await expect(first).toBeVisible();
  const second = await start();
  await expect(second).toBeVisible();
  await expect(activeRow).toHaveClass(/active/);
  expect(sessionReads).toEqual([]);

  await page.locator(".agent-item", { hasText: longNamePrefix }).click();
  const oversized = await start(longName);
  await expect(oversized).toBeVisible({ timeout: 5_000 });
  await expect(activeRow).toHaveClass(/active/);
  expect(sessionReads).toContain("/api/sessions");
  expect(sessionReads.every((path) => path === "/api/sessions")).toBe(true);
});

import { accountToken, enterAccountCredentials } from "./current-account";
import { expect, test, type Page } from "./test";
import { systemAgentId } from "./system-agent";

const API = "http://localhost:8765/api";
const headers = { get Authorization() { return `Bearer ${accountToken()}`; } };
async function select(page: Page, name: string) {
  const agent = page.locator(".agent-item", { hasText: "Octo" });
  await expect(agent).toBeVisible();
  const row = page.locator(".session-item", { hasText: name });
  if (!(await row.isVisible())) await agent.click();
  await row.click();
  await expect(page.locator(".conn-status.on")).toBeVisible();
}

test("shared chat text survives refresh, while clearing, sending, deletion and sign-out never revive it", async ({ page, request }) => {
  const sent: Record<string, unknown>[] = [];
  await page.routeWebSocket(/\/ws(?:\?|$)/, socket => {
    const server = socket.connectToServer();
    socket.onMessage(message => {
      const data = JSON.parse(String(message));
      if (data.type !== "send_message") { server.send(message); return; }
      sent.push(data);
      socket.send(JSON.stringify({ type: "submission", session_id: data.session_id,
        client_request_id: data.client_request_id, accepted: true, request_id: "observed-message" }));
    });
  });
  const rows: { id: string; name: string }[] = [];
  try {
    for (const suffix of ["A", "B"]) {
      const response = await request.post(`${API}/sessions`, { headers, data: {
        agent_id: await systemAgentId(request, `${API}/sessions`, headers), name: `Shared chat draft ${suffix}`,
      } });
      expect(response.ok()).toBeTruthy(); rows.push(await response.json());
    }
    await page.goto("/"); await enterAccountCredentials(page); await select(page, rows[0].name);
    const input = page.locator(".chat-input-bar textarea");
    await input.fill("Unsent shared text 中文");
    await select(page, rows[1].name);
    await expect(input).toHaveValue("Unsent shared text 中文");
    await page.reload(); await select(page, rows[0].name);
    await expect(input).toHaveValue("Unsent shared text 中文");
    expect(sent).toHaveLength(0);
    await input.fill("");
    await page.reload(); await select(page, rows[0].name);
    await expect(input).toHaveValue("");
    await input.fill("Explicit message only");
    await page.getByRole("button", { name: "Send message" }).click();
    await expect.poll(() => sent.length).toBe(1);
    await page.reload(); await select(page, rows[0].name);
    await expect(input).toHaveValue("");
    expect(sent).toHaveLength(1);
    await input.fill("Delete current shared text");
    expect((await request.delete(`${API}/sessions/${rows[0].id}`, { headers })).ok()).toBeTruthy();
    await expect(page.locator(".session-item", { hasText: rows[0].name })).toHaveCount(0);
    await page.reload(); await select(page, rows[1].name);
    await expect(input).toHaveValue("");
    await input.fill("Private before sign-out");
    await page.getByRole("button", { name: "Account menu" }).click();
    await page.getByRole("menuitem", { name: "Sign out" }).click();
    await enterAccountCredentials(page); await select(page, rows[1].name);
    await expect(input).toHaveValue("");
    expect(sent).toHaveLength(1);
  } finally {
    for (const row of rows) await request.delete(`${API}/sessions/${row.id}`, { headers }).catch(() => {});
  }
});

test("refresh after unknown compact never restores its command or replays its POST, even after storage refusal", async ({ page, request }) => {
  let posts = 0;
  await page.route("**/api/sessions/*/submissions**", route => {
    if (route.request().method() === "POST") { posts++; return route.abort("failed"); }
    if (/\/submissions\/[^/]+$/.test(route.request().url())) return route.fulfill({ status: 404, body: "Missing" });
    return route.continue();
  });
  const response = await request.post(`${API}/sessions`, { headers, data: {
    agent_id: await systemAgentId(request, `${API}/sessions`, headers), name: "Consumed compact refresh",
  } });
  expect(response.ok()).toBeTruthy();
  const row = await response.json() as { id: string; name: string };
  try {
    await page.goto("/"); await enterAccountCredentials(page); await select(page, row.name);
    const input = page.locator(".chat-input-bar textarea");
    await input.fill("/compact");
    await page.getByRole("button", { name: "Send message" }).click();
    await expect(page.getByRole("button", { name: "Retry admission confirmation" })).toBeVisible();
    await expect(input).toHaveValue("/compact");
    await page.reload(); await select(page, row.name);
    await expect(input).toHaveValue("");
    await expect(page.getByRole("button", { name: "Retry admission confirmation" })).toHaveCount(0);
    expect(posts).toBe(1);
    await page.evaluate(() => {
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function(key, value) {
        if (this === sessionStorage) throw new DOMException("denied", "SecurityError");
        original.call(this, key, value);
      };
    });
    await input.fill("Still editable after storage refusal");
    await expect(page.getByText(/Text draft could not be saved/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Send message" })).toBeEnabled();
    await input.fill("");
    await page.reload(); await select(page, row.name);
    await expect(input).toHaveValue("");
    expect(posts).toBe(1);
  } finally { await request.delete(`${API}/sessions/${row.id}`, { headers }).catch(() => {}); }
});

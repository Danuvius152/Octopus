import { TEST_BEARER, enterAccountCredentials } from "./current-account";
import { expect, test, type Page } from "./test";

test("confirmed REST deletion removes a selected session from both online pages", async ({ page, context, request }) => {
  const api = "http://localhost:8765/api";
  const headers = { Authorization: `Bearer ${TEST_BEARER}` };
  const agents = await (await request.get(`${api}/agents`, { headers })).json() as { id: string; name: string; archived?: boolean; pinned?: boolean }[];
  const agent = agents.find((row) => !row.archived && row.pinned !== false);
  if (!agent) throw new Error("The offline harness needs a visible active agent");
  const created = await request.post(`${api}/agents/${agent.id}/sessions`, { headers, data: { name: `Deleted on both pages ${Date.now()}` } });
  expect(created.ok()).toBeTruthy();
  const session = await created.json() as { id: string; name: string };
  const second = await context.newPage();
  const received = new Map<Page, Record<string, unknown>[]>();
  const open = async (browser: Page) => {
    const events: Record<string, unknown>[] = [];
    received.set(browser, events);
    browser.on("websocket", (socket) => socket.on("framereceived", (frame) => events.push(JSON.parse(frame.payload.toString()))));
    await browser.goto("/");
    await expect(browser.locator("#username, .agent-item").first()).toBeVisible();
    if (await browser.locator("#username").isVisible()) await enterAccountCredentials(browser);
    const row = browser.locator(".session-item", { hasText: session.name });
    if (!(await row.isVisible())) await browser.locator(".agent-item").filter({ has: browser.getByText(agent.name, { exact: true }) }).click();
    await row.click();
    await expect(browser.locator(".chat-header .crumb-current")).toHaveText(session.name);
    await expect(browser.locator(".conn-status.on")).toBeVisible();
    await browser.getByPlaceholder("Send a message…").fill("Private unfinished input");
  };
  try {
    await open(page); await open(second);
    const deleted = await request.delete(`${api}/sessions/${session.id}`, { headers });
    expect(deleted.status()).toBe(204);
    for (const browser of [page, second]) {
      await expect.poll(() => received.get(browser)!.filter((event) => event.type === "session_deleted" && event.session_id === session.id).length).toBe(1);
      await expect(browser.locator(".session-item", { hasText: session.name })).toHaveCount(0);
      await expect(browser.locator(".chat-empty")).toContainText("Pick a session from the sidebar");
      await expect(browser.locator(".chat-input-bar textarea")).toHaveCount(0);
      await expect(browser.locator(".conn-status.on")).toBeVisible();
      await browser.reload();
      await expect(browser.locator(".session-item", { hasText: session.name })).toHaveCount(0);
    }
  } finally {
    await second.close();
    await request.delete(`${api}/sessions/${session.id}`, { headers }).catch(() => {});
  }
});

import { accountToken, enterAccountCredentials } from "./current-account";
import { expect, test, type Page } from "./test";
import { systemAgentId } from "./system-agent";
import { deferred } from "../src/test-support/deferred";
import type { SessionInfo } from "../src/api";

const API = "http://localhost:8765/api";
const auth = { get Authorization() { return `Bearer ${accountToken()}`; } };

async function openArchive(page: Page) {
  await page.getByRole("button", { name: "Account menu" }).click();
  await page.getByRole("menuitem", { name: "Archived sessions" }).click();
}

test("Delete all keeps a new archive and a restored session while offering a failed target for retry", async ({ page, request }) => {
  const createdAgent = await request.post(`${API}/agents`, { headers: auth, data: { name: "Second archive owner" } });
  expect(createdAgent.status()).toBe(201);
  const { id: secondAgent } = await createdAgent.json();
  const createSession = async (name: string, agent_id?: string) => {
    const response = await request.post(`${API}/sessions/import`, { headers: auth, data: {
      name, agent_id, messages: [{ role: "user", type: "text", content: "Archive history" }],
    } });
    expect(response.status()).toBe(201);
    return await response.json() as SessionInfo;
  };
  const removed = await createSession("History to remove");
  const restored = await createSession("History restored elsewhere", secondAgent);
  const retry = await createSession("History needing retry", secondAgent);
  const newcomer = await createSession("History archived after confirmation");
  const archive = async (id: string) => {
    expect((await request.post(`${API}/sessions/${id}/archive?replace=false`, { headers: auth })).status()).toBe(204);
  };
  for (const session of [removed, restored, retry]) await archive(session.id);
  let newcomerArchived = false;
  let retryTargetFailed = false;
  await page.goto("/");
  const browserOrigin = new URL(page.url()).origin;
  await page.route(url => url.origin === browserOrigin && /^\/api\/sessions\/[^/]+$/.test(url.pathname) &&
      url.searchParams.get("archived_only") === "true", async route => {
    if (route.request().method() !== "DELETE") return route.fallback();
    const id = new URL(route.request().url()).pathname.split("/").at(-1);
    if (!newcomerArchived) {
      newcomerArchived = true;
      await archive(newcomer.id);
    }
    if (id === restored.id) {
      const path = `${API}/sessions/${id}`;
      const { service_generation } = await (await request.get(`${path}/restoration`, { headers: auth })).json();
      const response = await request.post(`${path}/unarchive`, { headers: auth, data: {
        operation_id: crypto.randomUUID(), service_generation,
      } });
      expect(response.ok()).toBeTruthy();
    }
    if (id === retry.id && !retryTargetFailed) {
      retryTargetFailed = true;
      await route.fulfill({ status: 503, json: { detail: "Temporary deletion failure" } });
    } else {
      await route.fallback();
    }
  });
  await enterAccountCredentials(page);
  await expect(page.locator(".conn-status.on")).toBeVisible();
  await openArchive(page);
  const dialog = page.getByRole("dialog");
  await expect(dialog.locator(".archived-agent-group")).toHaveCount(2);
  await expect(dialog.locator(".archived-session-row")).toHaveCount(3);
  page.once("dialog", async confirm => {
    expect(confirm.message()).toBe("Permanently delete 3 archived sessions?");
    await confirm.accept();
  });
  await dialog.getByRole("button", { name: "Delete all" }).click();
  await expect(dialog.getByRole("status")).toContainText("1 session was restored");
  await expect(dialog.locator(".archived-session-row")).toHaveCount(2);
  for (const name of [retry.name, newcomer.name]) await expect(dialog.getByText(name, { exact: true })).toBeVisible();
  await expect(dialog.getByRole("alert")).toContainText(retry.name);
  await dialog.getByRole("button", { name: "Retry deletion" }).click();
  await expect(dialog.locator(".archived-session-name")).toHaveText([newcomer.name]);
  await expect(dialog.getByRole("status")).toContainText("1 session was restored");
  for (const [session, archived] of [[removed, null], [retry, null], [restored, false], [newcomer, true]] as const) {
    const response = await request.get(`${API}/sessions/${session.id}/presence`, { headers: auth });
    expect(await response.json()).toEqual({ archived, deletion_pending: false });
  }
});

test("confirmed REST deletion removes a selected session from both online pages", async ({ page, context, request }) => {
  const api = "http://localhost:8765/api";
  const headers = { Authorization: `Bearer ${accountToken()}` };
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
    const asked = await request.post(`${api}/sessions/${session.id}/questions`, {
      headers, data: { questions: [{ question: "Keep each page's unfinished answer?" }] },
    });
    expect(asked.status()).toBe(201);
    const question = await asked.json() as { question_id: string };
    for (const browser of [page, second]) {
      await browser.getByPlaceholder("Or type your own answer…").fill("Private unfinished answer draft");
    }
    expect((await request.delete(`${api}/sessions/${session.id}/questions/${question.question_id}`, { headers })).ok()).toBeTruthy();
    for (const browser of [page, second]) {
      await expect(browser.getByText("This question closed. Your saved input is kept here.")).toBeVisible();
      await expect(browser.getByRole("button", { name: "Continue as new message" })).toBeDisabled();
    }
    const deleted = await request.delete(`${api}/sessions/${session.id}`, { headers });
    expect(deleted.status()).toBe(204);
    for (const browser of [page, second]) {
      await expect.poll(() => received.get(browser)!.filter((event) => event.type === "session_deleted" && event.session_id === session.id).length).toBe(1);
      await expect(browser.locator(".session-item", { hasText: session.name })).toHaveCount(0);
      await expect(browser.locator(".chat-empty")).toContainText("Pick a session from the sidebar");
      await expect(browser.locator(".chat-input-bar textarea")).toHaveCount(0);
      await expect(browser.getByRole("button", { name: "Continue as new message" })).toHaveCount(0);
      await expect(browser.locator(".conn-status.on")).toBeVisible();
      await browser.reload();
      await expect(browser.locator(".session-item", { hasText: session.name })).toHaveCount(0);
    }
  } finally {
    await second.close();
    await request.delete(`${api}/sessions/${session.id}`, { headers }).catch(() => {});
  }
});

for (const loseReceipt of [false, true]) {
  test(`archived deletion converges after ${loseReceipt ? "the response and notification are lost" : "only the notification is lost"}`, async ({ page, request }) => {
    const created = await request.post(`${API}/sessions/import`, { headers: auth, data: {
      name: `Delete archived history (${loseReceipt ? "no receipt" : "refresh"})`,
      messages: [{ role: "user", type: "text", content: "Saved history to delete" }],
    } });
    expect(created.ok()).toBeTruthy();
    const session = await created.json() as SessionInfo;
    const path = `${API}/sessions/${session.id}`;
    let dropped = 0;
    await page.routeWebSocket("**/ws?*", socket => {
      const server = socket.connectToServer();
      server.onMessage(message => {
        const frame = JSON.parse(String(message));
        if (frame.type === "session_deleted" && frame.session_id === session.id) dropped++;
        else socket.send(message);
      });
    });
    const reply = deferred<number>();
    if (loseReceipt) {
      await page.route(`**/api/sessions/${session.id}?archived_only=true`, async route => {
        const response = await route.fetch();
        reply.resolve(response.status());
        await route.abort("connectionreset");
      });
    }
    expect((await request.post(`${path}/archive?replace=false`, { headers: auth })).status()).toBe(204);
    await page.goto("/"); await enterAccountCredentials(page);
    await expect(page.locator(".conn-status.on")).toBeVisible();
    await openArchive(page);
    await page.locator(".archived-session-row", { hasText: session.name }).getByRole("button", { name: "View", exact: true }).click();
    await expect(page.locator(".chat-archived-banner")).toBeVisible();
    await expect(page.getByText("Saved history to delete")).toBeVisible();
    const confirmed = page.waitForResponse(response => new URL(response.url()).pathname === `/api/sessions/${session.id}/presence`);
    if (loseReceipt) {
      await openArchive(page);
      page.once("dialog", async dialog => { expect(dialog.message()).toContain("Permanently delete"); await dialog.accept(); });
      await page.locator(".archived-session-row", { hasText: session.name }).getByRole("button", { name: "Delete", exact: true }).click();
      expect(await reply.promise).toBe(204);
    } else {
      expect((await request.delete(path, { headers: auth, params: { archived_only: "true" } })).status()).toBe(204);
      await expect.poll(() => dropped).toBe(1);
      await expect(page.locator(".chat-archived-banner")).toBeVisible();
      await openArchive(page);
    }
    expect(await (await confirmed).json()).toEqual({ archived: null, deletion_pending: false });
    await expect.poll(() => dropped).toBe(1);
    await expect(page.locator(".archived-session-row", { hasText: session.name })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Retry deletion" })).toHaveCount(0);
    await page.getByRole("button", { name: "Close" }).click();
    await expect(page.locator(".chat-empty")).toContainText("Pick a session from the sidebar");
  });
}

for (const observation of ["refresh", "reconnect"]) {
  test(`a ${observation} spanning restoration leaves the current draft intact until the late event`, async ({ page, request }) => {
    const agentId = await systemAgentId(request, `${API}/sessions`, auth);
    const created = await request.post(`${API}/sessions`, { headers: auth, data: { agent_id: agentId, name: `Restore during ${observation}` } });
    expect(created.ok()).toBeTruthy();
    const session = await created.json() as SessionInfo;
    const path = `${API}/sessions/${session.id}`;
    const lateNotice = deferred<() => void>();
    let disconnect: (() => Promise<void>) | undefined;
    await page.routeWebSocket("**/ws?*", socket => {
      const server = socket.connectToServer();
      disconnect = () => socket.close();
      server.onMessage(message => {
        const frame = JSON.parse(String(message));
        if (frame.type === "session_unarchived" && frame.session_id === session.id) lateNotice.resolve(() => socket.send(message));
        else socket.send(message);
      });
    });
    await page.goto("/"); await enterAccountCredentials(page);
    await expect(page.locator(".conn-status.on")).toBeVisible();
    const liveRow = page.locator(".session-item", { hasText: session.name });
    if (!(await liveRow.isVisible())) await page.locator(".agent-item", { hasText: "Octo" }).click();
    await liveRow.click();
    const composer = page.getByPlaceholder("Send a message…");
    await composer.fill("Keep this draft through restore");
    expect((await request.post(`${path}/archive?replace=false`, { headers: auth })).status()).toBe(204);
    await expect(page.locator(".chat-archived-banner")).toBeVisible();
    await openArchive(page);
    await expect(page.locator(".archived-session-row", { hasText: session.name })).toBeVisible();
    await page.getByRole("button", { name: "Close" }).click();

    const { service_generation } = await (await request.get(`${path}/restoration`, { headers: auth })).json();
    const confirmed = page.waitForResponse(response => new URL(response.url()).pathname === `/api/sessions/${session.id}/presence`);
    const releaseDetail = deferred<void>();
    if (observation === "reconnect") {
      await page.route(`**/api/sessions/${session.id}?view=browser`, async route => {
        await releaseDetail.promise;
        await route.continue();
      });
    }
    await page.route(/\/api\/sessions\?include_archived=true$/, async route => {
      // Capture the live and archived projections on opposite sides of a real restore.
      const live = await (await request.get(`${API}/sessions`, { headers: auth })).json() as SessionInfo[];
      const restored = await request.post(`${path}/unarchive`, { headers: auth, data: {
        operation_id: `restore-during-${observation}`, service_generation,
      } });
      expect(await restored.json()).toMatchObject({ state: "finished", status_code: null });
      const response = await route.fetch();
      const rows = await response.json() as SessionInfo[];
      const split = [...live, ...rows.filter(row => row.archived)];
      expect(split.some(row => row.id === session.id)).toBe(false);
      await route.fulfill({ response, json: split });
    }, { times: 1 });
    try {
      if (observation === "refresh") await openArchive(page);
      else { expect(disconnect).toBeDefined(); await disconnect!(); }

      expect(await (await confirmed).json()).toEqual({ archived: false, deletion_pending: false });
      const sendNotice = await lateNotice.promise;
      await expect(page.locator(".chat-archived-banner")).toBeVisible();
      await expect(page.locator(".chat-header .crumb-current")).toHaveText(session.name);
      if (observation === "refresh") await page.getByRole("button", { name: "Close" }).click();
      sendNotice();
      await expect(page.locator(".chat-archived-banner")).toHaveCount(0);
      await expect(page.locator(".chat-header .crumb-current")).toHaveText(session.name);
      await expect(composer).toHaveValue("Keep this draft through restore");
    } finally {
      releaseDetail.resolve();
    }
  });
}

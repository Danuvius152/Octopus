import { TEST_BEARER, enterAccountCredentials } from "./current-account";
import {
  test, expect, type Page, type APIRequestContext, type WebSocketRoute,
} from "@playwright/test";

// The research UI uses controlled REST responses and WS events so cancellation
// cannot race a real model finishing (or an unavailable CLI failing). Sessions
// and sign-in still use the real backend. ResearchManager's cancellation is
// covered by backend unit tests; the live pipeline by test_research_real.py.

const TOKEN = TEST_BEARER;
const API = "http://localhost:8765/api";
const OWNED = new Set(["Research E2E"]);

test.afterAll(async ({ request }) => {
  try {
    const res = await request.get(`${API}/sessions`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
      params: { include_archived: "true" },
      timeout: 5_000,
    });
    if (res.ok()) {
      const sessions: { id: string; name: string }[] = await res.json();
      for (const s of sessions) {
        if (!OWNED.has(s.name)) continue;
        await request
          .delete(`${API}/sessions/${s.id}`, {
            headers: { Authorization: `Bearer ${TOKEN}` },
            timeout: 3_000,
          })
          .catch(() => {});
      }
    }
  } catch {
    /* best-effort */
  }
});

async function login(page: Page) {
  await page.goto("/");
  await enterAccountCredentials(page);
  await expect(page.locator(".agent-list-header")).toBeVisible();
  // Sessions live behind a folded agent row; open the default
  // agent's rail so they're reachable.
  await openSessionRail(page);
}

/** Open an agent's session rail. Every agent's session list starts folded
 * (SidebarAgents keeps the fold state empty on load and never unfolds by
 * itself), so reaching a session means clicking its agent first — exactly
 * what a user does. Idempotent: a rail that's already open is left open. */
async function openSessionRail(page: Page, agentName = "Octo") {
  const row = page.locator(".agent-item", { hasText: agentName });
  await expect(row).toBeVisible();
  if ((await row.locator(".agent-fold.rotate-90").count()) === 0) {
    await row.click();
    await expect(row.locator(".agent-fold.rotate-90")).toBeVisible();
  }
}

async function importSession(request: APIRequestContext): Promise<{ id: string }> {
  const res = await request.post(`${API}/sessions/import`, {
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    data: {
      name: "Research E2E",
      messages: [{ role: "user", type: "text", content: "hello" }],
    },
  });
  expect(res.ok()).toBeTruthy();
  return res.json();
}

test("the /research command surfaces a live research card", async ({ page, request }) => {
  const session = await importSession(request);
  const question = "what is the capital of France";
  const job = {
    id: "research-ui-cancel",
    session_id: session.id,
    question,
    status: "running",
    phase: "scope",
  };
  let started = false;
  let socket: WebSocketRoute | undefined;
  await page.routeWebSocket("**/ws?*", (ws) => {
    ws.connectToServer();
    socket = ws;
  });
  await page.route(`**/api/sessions/${session.id}/research`, async (route) => {
    const req = route.request();
    const bearer = await page.evaluate(() => localStorage.getItem("octopus_token") ?? "");
    expect(bearer).toBeTruthy();
    expect(req.headers().authorization).toBe(`Bearer ${bearer}`);
    if (req.method() === "GET") {
      await route.fulfill({ json: started ? [job] : [] });
      return;
    }
    expect(req.method()).toBe("POST");
    expect(req.postDataJSON()).toEqual({ question });
    started = true;
    await route.fulfill({ status: 201, json: job });
  });
  const cancelPath = `/api/sessions/${session.id}/research/${job.id}/cancel`;
  await page.route(`**${cancelPath}`, async (route) => {
    expect(route.request().method()).toBe("POST");
    const bearer = await page.evaluate(() => localStorage.getItem("octopus_token") ?? "");
    expect(bearer).toBeTruthy();
    expect(route.request().headers().authorization).toBe(`Bearer ${bearer}`);
    job.status = "cancelled";
    await route.fulfill({ json: job });
    expect(socket).toBeDefined();
    socket!.send(JSON.stringify({
      type: "research_failed",
      session_id: session.id,
      research_id: job.id,
      status: "cancelled",
      error: "cancelled by user",
    }));
  });
  await login(page);

  await page
    .locator(".session-item .session-name", { hasText: "Research E2E" })
    .click();
  await expect(page.locator(".chat-header .crumb-current")).toHaveText("Research E2E");

  // Fire the slash command. A space after the command hides the slash menu, so
  // Enter sends rather than completing a menu item.
  const composer = page.locator(".chat-input-bar textarea");
  await composer.fill(`/research ${question}`);
  await composer.press("Enter");

  // The command's 201 creates the card; subsequent changes arrive over WS.
  const card = page.locator(".research-card").first();
  await expect(card).toBeVisible({ timeout: 15_000 });
  await expect(card).toContainText(question);
  await expect(card).toHaveAttribute("data-status", "running");

  expect(socket).toBeDefined();
  socket!.send(JSON.stringify({
    type: "research_progress",
    session_id: session.id,
    research_id: job.id,
    phase: "search",
    detail: "Checking sources",
  }));
  await expect(card.locator(".research-phase")).toContainText("Checking sources");

  // The cancel request identifies this session and job; its WS event is what
  // removes the button and changes the status, rather than the HTTP response.
  const cancellation = page.waitForRequest((req) =>
    new URL(req.url()).pathname === cancelPath && req.method() === "POST",
  );
  await card.locator(".btn-research-cancel").click();
  await cancellation;
  await expect(card).toHaveAttribute("data-status", "cancelled", {
    timeout: 20_000,
  });
  await expect(card.locator(".btn-research-cancel")).toHaveCount(0);
});

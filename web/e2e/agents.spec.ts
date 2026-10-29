import { TEST_BEARER, enterAccountCredentials } from "./current-account";
import { test, expect, type Page } from "./test";
import { systemAgentId } from "./system-agent";

const TOKEN = TEST_BEARER;
const AGENTS_API = "http://localhost:8765/api/agents";
const SESSIONS_API = "http://localhost:8765/api/sessions";

// Agent names this spec creates — cleaned up in afterAll so reruns against
// the same in-memory server don't accumulate (and unique-name create works).
const OWNED_AGENTS = new Set([
  "E2E Researcher",
  "E2E Persisted",
  "E2E Rail",
  "E2E Codex Agent",
  "E2E Stable sessions",
]);
const OWNED_SESSIONS = new Set(["Agent Thread"]);

async function login(page: Page) {
  await page.goto("/");
  await enterAccountCredentials(page);
  await expect(page.locator(".agent-list-header h2")).toHaveText("Agents");
}

/** Open the active agent's settings via the account menu (no sidebar gear). */
async function openAgentSettings(page: Page) {
  await page.locator(".btn-account").click();
  await page.locator(".menu-agent-settings").click();
  await expect(page.locator(".agent-settings")).toBeVisible();
}

test.afterAll(async ({ request }) => {
  const headers = { Authorization: `Bearer ${TOKEN}` };
  // Delete owned sessions first.
  const sRes = await request.get(SESSIONS_API, { headers });
  if (sRes.ok()) {
    for (const s of (await sRes.json()) as { id: string; name: string }[]) {
      if (OWNED_SESSIONS.has(s.name)) {
        await request.delete(`${SESSIONS_API}/${s.id}`, { headers }).catch(() => {});
      }
    }
  }
  // Archive owned agents (archive works whether or not they have sessions).
  const aRes = await request.get(AGENTS_API, { headers });
  if (aRes.ok()) {
    for (const a of (await aRes.json()) as { id: string; name: string }[]) {
      if (OWNED_AGENTS.has(a.name)) {
        await request
          .post(`${AGENTS_API}/${a.id}/archive`, { headers })
          .catch(() => {});
      }
    }
  }
});

test.describe("Agents", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("the Default Agent is present", async ({ page }) => {
    await expect(
      page.locator(".agent-item .agent-name", { hasText: "Octo" })
    ).toBeVisible();
  });

  test("selecting sessions keeps their sidebar positions", async ({ page, request }) => {
    const headers = { Authorization: `Bearer ${TOKEN}` };
    const response = await request.post(AGENTS_API, { headers, data: { name: "E2E Stable sessions" } });
    expect(response.ok()).toBe(true);
    const agent = await response.json();
    const sessions: { id: string; name: string }[] = [];
    for (const name of ["Stable first", "Stable middle", "Stable last"]) {
      const created = await request.post(`${AGENTS_API}/${agent.id}/sessions`, { headers, data: { name } });
      expect(created.ok()).toBe(true);
      sessions.push(await created.json());
    }
    try {
      await page.reload();
      await page.locator(".agent-item", { hasText: agent.name }).click();
      const rail = page.locator(".agent-group", { hasText: agent.name }).locator(".session-rail");
      const names = rail.locator(".session-name");
      await expect(names).toHaveCount(3);
      const order = await names.allTextContents();

      for (const name of [order[0], order[1], order[0]]) {
        const selected = sessions.find((session) => session.name === name)!;
        // Research is requested after the selected detail has been applied,
        // so the assertion cannot pass before the list update that used to move it.
        const applied = page.waitForRequest((request) => request.url().endsWith(`/api/sessions/${selected.id}/research`));
        await rail.locator(".session-item", { hasText: name }).click();
        await applied;
        await expect(page.locator(".chat-header .crumb-current")).toHaveText(name);
        await expect(names).toHaveText(order);
      }
    } finally {
      for (const session of sessions) await request.delete(`${SESSIONS_API}/${session.id}`, { headers });
    }
  });

  test("creates an agent and a session under it", async ({ page }) => {
    await page.locator(".btn-agent-add").click();
    await page.locator("#agent-name").fill("E2E Researcher");
    await page.locator("#agent-prompt").fill("You research diligently.");
    await page.locator(".btn-agent-save").click();

    // The new agent appears and becomes active.
    const agentRow = page.locator(".agent-item", { hasText: "E2E Researcher" });
    await expect(agentRow).toBeVisible();
    await agentRow.click();

    // Create a session under this agent (+ lives on the agent's own row).
    await agentRow.locator(".btn-session-add").click();
    await page
      .locator('.session-create input[placeholder="Session name"]')
      .fill("Agent Thread");
    await page.locator("button.btn-create").click();

    await expect(
      page.locator(".session-item .session-name", { hasText: "Agent Thread" })
    ).toBeVisible();
    // Chat header shows both the agent and the session.
    await expect(page.locator(".chat-header .crumb-current")).toHaveText("Agent Thread");
    // The header breadcrumb carries the agent, then the session.
    await expect(page.locator(".chat-header")).toContainText("E2E Researcher");
  });

  test("a delayed create receipt cannot revive a session archived by another client", async ({ page, request }) => {
    const witnessResponse = await request.post(SESSIONS_API, {
      headers: { Authorization: `Bearer ${TOKEN}` },
      data: { agent_id: await systemAgentId(request, SESSIONS_API, { Authorization: `Bearer ${TOKEN}` }), name: "E2E Archive witness" },
    });
    expect(witnessResponse.ok()).toBe(true);
    const witness = await witnessResponse.json();
    await page.reload();
    let created!: (id: string) => void;
    const serverCreated = new Promise<string>((resolve) => { created = resolve; });
    let release!: () => void;
    const receipt = new Promise<void>((resolve) => { release = resolve; });
    await page.route("**/api/agents/*/sessions", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      const response = await route.fetch();
      created((await response.json()).id);
      await receipt;
      await route.fulfill({ response });
    });
    const agent = page.locator(".agent-item", { hasText: "Octo" });
    await agent.locator(".btn-session-add").click();
    const witnessRow = page.locator(".session-item .session-name", { hasText: "E2E Archive witness" });
    await expect(witnessRow).toBeVisible();
    await page.locator('.session-create input[placeholder="Session name"]').fill("E2E Delayed creation");
    await page.locator("button.btn-create").click();
    const id = await serverCreated;
    try {
      const archived = await request.post(`${SESSIONS_API}/${id}/archive?replace=false`, {
        headers: { Authorization: `Bearer ${TOKEN}` },
      });
      expect(archived.ok()).toBe(true);
      const witnessed = await request.post(`${SESSIONS_API}/${witness.id}/archive?replace=false`, {
        headers: { Authorization: `Bearer ${TOKEN}` },
      });
      expect(witnessed.ok()).toBe(true);
      // This later event on the same socket proves the earlier archive handler
      // ran before the delayed creation response is released.
      await expect(witnessRow).toHaveCount(0);
      const confirmed = page.waitForResponse((response) => response.url().endsWith("/api/sessions") && response.request().method() === "GET");
      release();
      await confirmed;
      await expect(page.locator(".session-item .session-name", { hasText: "E2E Delayed creation" })).toHaveCount(0);
    } finally {
      release();
      await request.delete(`${SESSIONS_API}/${id}`, { headers: { Authorization: `Bearer ${TOKEN}` } });
      await request.delete(`${SESSIONS_API}/${witness.id}`, { headers: { Authorization: `Bearer ${TOKEN}` } });
    }
  });

  test("edits an agent's system prompt and it persists", async ({ page }) => {
    // Create the agent.
    await page.locator(".btn-agent-add").click();
    await page.locator("#agent-name").fill("E2E Persisted");
    await page.locator("#agent-prompt").fill("first prompt");
    await page.locator(".btn-agent-save").click();

    const agentRow = page.locator(".agent-item", { hasText: "E2E Persisted" });
    await expect(agentRow).toBeVisible();

    // Make it the active agent, then edit via the account menu (no gear).
    await agentRow.click();

    await openAgentSettings(page);
    await expect(page.locator("#agent-prompt")).toHaveValue("first prompt");
    await page.locator("#agent-prompt").fill("second prompt — edited");
    await page.locator(".btn-agent-save").click();

    // Reopen again — the edit persisted (proves PATCH + store upsert).
    await openAgentSettings(page);
    await expect(page.locator("#agent-prompt")).toHaveValue(
      "second prompt — edited"
    );
  });

  test("the account menu edits whichever agent the sidebar has selected", async ({
    page,
  }) => {
    // Create an agent so there are at least two to switch between.
    await page.locator(".btn-agent-add").click();
    await page.locator("#agent-name").fill("E2E Rail");
    await page.locator("#agent-prompt").fill("rail prompt");
    await page.locator(".btn-agent-save").click();
    await expect(
      page.locator(".agent-item", { hasText: "E2E Rail" })
    ).toBeVisible();

    // The form opens on the active agent — the one we just created.
    await openAgentSettings(page);
    await expect(page.locator("#agent-name")).toHaveValue("E2E Rail");
    // An ordinary agent can be archived from here.
    await expect(page.locator(".btn-agent-archive")).toBeVisible();

    // Selecting Octo in the sidebar and reopening reseeds the form, and the
    // protected agent offers no Archive.
    await page.locator(".btn-agent-cancel").click();
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await openAgentSettings(page);
    await expect(page.locator("#agent-name")).toHaveValue("Octo");
    await expect(page.locator(".btn-agent-archive")).toHaveCount(0);

    // The sidebar "+" always opens a blank draft, whatever is selected.
    await page.locator(".btn-agent-add").click();
    await expect(page.locator("#agent-name")).toHaveValue("");
  });

  test("sets an agent's default harness to Codex and it persists", async ({
    page,
  }) => {
    // The codex CLI is on PATH in the test env, so /api/backends reports it
    // and the Harness selector appears.
    await page.locator(".btn-agent-add").click();
    await page.locator("#agent-name").fill("E2E Codex Agent");
    await expect(page.locator(".agent-backend-select")).toBeVisible();
    await page.locator(".btn-agent-backend-codex").click();
    await expect(page.locator(".btn-agent-backend-codex")).toHaveAttribute(
      "aria-checked",
      "true"
    );
    await page.locator(".btn-agent-save").click();

    const row = page.locator(".agent-item", { hasText: "E2E Codex Agent" });
    await expect(row).toBeVisible();
    await row.click();

    // Reopen settings — the Codex default persisted.
    await openAgentSettings(page);
    await expect(page.locator("#agent-name")).toHaveValue("E2E Codex Agent");
    await expect(page.locator(".btn-agent-backend-codex")).toHaveAttribute(
      "aria-checked",
      "true"
    );
  });

  test("the Default Agent cannot be archived from settings", async ({ page }) => {
    const def = page.locator(".agent-item", { hasText: "Octo" });
    // Select Octo, then open its settings from the account menu.
    await def.click();
    await openAgentSettings(page);
    await expect(page.locator(".agent-settings #agent-name")).toHaveValue("Octo");
    // is_system agents expose no "Archive agent" button.
    await expect(page.locator(".btn-agent-archive")).toHaveCount(0);
  });
});

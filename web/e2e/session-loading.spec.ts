import { enterAccountCredentials, TEST_BEARER } from "./current-account";
import { systemAgentId } from "./system-agent";
import { expect, test } from "./test";

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

test("mobile creation acknowledges one submission and shows history before auxiliary data", async ({ page, request }) => {
  const headers = { Authorization: `Bearer ${TEST_BEARER}` };
  const endpoint = "http://localhost:8765/api/sessions";
  const agentId = await systemAgentId(request, endpoint, headers);
  let releaseCreate!: () => void;
  let releaseBackground!: () => void;
  const creation = new Promise<void>((resolve) => { releaseCreate = resolve; });
  const background = new Promise<void>((resolve) => { releaseBackground = resolve; });
  let createdId: string | null = null;
  let submissions = 0;
  let backgroundEntered = false;
  let backgroundReleased = false;
  await page.route(`**/api/agents/${agentId}/sessions`, async (route) => {
    submissions++;
    await creation;
    const response = await route.fetch();
    const row = await response.json();
    createdId = row.id;
    await route.fulfill({ response, json: row });
  });
  await page.route("**/api/sessions/*/bg-tasks", async (route) => {
    if (!createdId || !route.request().url().includes(`/${createdId}/`)) return route.fallback();
    backgroundEntered = true;
    await background;
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/sessions/*", async (route) => {
    if (new URL(route.request().url()).pathname !== `/api/sessions/${createdId}`) return route.fallback();
    const response = await route.fetch();
    const detail = await response.json();
    await route.fulfill({ response, json: { ...detail, message_count: 1,
      messages: [{ role: "assistant", type: "text", content: "History available while task details are pending", seq: 0 }],
      next_message_seq: 1, oldest_loaded_seq: 0, has_more_messages: false } });
  });
  try {
    await page.goto("/");
    await enterAccountCredentials(page);
    await page.getByRole("button", { name: "Toggle sidebar", exact: true }).click();
    await page.locator(".agent-item", { has: page.locator(`[aria-label="New session for Octo"]`) }).click();
    await page.getByRole("button", { name: "New session for Octo", exact: true }).tap();
    await page.getByPlaceholder("Session name").fill("E2E mobile responsive creation");
    const button = page.getByRole("button", { name: "Create session", exact: true });
    const rect = await button.boundingBox();
    expect(rect).not.toBeNull();
    await button.tap();
    await expect(page.getByText("Creating session…", { exact: true })).toBeVisible();
    await expect(button).toBeDisabled();
    await page.touchscreen.tap(rect!.x + rect!.width / 2, rect!.y + rect!.height / 2);
    await expect.poll(() => submissions).toBe(1);
    releaseCreate();
    await expect(page.locator(".chat-header")).toContainText("E2E mobile responsive creation");
    await expect(page.getByText("History available while task details are pending", { exact: true })).toBeVisible();
    expect(backgroundEntered).toBe(true);
    expect(backgroundReleased).toBe(false);
    expect(submissions).toBe(1);
    expect(await page.evaluate(() => matchMedia("(hover: none)").matches)).toBe(true);
  } finally {
    releaseCreate(); releaseBackground(); backgroundReleased = true;
    await page.unrouteAll({ behavior: "wait" });
    if (createdId) await request.delete(`${endpoint}/${createdId}`, { headers });
  }
});

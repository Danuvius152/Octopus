import { ACCOUNT_API } from "./account-resources";
import { accountToken, enterAccountCredentials } from "./current-account";
import { systemAgentId } from "./system-agent";
import { expect, test, type APIRequestContext, type Page } from "./test";

const endpoint = `${ACCOUNT_API}/sessions`;
const touchOptions = { viewport: { width: 320, height: 844 }, isMobile: true, hasTouch: true };

async function observeSession(page: Page, request: APIRequestContext, agentId: string, headers: Record<string, string>) {
  const response = await request.post(endpoint, { headers, data: { agent_id: agentId, name: "Existing conversation" } });
  expect(response.ok()).toBe(true);
  const { id } = await response.json() as { id: string };
  let status = "idle";
  // Publish running to the browser while keeping the real creation API offline.
  await page.route("**/api/sessions", async (route) => {
    if (status === "idle") return route.fallback();
    const response = await route.fetch();
    const sessions = await response.json() as { id: string }[];
    await route.fulfill({ response, json: sessions.map((session) => session.id === id ? { ...session, status } : session) });
  });
  return { id, markRunning: () => { status = "running"; } };
}

for (const touch of [false, true]) {
  test.describe(touch ? "touch drawer" : "desktop sidebar", () => {
    if (touch) test.use(touchOptions);

    test("creates a session while the agent already has a running session", async ({ page, request }) => {
      const headers = { Authorization: `Bearer ${accountToken()}` };
      const agentId = await systemAgentId(request, endpoint, headers);
      const original = await observeSession(page, request, agentId, headers);
      if (touch) original.markRunning();

      await page.goto("/");
      await enterAccountCredentials(page);
      const row = page.locator(".agent-item", { hasText: "Octo" });
      const openDrawer = page.getByRole("button", { name: "Toggle sidebar", exact: true });
      const badge = row.getByText("1 running", { exact: true });
      const add = row.getByRole("button", { name: "New session for Octo", exact: true });
      const sessionName = page.getByPlaceholder("Session name");
      let submissions = 0;
      page.on("request", (outbound) => {
        if (outbound.method() === "POST" && new URL(outbound.url()).pathname === `/api/agents/${agentId}/sessions`) {
          submissions++;
        }
      });
      if (touch) {
        expect(await page.evaluate(() => matchMedia("(hover: none)").matches)).toBe(true);
        await openDrawer.tap();
        await expect(page.getByRole("complementary", { name: "Sidebar" })).toHaveCSS("left", "0px");
        await expect(badge).toBeVisible();
        await expect(add).toHaveCSS("opacity", "0");
        const collapsedAdd = (await add.boundingBox())!;
        // Where the hidden + sits, a tap should unfold the row without opening the form.
        await page.touchscreen.tap(collapsedAdd.x + collapsedAdd.width / 2, collapsedAdd.y + collapsedAdd.height / 2);
        await expect(row).toHaveClass(/expanded/);
        await expect(sessionName).toBeHidden();
        await expect(add).toHaveCSS("opacity", "1");
        await add.tap();
      } else {
        await row.hover();
        await expect(add).toHaveCSS("opacity", "1");
        const idleX = (await add.boundingBox())!.x;
        original.markRunning();
        await page.reload();
        await row.hover();
        await expect(badge).toBeVisible();
        await expect(add).toHaveCSS("opacity", "1");
        expect((await add.boundingBox())!.x).toBe(idleX);
        await add.click();
      }

      const create = page.getByRole("button", { name: "Create session", exact: true });
      const cancel = page.getByRole("button", { name: "Cancel new session", exact: true });
      const pressAt = (x: number, y: number) => touch ? page.touchscreen.tap(x, y) : page.mouse.click(x, y);
      await expect(create).toBeInViewport({ ratio: 1 });
      await expect(cancel).toBeInViewport({ ratio: 1 });
      await expect(create).toHaveCSS("width", "24px");
      await expect(cancel).toHaveCSS("width", "24px");
      await expect(create.locator("svg")).toHaveAttribute("width", "13");
      await expect(cancel.locator("svg")).toHaveAttribute("width", "13");
      await sessionName.fill("Discarded conversation");
      if (touch) {
        const input = (await sessionName.boundingBox())!;
        for (const inset of [4, 1]) {
          await sessionName.blur();
          await page.touchscreen.tap(input.x + input.width - inset, input.y + input.height / 2);
          await expect(sessionName).toBeFocused();
          expect(submissions).toBe(0);
        }
      }
      const cancelBounds = (await cancel.boundingBox())!;
      const cancelX = cancelBounds.x + cancelBounds.width / 2 + 16;
      const cancelY = cancelBounds.y + cancelBounds.height / 2;
      await pressAt(cancelX, cancelY);
      await expect(sessionName).toBeHidden();
      expect(submissions).toBe(0);
      if (touch) await add.tap();
      else await add.click();
      await expect(sessionName).toHaveValue("");

      const createBounds = (await create.boundingBox())!;
      const reopenedCancel = (await cancel.boundingBox())!;
      const gapMidpoint = (createBounds.x + createBounds.width + reopenedCancel.x) / 2;
      await sessionName.fill("Another discarded conversation");
      await pressAt(gapMidpoint + 1, reopenedCancel.y - 4);
      await expect(sessionName).toBeHidden();
      expect(submissions).toBe(0);
      if (touch) await add.tap();
      else await add.click();
      await expect(sessionName).toHaveValue("");

      const name = "New session alongside running work";
      await sessionName.fill(name);
      const createY = createBounds.y + createBounds.height / 2;
      await pressAt(gapMidpoint - 2.5, createY);
      await expect(page.locator(".chat-header .crumb-current")).toHaveText(name);
      expect(submissions).toBe(1);
      if (touch) await openDrawer.tap();
      await expect(badge).toBeVisible();
      await expect(page.locator(`.session-item[data-session-id="${original.id}"] .status-running`)).toBeVisible();
      if (!touch) {
        await page.mouse.move(263, 400);
        await page.getByRole("button", { name: "Collapse sidebar" }).click();
        await expect(add).toBeHidden();
        await expect(badge).toBeHidden();
        await expect(row.locator(".agent-running .dot")).toBeVisible();
      }
    });
  });
}

test.describe("touch sidebar actions", () => {
  test.use(touchOptions);

  test("a running agent's pin and add buttons have distinct tap targets", async ({ page, request }) => {
    const headers = { Authorization: `Bearer ${accountToken()}` };
    const name = "An agent with a long display name";
    const response = await request.post(`${ACCOUNT_API}/agents`, { headers, data: { name } });
    expect(response.ok()).toBe(true);
    const { id } = await response.json() as { id: string };
    const original = await observeSession(page, request, id, headers);
    original.markRunning();

    await page.goto("/");
    await enterAccountCredentials(page);
    expect(await page.evaluate(() => matchMedia("(hover: none)").matches)).toBe(true);
    const openDrawer = page.getByRole("button", { name: "Toggle sidebar", exact: true });
    await openDrawer.tap();
    const row = page.locator(".agent-item", { hasText: name });
    await row.getByText(name, { exact: true }).tap();
    await expect(row.getByText("1 running", { exact: true })).toBeVisible();
    const unpin = row.getByRole("button", { name: `Unpin ${name} from the sidebar`, exact: true });
    const pin = row.getByRole("button", { name: `Pin ${name} to the sidebar`, exact: true });
    const sessionName = page.getByPlaceholder("Session name");

    await expect(unpin).toHaveCSS("opacity", "1");
    const unpinBounds = (await unpin.boundingBox())!;
    await page.touchscreen.tap(Math.floor(unpinBounds.x + unpinBounds.width) - 1, unpinBounds.y + unpinBounds.height / 2);
    await expect(pin).toBeInViewport();
    await expect(sessionName).toBeHidden();

    // Reopen from the persisted unpinned state as a separate interaction.
    await page.reload();
    await openDrawer.tap();
    await row.getByText(name, { exact: true }).tap();
    await expect(pin).toHaveCSS("opacity", "1");
    const pinBounds = (await pin.boundingBox())!;
    await page.touchscreen.tap(Math.floor(pinBounds.x + pinBounds.width) - 1, pinBounds.y + pinBounds.height / 2);
    await expect(unpin).toBeVisible();
    await expect(sessionName).toBeHidden();
    await row.getByRole("button", { name: `New session for ${name}`, exact: true }).tap();
    await expect(sessionName).toBeVisible();
  });
});

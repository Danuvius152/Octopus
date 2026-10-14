import { expect, test } from "@playwright/test";

const API = "http://localhost:8765/api";
const headers = { Authorization: "Bearer changeme" };

for (const width of [1280, 390]) {
  test.describe(`Wiki composer at ${width}px`, () => {
    test.use({
      viewport: { width, height: 844 },
      isMobile: width < 768,
      hasTouch: width < 768,
    });
    test("selection is visible, accessible and cancellable", async ({ page, request }) => {
      // Observe the real client payload without starting a model in a UI test.
      // The server's cancellation boundary is covered in test_wiki_composer.py.
      const sent: Record<string, unknown>[] = [];
      await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => {
        const server = socket.connectToServer();
        socket.onMessage((message) => {
          const data = JSON.parse(String(message));
          if (data.type === "send_message") sent.push(data);
          else server.send(message);
        });
      });
      const created = await request.post(`${API}/sessions`, {
        headers, data: { name: `Wiki Composer ${width}`, working_dir: "/tmp" },
      });
      expect(created.ok()).toBeTruthy();
      const session = await created.json();
      try {
        await page.goto("/");
        await page.locator('input[type="password"]').fill("changeme");
        await page.locator(".btn-login").click();
        if (width < 768) {
          await page.getByRole("button", { name: "Toggle sidebar" }).click();
          await expect(page.locator(".sidebar")).toHaveClass(/open/);
        }
        await expect(page.locator(".agent-list-header")).toBeVisible();
        const agent = page.locator(".agent-item", { hasText: "Octo" });
        await agent.click();
        await page.locator(".session-item", { hasText: session.name }).click();
        await expect(page.locator(".conn-status.on")).toBeVisible();
        const wiki = page.getByRole("button", { name: "Use local Wiki for this message" });
        const input = page.locator(".chat-input-bar textarea");
        const activate = () => width < 768 ? wiki.tap() : wiki.click();
        await expect(wiki).toBeVisible();
        const initialColor = await wiki.evaluate((el) => getComputedStyle(el).backgroundColor);
        await activate();
        await expect(wiki).toHaveAttribute("aria-pressed", "true");
        await expect(wiki).toHaveText("Wiki · 已启用");
        await expect(input).toBeFocused();
        await expect.poll(() => wiki.evaluate((el) => getComputedStyle(el).backgroundColor))
          .not.toBe(initialColor);
        await activate();
        await expect(wiki).toHaveAttribute("aria-pressed", "false");
        if (width >= 768) {
          await input.press("Tab");
          await expect(wiki).toBeFocused();
          await expect(wiki).not.toHaveCSS("box-shadow", "none");
          await wiki.press("Space");
          await expect(wiki).toHaveAttribute("aria-pressed", "true");
          await expect(input).toBeFocused();
          await input.press("Tab");
          await wiki.press("Enter");
          await expect(wiki).toHaveAttribute("aria-pressed", "false");
        }
        await input.fill("/wiki maintain Library/a.md");
        // Explicit cancellation persists until the person re-enables Wiki.
        await expect(wiki).toHaveAttribute("aria-pressed", "false");
        await activate();
        await expect(wiki).toHaveText("Wiki · 整理");
        await activate();
        await expect(input).toHaveValue("/wiki maintain Library/a.md");
        await expect(wiki).toHaveAttribute("aria-pressed", "false");
        const box = await wiki.boundingBox();
        expect(box).not.toBeNull();
        expect(box!.x).toBeGreaterThanOrEqual(0);
        expect(box!.x + box!.width).toBeLessThanOrEqual(width);
        const nested = "/wiki /wiki maintain 的用法是什么？";
        await input.fill(nested);
        await expect(wiki).toHaveAttribute("aria-pressed", "false");
        await page.getByRole("button", { name: "Send message" }).click();
        await expect.poll(() => sent).toEqual([expect.objectContaining({
          type: "send_message", session_id: session.id,
          content: nested, wiki_mode: "off",
        })]);
        await expect(input).toHaveValue("");
        await expect(wiki).toHaveAttribute("aria-pressed", "false");
      } finally {
        await request.delete(`${API}/sessions/${session.id}`, { headers }).catch(() => {});
      }
    });
  });
}

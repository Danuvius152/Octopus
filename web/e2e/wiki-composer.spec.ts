import { accountToken, enterAccountCredentials } from "./current-account";
import { expect, test } from "./test";
import { systemAgentId } from "./system-agent";

const API = "http://localhost:8765/api";
const headers = { get Authorization() { return `Bearer ${accountToken()}`; } };

for (const width of [1280, 390]) {
  test.describe(`Shared Wiki composer at ${width}px`, () => {
    test.use({
      viewport: { width, height: 844 },
      isMobile: width < 768,
      hasTouch: width < 768,
    });
    test("ordinary lookup and legacy commands need no Wiki switch", async ({ page, request }) => {
      // Observe browser payloads without starting a model in this UI test.
      // Backend tests cover default lookup and explicit maintenance scope.
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
        headers, data: { agent_id: await systemAgentId(request, `${API}/sessions`, headers), name: `Shared Wiki Composer ${width}`, },
      });
      expect(created.ok()).toBeTruthy();
      const session = await created.json();
      try {
        await page.goto("/");
        await enterAccountCredentials(page);
        if (width < 768) {
          await page.getByRole("button", { name: "Toggle sidebar" }).click();
          await expect(page.locator(".sidebar")).toHaveClass(/open/);
        }
        await expect(page.locator(".agent-list-header")).toBeVisible();
        await page.locator(".agent-item", { hasText: "Octo" }).click();
        await page.locator(".session-item", { hasText: session.name }).click();
        await expect(page.locator(".conn-status.on")).toBeVisible();
        const input = page.locator(".chat-input-bar textarea");
        await expect(page.getByRole("button", { name: "Use local Wiki for this message" })).toHaveCount(0);
        await expect(page.getByRole("button", { name: "Attach file" })).toBeVisible();
        const messages = [
          "请结合共享知识解释这个问题",
          "/wiki 查询相关文章",
          "/wiki maintain Library/a.md",
          "/wiki /wiki maintain 的用法是什么？",
        ];
        for (const [index, message] of messages.entries()) {
          await input.fill(message);
          await page.getByRole("button", { name: "Send message" }).click();
          await expect.poll(() => sent.length).toBe(index + 1);
          expect(sent[index]).toEqual(expect.objectContaining({
            type: "send_message", session_id: session.id, content: message,
          }));
          expect(sent[index]).not.toHaveProperty("wiki_mode");
          await expect(input).toHaveValue("");
        }
      } finally {
        await request.delete(`${API}/sessions/${session.id}`, { headers }).catch(() => {});
      }
    });
  });
}

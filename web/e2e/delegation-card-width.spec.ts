import { expect, test } from "./test";
import { enterAccountCredentials } from "./current-account";
import type { Message } from "../src/stores/sessionStore";

for (const width of [390, 1280]) {
  test(`delegation requests keep long paths inside the chat at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const session = { id: "card-width", agent_id: "card-agent", name: "Card width", backend: "codex",
      status: "idle", archived: false, working_dir: "/tmp", created_at: "2026-11-12T00:00:00Z" };
    const path = `/home/jack/${"long-directory-name/".repeat(30)}${"x".repeat(240)}.md`;
    const request = `Review this file: ${path}`;
    const messages: Message[] = [];
    for (const continued of [false, true]) {
      const call = continued ? "continued" : "asked";
      const metadata = { request_id: call, attempt_id: call, tool_call_id: call };
      messages.push({ role: "assistant", type: "tool_use", seq: messages.length + 1,
        tool_name: "mcp__ask_agent__ask", tool_use_id: call, runtime_metadata: metadata,
        tool_input: { ...(continued ? { delegation_id: "child" } : { name: "Vera" }), request, files: [path] } });
      messages.push({ role: "tool", type: "tool_result", seq: messages.length + 1, tool_use_id: call,
        content: "Delegation accepted", runtime_metadata: { ...metadata,
          receipt: { kind: "delegation", delegation_id: "child", work_id: call } } });
    }
    const rounds = ["asked", "continued"].map((work_id) => ({ delegation_id: "child", work_id,
      parent_session_id: session.id, target_agent_name: "Vera", target_agent_id: "vera", request,
      state: "running", created_at: session.created_at, finished_at: null, error: null }));
    await page.route("**/api/**", async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (!pathname.startsWith("/api/")) { await route.continue(); return; }
      let data: unknown = [];
      if (pathname === "/api/auth/state") data = { accounts_exist: true };
      else if (pathname === "/api/auth/login") data = { token: "card-token" };
      else if (pathname === "/api/auth/identity") data = { user_id: "card-user", label: "Reader", is_admin: false };
      else if (pathname === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend: "codex", pinned: true, is_system: true }];
      else if (pathname === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
      else if (pathname === "/api/sessions") data = [session];
      else if (pathname === `/api/sessions/${session.id}/delegations`) data = rounds;
      else if (pathname === `/api/sessions/${session.id}`) data = { ...session, messages, delegations: rounds,
        next_message_seq: messages.length + 1, oldest_loaded_seq: 1, has_more_messages: false,
        pending_questions: [], subagents: [], active_attempt: null,
        dispatch_state: { generation: "card-service", revision: 1, paused: false,
          active_request_id: null, active_operation: null, items: [] } };
      await route.fulfill({ json: data });
    });
    await page.routeWebSocket("**/ws?*", () => {});
    await page.goto("/");
    await enterAccountCredentials(page);
    if (width < 768) await page.getByRole("button", { name: "Toggle sidebar", exact: true }).click();
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await page.locator(".session-item", { hasText: session.name }).click();
    const cards = page.locator(".agent-delegation-request");
    await expect(cards).toHaveCount(2);
    await expect(cards.nth(0)).toContainText("Asked Vera");
    await expect(cards.nth(1)).toContainText("Continued Vera");
    for (const card of await cards.all()) {
      await card.scrollIntoViewIfNeeded();
      const geometry = await card.evaluate((element) => {
        const card = element.getBoundingClientRect();
        const tool = element.parentElement!.querySelector(".msg-tool")!.getBoundingClientRect();
        return { left: card.left - tool.left, right: card.right - tool.right,
          overflow: element.scrollWidth - element.clientWidth };
      });
      expect.soft(geometry.left).toBeGreaterThanOrEqual(-1);
      expect.soft(geometry.right).toBeLessThanOrEqual(1);
      expect.soft(geometry.overflow).toBeLessThanOrEqual(1);
      await expect(card.getByTitle("Cancel delegation")).toBeVisible();
      await expect(card.getByTitle("Open Vera's session")).toBeVisible();
      await expect(card.getByText(`files: ${path}`, { exact: true })).toBeVisible();
    }
    const scroller = page.locator(".chat-messages");
    expect(await scroller.evaluate((element) => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
  });
}

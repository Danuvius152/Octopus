import { expect, test } from "./test";
import { enterAccountCredentials } from "./current-account";

test("hidden reasoning leaves no virtual-list spacing after history reload", async ({ page }) => {
  const session = { id: "spacing-session", agent_id: "spacing-agent", name: "Message spacing",
    backend: "codex", status: "idle", archived: false, working_dir: "/tmp", created_at: "2026-11-02T00:00:00Z" };
  let hiddenCount = 0;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) { await route.continue(); return; }
    let data: unknown = [];
    if (path === "/api/auth/state") data = { accounts_exist: true };
    else if (path === "/api/auth/login") data = { token: "spacing-token" };
    else if (path === "/api/auth/identity") data = { user_id: "spacing-user", label: "Reader", is_admin: false };
    else if (path === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend: "codex", pinned: true, is_system: true }];
    else if (path === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
    else if (path === "/api/sessions") data = [session];
    else if (path === `/api/sessions/${session.id}`) data = { ...session,
      messages: [
        { role: "assistant", type: "text", seq: 1, content: "Before reasoning" },
        ...Array.from({ length: hiddenCount }, (_, index) => ({ role: "assistant", type: "thinking", seq: index + 2, content: "" })),
        { role: "assistant", type: "text", seq: hiddenCount + 2, content: "After reasoning" },
      ],
      next_message_seq: hiddenCount + 3, oldest_loaded_seq: 1, has_more_messages: false,
      pending_questions: [], subagents: [], active_attempt: null,
      dispatch_state: { generation: "spacing-service", revision: 1, paused: false, active_request_id: null, active_operation: null, items: [] },
    };
    await route.fulfill({ json: data });
  });
  await page.routeWebSocket("**/ws?*", () => {});
  const open = async () => {
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await page.locator(".session-item", { hasText: session.name }).click();
    await expect(page.getByText("After reasoning", { exact: true })).toBeVisible();
  };
  const gap = async () => {
    const before = await page.locator(".msg-row", { hasText: "Before reasoning" }).boundingBox();
    const after = await page.locator(".msg-row", { hasText: "After reasoning" }).boundingBox();
    return after!.y - before!.y - before!.height;
  };
  await page.goto("/");
  await enterAccountCredentials(page);
  await open();
  const baseline = await gap();
  hiddenCount = 7;
  await page.reload();
  await open();
  await expect.poll(gap).toBeLessThanOrEqual(baseline + 1);
  await expect(page.locator(".chat-messages [data-item-index]")).toHaveCount(2);
});

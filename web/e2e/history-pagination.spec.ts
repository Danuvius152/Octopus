import { expect, test, type WebSocketRoute } from "./test";
import type { Route } from "@playwright/test";
import { enterAccountCredentials } from "./current-account";
import type { Message } from "../src/stores/sessionStore";
import type { RuntimeMetadata } from "../src/api";

for (const childOnly of [false, true]) {
test(`history prepend keeps the visible row and offset across ${childOnly ? "child-only" : "mixed"} regrouping and live tail appends`, async ({ page }) => {
  const session = { id: "history-window", agent_id: "history-agent", name: "Reading history", backend: "codex",
    status: "idle", archived: false, working_dir: "/tmp", created_at: "2026-11-02T00:00:00Z" };
  const identity = (thread: string): RuntimeMetadata => ({ request_id: "request", attempt_id: "attempt", native: {
    thread_id: thread, turn_id: "turn", item_id: "item", root_thread_id: "root", root_turn_id: "turn",
  } });
  const row = (seq: number): Message => ({ role: "assistant", type: "text", seq, content: `History row ${seq}` });
  const messages = Array.from({ length: 61 }, (_, index) => row(index + 200));
  messages[1] = { ...row(201), runtime_metadata: identity("helper"), content: "Saved child body" };
  const helper = { card_id: "helper-card", task_id: "helper", native_thread_id: "helper", native_turn_id: "turn",
    runtime_metadata: identity("helper"), status: "running", name: "Helper", description: "Reading", prompt: "", summary: "", steps: [] };
  const pages: Route[] = [];
  let socket!: WebSocketRoute;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith("/api/")) { await route.continue(); return; }
    let data: unknown = [];
    if (path === "/api/auth/state") data = { accounts_exist: true };
    else if (path === "/api/auth/login") data = { token: "history-token" };
    else if (path === "/api/auth/identity") data = { user_id: "history-user", label: "Reader", is_admin: false };
    else if (path === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend: "codex", pinned: true, is_system: true }];
    else if (path === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
    else if (path === "/api/sessions") data = [session];
    else if (path === `/api/sessions/${session.id}/messages`) {
      expect(url.searchParams.get("before_seq")).toBe("200");
      pages.push(route);
      return;
    } else if (path === `/api/sessions/${session.id}`) data = { ...session,
      messages, next_message_seq: 261, oldest_loaded_seq: 200, has_more_messages: true, pending_questions: [], subagents: [helper],
      dispatch_state: { generation: "history-service", revision: 1, paused: false, active_request_id: null, active_operation: null, items: [] },
    };
    await route.fulfill({ json: data });
  });
  await page.routeWebSocket("**/ws?*", (ws) => { socket = ws; });
  await page.goto("/");
  await enterAccountCredentials(page);
  await page.locator(".agent-item", { hasText: "Octo" }).click();
  await page.locator(".session-item", { hasText: session.name }).click();
  const scroller = page.locator(".chat-messages");
  await expect(scroller.getByText("History row 260", { exact: true })).toBeVisible();
  // Reach history through native navigation, which carries reading intent;
  // layout-driven position changes deliberately do not retire following.
  await scroller.press("Home");
  await expect.poll(() => pages.length).toBe(1);
  const anchor = scroller.getByText("History row 200", { exact: true });
  await expect(anchor).toBeVisible();
  const offset = async () => (await anchor.boundingBox())!.y - (await scroller.boundingBox())!.y;
  const before = await offset();
  for (const seq of [261, 262]) socket.send(JSON.stringify({ ...row(seq), type: "assistant_text", session_id: session.id }));
  await pages[0].fulfill({ json: {
    messages: childOnly ? Array.from({ length: 4 }, (_, index) => ({ ...row(index + 196), runtime_metadata: identity("helper") })) :
    [{ role: "system", type: "native_subagent", seq: 196,
      runtime_metadata: identity("helper"), tool_input: { ...helper, status: "completed" } },
    ...Array.from({ length: 3 }, (_, index) => row(index + 197))],
    oldest_loaded_seq: 196, has_more_messages: false,
  } });
  await expect(scroller.getByText("Loading earlier messages…")).toHaveCount(0);
  await expect(anchor).toBeVisible();
  await expect.poll(async () => Math.abs(await offset() - before)).toBeLessThanOrEqual(2);
  await expect(scroller.locator(".subagent-card")).toHaveCount(1);
  expect(pages).toHaveLength(1);
});
}

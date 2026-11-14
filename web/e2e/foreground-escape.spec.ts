import { enterAccountCredentials } from "./current-account";
import { expect, test } from "./test";

test("Escape closes foreground controls without stopping the chat until an unconsumed chat Escape", async ({ page }) => {
  const session = { id: "escape-browser", agent_id: "escape-agent", name: "Running escape chat", backend: "codex",
    working_dir: "/tmp", created_at: "2026-11-14T00:00:00Z", status: "running", archived: false };
  const dispatch = { generation: "escape-service", revision: 1, paused: false, active_request_id: "escape-request",
    active_operation: "message", items: [] };
  const application = { id: "escape-app", name: "Escape App", description: "", icon: "⌨️", agent_id: session.agent_id,
    session_id: null, app_dir: "/tmp/escape-app", entrypoint: "index.html", status: "ready", error: null, archived: false,
    pinned: true, pin_order: 1, created_at: "2026-11-14T00:00:00Z", updated_at: "2026-11-14T00:00:00Z",
    last_built_at: null, backend: { state: "running", port: 8080 } };
  const commands: Record<string, unknown>[] = [];
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) { await route.fallback(); return; }
    let data: unknown = [];
    if (path === "/api/auth/state") data = { accounts_exist: true };
    else if (path === "/api/auth/login") data = { token: "escape-token" };
    else if (path === "/api/auth/identity") data = { user_id: "escape-owner", label: "Owner", is_admin: true };
    else if (path === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend: "codex", is_system: true, pinned: true, archived: false }];
    else if (path === "/api/applications") data = [application];
    else if (path === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
    else if (path === "/api/sessions") data = [session];
    else if (path === `/api/sessions/${session.id}`) data = { ...session, messages: [], next_message_seq: 0,
      oldest_loaded_seq: 0, has_more_messages: false, pending_questions: [], subagents: [], dispatch_state: dispatch,
      active_attempt: { request_id: "escape-request", attempt_id: "escape-attempt" } };
    else if (path.endsWith("/submissions")) data = { items: [], next_cursor: null };
    await route.fulfill({ json: data });
  });
  await page.route("**/apps/escape-app/**", route => route.fulfill({ contentType: "text/html", body: "<p>Keyboard fixture</p>" }));
  await page.routeWebSocket("**/ws?*", ws => { ws.onMessage(raw => commands.push(JSON.parse(String(raw)))); });
  await page.goto("/");
  await enterAccountCredentials(page);
  await page.locator(".agent-item", { hasText: "Octo" }).click();
  await page.locator(".session-item", { hasText: session.name }).click();
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeVisible();
  const input = page.locator(".chat-input-bar textarea");
  await input.fill("Preserved draft 中文");

  await page.getByRole("button", { name: "Context remaining:", exact: false }).click();
  await expect(page.getByRole("dialog", { name: "Context details" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(commands).toEqual([]);

  // A Radix menu and the inline slash menu also consume their closing Escape.
  await page.getByRole("button", { name: "Change this session's credential" }).click();
  await expect(page.getByRole("menu")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
  expect(commands).toEqual([]);
  await input.fill("/mo");
  await expect(page.locator(".slash-menu")).toBeVisible();
  await input.press("Escape");
  await expect(page.locator(".slash-menu")).toHaveCount(0);
  expect(commands).toEqual([]);
  await input.fill("Preserved draft 中文");
  await input.press("Escape");
  await expect.poll(() => commands.length).toBe(1);
  expect(commands[0]).toMatchObject({ type: "interrupt", session_id: session.id, request_id: "escape-request", attempt_id: "escape-attempt" });
  await expect(input).toHaveValue("Preserved draft 中文");

  await page.locator(".application-item", { hasText: "Escape App" }).click();
  const backend = page.getByTitle("Backend process"), iterate = page.locator(".btn-application-iterate");
  await backend.focus();
  await page.keyboard.press("Enter");
  await iterate.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog")).toHaveCount(2);
  await page.locator(".application-request-input").focus();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Ask Octo for a change" })).toHaveCount(0);
  await expect(page.getByRole("dialog", { name: "Backend", exact: true })).toBeVisible();
  await backend.focus();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await iterate.focus();
  await page.keyboard.press("Enter");
  await page.getByLabel("Reload the app").focus();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(commands).toHaveLength(1);
});

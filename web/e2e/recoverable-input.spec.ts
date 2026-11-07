import { expect, test } from "./test";
import { enterAccountCredentials } from "./current-account";

/** Browser recovery contract; real SQLite reopen is covered in backend tests. */
test("saved input survives a page reload and only executes after an explicit new send", async ({ page }) => {
  const session = { id: "recovery-browser", agent_id: "recovery-agent", name: "Recovery conversation", backend: "codex",
    working_dir: "/tmp", created_at: "2026-10-20T00:00:00Z", archived: false, status: "idle" };
  const execution = { state: "withdrawn", state_version: 2, execution_owner_request_id: "old-request", recoverable_input: true,
    reason_code: "service_restart_before_start" };
  const summary = { client_request_id: "old-client", request_id: "old-request", execution, preview: "Saved input after restart", attachment_count: 0 };
  const frames: Record<string, unknown>[] = [];
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) { await route.continue(); return; }
    let data: unknown = [];
    if (path === "/api/auth/state") data = { accounts_exist: true };
    else if (path === "/api/auth/login") data = { token: "browser-token" };
    else if (path === "/api/auth/identity") data = { user_id: "browser-user", label: "Operator", is_admin: false };
    else if (path === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend: "codex", pinned: true, is_system: true }];
    else if (path === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
    else if (path === "/api/sessions") data = [session];
    else if (path === `/api/sessions/${session.id}`) data = { ...session, messages: [], pending_questions: [], subagents: [],
      next_message_seq: 0, has_more_messages: false, dispatch_state: { generation: "restarted-service", revision: 0, paused: false, active_request_id: null, active_operation: null, items: [] } };
    else if (path === `/api/sessions/${session.id}/submissions`) data = { items: frames.length ? [] : [summary], next_cursor: null };
    else if (path === `/api/sessions/${session.id}/submissions/old-client`) data = { session_id: session.id, ...summary, accepted: true,
      input: { content: "Saved input after restart 中文", attachment_ids: [], attachments: [], wiki_mode: null } };
    await route.fulfill({ json: data });
  });
  await page.routeWebSocket("**/ws?*", (socket) => socket.onMessage((raw) => {
    const data = JSON.parse(String(raw));
    if (data.type !== "send_message") return;
    frames.push(data);
    socket.send(JSON.stringify({ type: "submission", session_id: session.id, client_request_id: data.client_request_id,
      request_id: "new-request", accepted: true, execution: { ...execution, state: "queued", recoverable_input: false } }));
  }));
  const open = async () => {
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await page.locator(".session-item", { hasText: session.name }).click();
    await expect(page.getByText("Not executed · input saved")).toBeVisible();
  };
  await page.goto("/");
  await enterAccountCredentials(page);
  await open();
  await page.reload();
  await open();
  expect(frames).toHaveLength(0);
  await page.getByRole("button", { name: "Review saved input" }).click();
  await expect(page.getByText("Saved input after restart 中文", { exact: true })).toBeVisible();
  expect(frames).toHaveLength(0);
  await page.getByRole("button", { name: "Restore to input" }).click();
  const input = page.locator(".chat-input-bar textarea");
  await expect(input).toHaveValue("Saved input after restart 中文");
  expect(frames).toHaveLength(0);
  await input.fill("Reviewed and edited before sending");
  await expect(page.getByRole("button", { name: "Restore to input" })).toBeDisabled();
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await expect.poll(() => frames.length).toBe(1);
  expect(frames[0]).toMatchObject({ content: "Reviewed and edited before sending", recovery_of_client_id: "old-client" });
  expect(frames[0].client_request_id).not.toBe("old-client");
});

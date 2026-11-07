import { expect, test, type WebSocketRoute } from "./test";
import { enterAccountCredentials } from "./current-account";
import type { Message } from "../src/stores/sessionStore";

test("native failures, hook provenance and original image paths survive WS and history reload", async ({ page, context }) => {
  const session = { id: "native-items", agent_id: "native-agent", name: "Native items", backend: "codex",
    status: "idle", archived: false, working_dir: "/working", created_at: "2026-11-02T00:00:00Z" };
  const messages: Message[] = [];
  const path = "/working/native.png";
  let socket: WebSocketRoute;
  let missing = false;
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.route("**/api/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (!pathname.startsWith("/api/")) { await route.continue(); return; }
    let data: unknown = [];
    if (pathname === "/api/auth/state") data = { accounts_exist: true };
    else if (pathname === "/api/auth/login") data = { token: "browser-token" };
    else if (pathname === "/api/auth/identity") data = { user_id: "browser-user", label: "Operator", is_admin: false };
    else if (pathname === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend: "codex", pinned: true, is_system: true }];
    else if (pathname === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
    else if (pathname === "/api/sessions") data = [session];
    else if (pathname === `/api/sessions/${session.id}/submissions`) data = { items: [] };
    else if (pathname === `/api/sessions/${session.id}/files/meta`) {
      expect(new URL(route.request().url()).searchParams.get("path")).toBe(path);
      if (missing) { await route.fulfill({ status: 404, json: { detail: "Original file no longer exists" } }); return; }
      data = { path, kind: "image", mime_type: "image/png", size: 68 };
    } else if (pathname === `/api/sessions/${session.id}/files`) {
      await route.fulfill({ contentType: "image/png", body: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a/r8AAAAASUVORK5CYII=", "base64") });
      return;
    } else if (pathname === `/api/sessions/${session.id}`) data = { ...session, messages,
      next_message_seq: messages.length + 1, oldest_loaded_seq: messages[0]?.seq ?? null, has_more_messages: false,
      pending_questions: [], subagents: [], dispatch_state: {
        generation: "native-items-service", revision: 1, paused: false, active_request_id: null, active_operation: null, items: [] } };
    await route.fulfill({ json: data });
  });
  await page.routeWebSocket("**/ws?*", (ws) => { socket = ws; });
  const open = async () => {
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await page.locator(".session-item", { hasText: session.name }).click();
    await expect(page.locator(".conn-status.on")).toBeVisible();
  };
  await page.goto("/");
  await enterAccountCredentials(page);
  await open();
  const outputs = [
    { type: "mcpToolCall", status: "failed", content: "Business failure body", is_error: true },
    { type: "commandExecution", status: "declined", content: "Command declined", is_error: true },
    { type: "hookPrompt", fragments: [{ text: "Hook context", hookRunId: "hook-source" }], content: "Hook context · hook-source" },
    { type: "imageGeneration", status: "completed", savedPath: path, content: `Generated image saved at: ${path}` },
    { type: "imageGeneration", status: "completed", content: "Native image generation completed; local file unavailable." },
    { type: "imageGeneration", status: "failed", content: "Native image generation failed", is_error: true },
  ];
  for (const [index, output] of outputs.entries()) {
    const { content, is_error, ...nativeItem } = output;
    const message: Message = { seq: index + 1, role: "tool", type: "tool_result", content, is_error: !!is_error,
      tool_input: { native_item: nativeItem }, runtime_metadata: { request_id: "request", attempt_id: "attempt", native: {
        thread_id: "root", turn_id: "turn", item_id: `item-${index}`, root_thread_id: "root", root_turn_id: "turn" } } };
    messages.push(message);
    socket!.send(JSON.stringify({ ...message, type: "tool_result", session_id: session.id, output: content }));
  }
  const verify = async () => {
    await expect(page.locator(".msg-tool-result.error")).toHaveCount(3);
    await expect(page.getByRole("button", { name: /Hook prompt/ })).toBeVisible();
    await expect(page.getByText(/local file unavailable/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Copy path" })).toHaveCount(1);
  };
  await verify();
  await page.getByRole("button", { name: "Copy path" }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(path);
  await page.getByRole("button", { name: "Open in viewer" }).click();
  const image = page.getByRole("dialog").getByRole("img", { name: path, exact: true });
  await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(1);
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await page.reload();
  await open();
  await verify();
  missing = true;
  await page.getByRole("button", { name: "Open in viewer" }).click();
  await expect(page.getByRole("dialog").getByText("Original file no longer exists")).toBeVisible();
  await expect(page.getByRole("dialog").getByRole("img", { name: path, exact: true })).toHaveCount(0);
});

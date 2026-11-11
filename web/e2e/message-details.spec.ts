import { enterAccountCredentials } from "./current-account";
import { expect, test, type Page, type WebSocketRoute } from "./test";

async function openReadabilityMessages(page: Page, mobile: boolean) {
  const session = { id: "readability", agent_id: "reader", name: "Readable message", backend: "codex",
    status: "stopped", archived: false, working_dir: "/working", created_at: new Date().toISOString() };
  const messages = [
    { role: "assistant", type: "text", seq: 1, content: "Long first line of saved message text. ".repeat(10),
      body_state: "preview", detail_ref: "/api/sessions/readability/messages/1", created_at: session.created_at },
  ];
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) { await route.continue(); return; }
    let data: unknown = [];
    if (path === "/api/auth/state") data = { accounts_exist: true };
    else if (path === "/api/auth/login") data = { token: "reader-token" };
    else if (path === "/api/auth/identity") data = { user_id: "reader-user", label: "Reader", is_admin: false };
    else if (path === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend: "codex", pinned: true, is_system: true }];
    else if (path === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
    else if (path === "/api/sessions") data = [session];
    else if (path === `/api/sessions/${session.id}/submissions`) data = { items: [] };
    else if (path === `/api/sessions/${session.id}`) data = { ...session, messages, next_message_seq: 2,
      oldest_loaded_seq: 1, has_more_messages: false, pending_questions: [], subagents: [],
      dispatch_state: { generation: "readability-service", revision: 1, paused: false,
        active_request_id: null, active_operation: null, items: [] } };
    await route.fulfill({ json: data });
  });
  await page.routeWebSocket("**/ws?*", () => {});
  await page.goto("/");
  await enterAccountCredentials(page);
  if (mobile) await page.getByRole("button", { name: "Toggle sidebar" }).click();
  await page.locator(".agent-item", { hasText: "Octo" }).click();
  await page.locator(".session-item", { hasText: session.name }).click();
  await expect(page.locator(".msg-assistant")).toBeVisible();
}

for (const mobile of [false, true]) {
  test.describe(mobile ? "touch reading" : "desktop reading", () => {
    test.use({ viewport: mobile ? { width: 390, height: 640 } : { width: 1280, height: 800 }, hasTouch: mobile });

    test("full-message control leaves long text and the timestamp unobstructed", async ({ page }) => {
      await openReadabilityMessages(page, mobile);
      const button = page.getByRole("button", { name: "View full message", exact: true });
      await button.scrollIntoViewIfNeeded();
      if (!mobile) await button.hover();
      await expect(button).toHaveCSS("opacity", "1");
      const overlaps = await button.evaluate((element) => {
        const buttonRect = element.getBoundingClientRect();
        const row = element.closest(".msg-row")!;
        const range = document.createRange();
        range.selectNodeContents(row.querySelector(".msg-content p")!);
        const intersects = (rect: DOMRect) => rect.left < buttonRect.right && rect.right > buttonRect.left &&
          rect.top < buttonRect.bottom && rect.bottom > buttonRect.top;
        return { text: [...range.getClientRects()].some(intersects),
          time: intersects(row.querySelector("time")!.getBoundingClientRect()) };
      });
      expect(overlaps).toEqual({ text: false, time: false });
      if (!mobile) {
        await page.mouse.move(0, 0);
        await button.focus();
        await page.keyboard.press("Shift+Tab");
        await page.keyboard.press("Tab");
        await expect(button).toBeFocused();
        await expect(button).toHaveCSS("opacity", "1");
      }
    });
  });
}

test("saved previews retry full reads and keep an outside-window question answerable after reconnect", async ({ page }) => {
  const session = { id: "bounded-browser", agent_id: "bounded-agent", name: "Saved previews", backend: "codex",
    status: "running", archived: false, working_dir: "/working", created_at: "2026-11-10T00:00:00Z" };
  const question = { question_id: "original-question", message_seq: 1, questions: [], request_id: "request", attempt_id: "attempt",
    interaction_policy: "human", wait_mode: "optional", snoozed: false, body_state: "preview",
    deadline: new Date(Date.now() + 90_000).toISOString() as string | null,
    detail_ref: `/api/sessions/${session.id}/messages/1` };
  const preview = { role: "assistant", type: "text", seq: 100, content: "Saved answer preview…", body_state: "preview",
    detail_ref: `/api/sessions/${session.id}/messages/100` };
  const full = { role: "assistant", type: "text", seq: 100, content: `Full original answer\n\n${"完整原文 ".repeat(20_000)}\n\nComplete answer end.` };
  const tool = { role: "tool", type: "tool_result", seq: 99, content: "Saved tool result…", body_state: "preview",
    detail_ref: `/api/sessions/${session.id}/messages/99` };
  const bash = { role: "assistant", type: "tool_use", seq: 98, tool_name: "Bash", body_state: "preview",
    tool_input: { command: "Saved command…" }, detail_ref: `/api/sessions/${session.id}/messages/98` };
  const mcp = { role: "assistant", type: "tool_use", seq: 97, tool_name: "mcp__wiki__read", body_state: "preview",
    tool_input: { path: "/original" }, detail_ref: `/api/sessions/${session.id}/messages/97` };
  const nativeRuntime = (item_id: string) => ({ request_id: "request", attempt_id: "attempt", native: {
    thread_id: "root", turn_id: "turn", root_thread_id: "root", root_turn_id: "turn", item_id } });
  const sockets: WebSocketRoute[] = [];
  const sent: Record<string, unknown>[] = [];
  let bodyReads = 0;
  let toolReads = 0;
  let bashReads = 0;
  let mcpReads = 0;
  let snapshots = 0;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith("/api/")) { await route.continue(); return; }
    let data: unknown = [];
    if (path === "/api/auth/state") data = { accounts_exist: true };
    else if (path === "/api/auth/login") data = { token: "bounded-token" };
    else if (path === "/api/auth/identity") data = { user_id: "bounded-user", label: "Reader", is_admin: false };
    else if (path === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend: "codex", pinned: true, is_system: true }];
    else if (path === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
    else if (path === "/api/sessions") data = [session];
    else if (path === `/api/sessions/${session.id}/submissions`) data = { items: [] };
    else if (path === preview.detail_ref) {
      bodyReads++;
      if (bodyReads === 1) { await route.fulfill({ status: 503, body: "Read unavailable" }); return; }
      data = full;
    } else if (path === tool.detail_ref) {
      toolReads++;
      data = { ...tool, content: "Complete saved tool result", body_state: "inline" };
    } else if (path === bash.detail_ref) {
      bashReads++;
      data = { ...bash, runtime_metadata: nativeRuntime("original-command"), tool_input: { command: "printf 'Complete saved command'", native_item: {
        type: "commandExecution", id: "original-command", command: "printf 'Complete saved command'", cwd: "/working",
        status: "inProgress", exitCode: null, commandActions: [{ type: "unknown", command: "printf 'Complete saved command'" }] } } };
    } else if (path === mcp.detail_ref) {
      mcpReads++;
      data = { ...mcp, runtime_metadata: nativeRuntime("original-tool"), tool_input: { path: "/original", native_item: {
        type: "mcpToolCall", id: "original-tool", arguments: { path: "/original" }, status: "inProgress" } } };
    } else if (path === question.detail_ref) data = { role: "assistant", type: "question_request", seq: 1,
      tool_use_id: question.question_id, tool_input: { request_id: "request", attempt_id: "attempt",
        questions: [{ question: "Choose the original option", options: [{ label: "Original option" }] }] } };
    else if (path === `/api/sessions/${session.id}`) {
      expect(url.searchParams.get("view")).toBe("browser"); snapshots++;
      const pause = { role: "system", type: "question_snoozed", seq: 101, tool_use_id: question.question_id,
        tool_input: { request_id: "request", attempt_id: "attempt", deadline: null, snoozed: true } };
      data = { ...session, messages: [mcp, bash, tool, preview, ...(question.snoozed ? [pause] : [])], next_message_seq: question.snoozed ? 102 : 101,
        oldest_loaded_seq: 97, has_more_messages: false,
        active_attempt: { request_id: "request", attempt_id: "attempt" }, pending_questions: [question], subagents: [],
        dispatch_state: { generation: "bounded-service", revision: 1, paused: false, active_request_id: "request", active_operation: "message", items: [] } };
    }
    await route.fulfill({ json: data });
  });
  await page.routeWebSocket("**/ws?*", (socket) => {
    sockets.push(socket);
    socket.onMessage((raw) => {
      const command = JSON.parse(String(raw)); sent.push(command);
      socket.send(JSON.stringify({ type: "command_receipt", session_id: session.id, operation: command.type,
        client_command_id: command.client_command_id, accepted: true }));
      if (command.type === "snooze_question") {
        question.snoozed = true; question.deadline = null;
        socket.send(JSON.stringify({ type: "question_snoozed", session_id: session.id, seq: 101,
          question_id: question.question_id, request_id: "request", attempt_id: "attempt", deadline: null, snoozed: true }));
      }
    });
  });
  await page.goto("/");
  await enterAccountCredentials(page);
  await page.locator(".agent-item", { hasText: "Octo" }).click();
  await page.locator(".session-item", { hasText: session.name }).click();
  await expect(page.getByText(preview.content, { exact: true })).toBeVisible();
  expect(bodyReads).toBe(0);
  expect(toolReads).toBe(0);
  expect(bashReads).toBe(0);
  expect(mcpReads).toBe(0);
  await expect(page.getByText("Preview · some content is not shown", { exact: true })).toHaveCount(0);
  for (const [name, expected] of [["Bash", "printf 'Complete saved command'"],
    ["mcp__wiki__read", JSON.stringify({ path: "/original" }, null, 2)]]) {
    const card = page.locator(".msg-tool").filter({ has: page.locator(".tool-name", { hasText: name }) });
    await card.locator(".tool-header").click();
    await expect(card.locator(".tool-detail")).toHaveText(expected);
    await expect(card.getByText("Raw tool data", { exact: true })).toHaveCount(0);
    await expect(card.getByLabel("Raw tool input")).toHaveCount(0);
    await card.locator(".tool-header").click();
  }
  expect(bashReads).toBe(1);
  expect(mcpReads).toBe(1);
  await page.locator(".msg-tool-result .tool-header").click();
  await expect(page.getByText("Complete saved tool result", { exact: true })).toBeVisible();
  expect(toolReads).toBe(1);
  const form = page.locator(".msg-question:not(.msg-question-done)");
  await expect(form.getByRole("button", { name: "Submit", exact: true })).toBeDisabled();
  await form.locator(".question-header").click();
  await expect(form.getByText("Automatic expiry paused · waiting for your answer")).toBeVisible();
  await page.getByRole("button", { name: "View full message", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("503");
  await expect(page.getByText(preview.content, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Retry full message" }).click();
  await expect(page.getByText("Complete answer end.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Show preview", exact: true })).toBeAttached();
  const beforeReconnect = snapshots;
  sockets.at(-1)!.close({ code: 1013, reason: "Reconnect to synchronize" });
  await expect.poll(() => sockets.length).toBeGreaterThan(1);
  await expect.poll(() => snapshots).toBeGreaterThan(beforeReconnect);
  // The same account/message window retains its completed local read.
  await expect(page.getByText("Complete answer end.", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Show preview" }).click();
  await expect(page.getByText(preview.content, { exact: true })).toBeVisible();
  expect(bodyReads).toBe(2);
  await form.getByRole("button", { name: "Load full question" }).click();
  await expect(form.getByText("Choose the original option", { exact: true })).toBeVisible();
  await expect(form.getByText("Automatic expiry paused · waiting for your answer")).toBeVisible();
  await form.getByRole("radio").check();
  await form.getByRole("button", { name: "Submit", exact: true }).click();
  await expect.poll(() => sent.filter((value) => value.type === "answer_question").length).toBe(1);
  expect(sent.find((value) => value.type === "answer_question")).toMatchObject({ session_id: session.id,
    question_id: question.question_id, request_id: "request", attempt_id: "attempt", answers: [{ selected: ["Original option"] }] });
  expect(sent.filter((value) => value.type === "send_message")).toHaveLength(0);
  expect(sent.filter((value) => value.type === "snooze_question")).toHaveLength(1);
});

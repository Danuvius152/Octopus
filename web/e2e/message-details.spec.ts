import { enterAccountCredentials } from "./current-account";
import { expect, test, type WebSocketRoute } from "./test";

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
  const sockets: WebSocketRoute[] = [];
  const sent: Record<string, unknown>[] = [];
  let bodyReads = 0;
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
    } else if (path === question.detail_ref) data = { role: "assistant", type: "question_request", seq: 1,
      tool_use_id: question.question_id, tool_input: { request_id: "request", attempt_id: "attempt",
        questions: [{ question: "Choose the original option", options: [{ label: "Original option" }] }] } };
    else if (path === `/api/sessions/${session.id}`) {
      expect(url.searchParams.get("view")).toBe("browser"); snapshots++;
      const pause = { role: "system", type: "question_snoozed", seq: 101, tool_use_id: question.question_id,
        tool_input: { request_id: "request", attempt_id: "attempt", deadline: null, snoozed: true } };
      data = { ...session, messages: [preview, ...(question.snoozed ? [pause] : [])], next_message_seq: question.snoozed ? 102 : 101,
        oldest_loaded_seq: 100, has_more_messages: false,
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
  const form = page.locator(".msg-question:not(.msg-question-done)");
  await expect(form.getByRole("button", { name: "Submit", exact: true })).toBeDisabled();
  await form.locator(".question-header").click();
  await expect(form.getByText("Automatic expiry paused · waiting for your answer")).toBeVisible();
  await page.getByRole("button", { name: "View full message", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("503");
  await expect(page.getByText(preview.content, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Retry full message" }).click();
  await expect(page.getByText("Complete answer end.", { exact: true })).toBeVisible();
  await expect(page.getByText("Full message", { exact: true })).toBeVisible();
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

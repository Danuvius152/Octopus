import { enterAccountCredentials } from "./current-account";
import { expect, test, type Page, type WebSocketRoute } from "./test";
import type { PendingQuestion } from "../src/stores/sessionStore";

// Browser projection and actual keyboard/pointer events. The original native
// Owner's deadline, cancellation and expiry arbitration use the process tests.
test("optional input pauses only on user interaction and both pages read the paused projection", async ({ page, context }) => {
  const session = { id: "optional-browser", agent_id: "agent", name: "Optional interaction", backend: "codex",
    status: "running", archived: false, working_dir: "/working", created_at: "2026-11-04T00:00:00Z" };
  const sockets = new Map<Page, WebSocketRoute>();
  const sent = new Map<Page, Record<string, unknown>[]>();
  let question: PendingQuestion = { question_id: "initial", questions: [] };
  let seq = 1;
  const freshQuestion = (index: number) => {
    question = { question_id: `optional-${index}`, questions: [{ question: "Which route?", options: [{ label: "Harbor" }] }],
      request_id: "request", attempt_id: "attempt", interaction_policy: "human", wait_mode: "optional",
      deadline: new Date(Date.now() + 90_000).toISOString(), snoozed: false };
  };
  freshQuestion(0);
  await context.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) { await route.continue(); return; }
    let data: unknown = [];
    if (path === "/api/auth/state") data = { accounts_exist: true };
    else if (path === "/api/auth/login") data = { token: "browser-token" };
    else if (path === "/api/auth/identity") data = { user_id: "browser-user", label: "Operator", is_admin: false };
    else if (path === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend: "codex", pinned: true, is_system: true }];
    else if (path === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
    else if (path === "/api/sessions") data = [session];
    else if (path === `/api/sessions/${session.id}/submissions`) data = { items: [] };
    else if (path === `/api/sessions/${session.id}`) data = { ...session,
      messages: [{ role: "assistant", type: "question_request", seq, tool_use_id: question.question_id, tool_input: question }],
      next_message_seq: seq + 1, oldest_loaded_seq: seq, has_more_messages: false,
      active_attempt: { request_id: "request", attempt_id: "attempt" }, pending_questions: [question], subagents: [],
      dispatch_state: { generation: "service", revision: seq, paused: false, active_request_id: "request", active_operation: "message" as const, items: [] } };
    await route.fulfill({ json: data });
  });
  const open = async (browser: Page) => {
    sent.set(browser, []);
    await browser.routeWebSocket("**/ws?*", (ws) => {
      sockets.set(browser, ws);
      ws.onMessage((raw) => {
        const command = JSON.parse(String(raw)) as Record<string, unknown>;
        sent.get(browser)!.push(command);
        ws.send(JSON.stringify({ type: "command_receipt", session_id: session.id, operation: command.type,
          client_command_id: command.client_command_id, accepted: true }));
      });
    });
    await browser.goto("/");
    await expect(browser.locator("#username, .agent-item").first()).toBeVisible();
    if (await browser.locator("#username").isVisible()) await enterAccountCredentials(browser);
    await expect(browser.locator(".agent-item", { hasText: "Octo" })).toBeVisible();
    await browser.locator(".agent-item", { hasText: "Octo" }).click();
    await browser.locator(".session-item", { hasText: session.name }).click();
    await expect(browser.locator(".msg-question:not(.msg-question-done)")).toBeVisible();
  };
  const second = await context.newPage();
  try {
    await open(page);
    for (const [index, gesture] of ["Tab", "Shift+Tab", "pointer", "typing"].entries()) {
      if (index > 0) {
        freshQuestion(index); seq += 1;
        await page.reload();
        await page.locator(".agent-item", { hasText: "Octo" }).click();
        await page.locator(".session-item", { hasText: session.name }).click();
      }
      const form = page.locator(".msg-question:not(.msg-question-done)");
      const input = form.getByPlaceholder("Or type your own answer…");
      await expect(input).toBeVisible();
      await input.evaluate((node) => node.focus());
      await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      const snoozes = () => sent.get(page)!.filter((entry) => entry.type === "snooze_question" && entry.question_id === question.question_id);
      expect(snoozes()).toHaveLength(0);
      if (gesture === "Tab" || gesture === "Shift+Tab") {
        await form.evaluate((node, after) => {
          const marker = document.createElement("button"); marker.id = "outside-form"; marker.textContent = "Outside form";
          if (after) node.after(marker); else node.before(marker);
          marker.focus();
        }, gesture === "Shift+Tab");
        await page.keyboard.press(gesture);
        await expect.poll(() => form.evaluate((node) => node.contains(document.activeElement))).toBe(true);
      } else if (gesture === "pointer") await form.locator(".question-header").click();
      else await input.press("a");
      await expect.poll(() => snoozes().length).toBe(1);
      expect(snoozes()[0]).toMatchObject({ session_id: session.id, request_id: "request", attempt_id: "attempt",
        generation: "service", client_command_id: expect.any(String) });
      await input.press("b");
      expect(snoozes()).toHaveLength(1);
    }
    // Authority arrives as a separate original event; a send alone did not
    // change the fixed deadline. A second page then reads the saved projection.
    await expect(page.getByText(/Automatic expiry paused/)).toHaveCount(0);
    question = { ...question, deadline: null, snoozed: true }; seq += 1;
    sockets.get(page)!.send(JSON.stringify({ type: "question_snoozed", session_id: session.id, seq,
      question_id: question.question_id, request_id: "request", attempt_id: "attempt", deadline: null, snoozed: true }));
    await expect(page.getByText(/Automatic expiry paused/)).toBeVisible();
    await open(second);
    await expect(second.getByText(/Automatic expiry paused/)).toBeVisible();
    expect(sent.get(second)!.filter((entry) => entry.type === "snooze_question")).toHaveLength(0);
  } finally { await second.close(); }
});

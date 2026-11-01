import { expect, test, type WebSocketRoute } from "./test";
import { enterAccountCredentials } from "./current-account";
import type { Message } from "../src/stores/sessionStore";
import type { RuntimeMetadata, SubagentRun } from "../src/api";

/** Browser projection only; native I/O and SQLite ownership use the controlled
 * process regressions in test_codex_app_server_{questions,output}.py. */
test("native root and helper items stay separate through completion, reconnect and reload", async ({ page }) => {
  const session = { id: "native-browser", agent_id: "native-agent", name: "Native observation",
    backend: "codex", status: "running", archived: false, working_dir: "/tmp", created_at: "2026-11-01T00:00:00Z" };
  const identity = (thread: string, item: string | null): RuntimeMetadata => ({ request_id: "request", attempt_id: "attempt",
    native: { thread_id: thread, turn_id: "turn", item_id: item, root_thread_id: "root", root_turn_id: "turn" } });
  let helper: SubagentRun = { card_id: "helper-round", task_id: "helper", native_thread_id: "helper", native_turn_id: "turn",
    parent_thread_id: "root", runtime_metadata: identity("helper", null), name: "/root/reviewer", status: "running",
    description: "Reviewing", summary: "", prompt: "", steps: [] };
  const messages: Message[] = [{ role: "assistant", type: "text", seq: 1, content: "Root progress",
    runtime_metadata: identity("root", "initial") }];
  let socket: WebSocketRoute;
  let connections = 0;
  let revision = 1;
  let done = false;
  let reconnectDetails = 0;
  let reconnectGeneration = 0;
  const sent: unknown[] = [];
  const httpWrites: { method: string; path: string }[] = [];
  const detailReads: number[] = [];
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) { await route.continue(); return; }
    const method = route.request().method();
    if (!["GET", "HEAD"].includes(method) && path !== "/api/auth/login") {
      httpWrites.push({ method, path });
      await route.fulfill({ status: 409, json: { detail: "Observation fixture forbids execution or mutation" } });
      return;
    }
    let data: unknown = [];
    if (path === "/api/auth/state") data = { accounts_exist: true };
    else if (path === "/api/auth/login") data = { token: "browser-token" };
    else if (path === "/api/auth/identity") data = { user_id: "browser-user", label: "Operator", is_admin: false };
    else if (path === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend: "codex", pinned: true, is_system: true }];
    else if (path === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
    else if (path === "/api/sessions") data = [{ ...session, status: done ? "idle" : "running" }];
    else if (path.startsWith(`/api/sessions/${session.id}/native-details/`)) {
      const anchor = Number(path.split("/").at(-1));
      detailReads.push(anchor);
      if (!messages.some((row) => row.seq === anchor)) {
        await route.fulfill({ status: 404, json: { detail: "No saved reference" } });
        return;
      }
      const collected = messages.filter((row) => row.type === "text" && row.runtime_metadata?.native?.thread_id === "helper");
      data = { anchor_seq: anchor, child: helper, availability: collected.length ? "available" : "not_collected",
        messages: collected, next_after_seq: collected.at(-1)?.seq ?? null, has_more: false };
    }
    else if (path === `/api/sessions/${session.id}`) {
      const reconnected = reconnectGeneration > 0 && connections >= reconnectGeneration;
      if (reconnected) reconnectDetails += 1;
      data = { ...session, status: done ? "idle" : "running",
        messages, next_message_seq: messages.at(-1)!.seq! + 1, oldest_loaded_seq: 1, has_more_messages: false,
        pending_questions: [], subagents: [{ ...helper, name: reconnected ? "/root/reviewer restored" : helper.name }],
        dispatch_state: { generation: "native-service", revision, paused: false, active_request_id: done ? null : "request", items: [] } };
    }
    await route.fulfill({ json: data });
  });
  await page.routeWebSocket("**/ws?*", (ws) => {
    socket = ws;
    connections += 1;
    ws.onMessage((raw) => sent.push(JSON.parse(String(raw))));
  });
  const send = (frame: Record<string, unknown>) => socket.send(JSON.stringify({ session_id: session.id, ...frame }));
  const open = async () => {
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await page.locator(".session-item", { hasText: session.name }).click();
    await expect(page.locator(".conn-status.on")).toBeVisible();
  };
  await page.goto("/");
  await enterAccountCredentials(page);
  await open();
  const card = page.locator(".subagent-card");
  // The status row is outside this window: the existing snapshot supplies its
  // anchor, without manufacturing a stored message or executable work.
  await expect(card).toHaveCount(1);
  await expect(card.locator(".subagent-name")).toHaveText("/root/reviewer");
  await card.locator(".subagent-summary-row").click();
  await expect(card.getByText(/No saved reference/)).toBeVisible();
  expect(detailReads).toEqual([]);
  await card.locator(".subagent-summary-row").click();
  send({ type: "assistant_delta", content: "Root partial", runtime_metadata: identity("root", "same") });
  send({ type: "assistant_delta", content: "Helper partial", runtime_metadata: identity("helper", "same") });
  await expect(page.locator('[data-testid="streaming-text"] .msg-streaming-text')).toHaveText("Root partial");
  await expect(card.locator(".subagent-summary")).toHaveText("Helper partial");
  const child: Message = { role: "assistant", type: "text", seq: 2, content: "Helper saved answer", runtime_metadata: identity("helper", "same") };
  messages.push(child);
  send({ ...child, type: "assistant_text" });
  await expect(card.locator(".subagent-summary")).toHaveText("Helper saved answer");
  await expect(page.locator('[data-testid="streaming-text"] .msg-streaming-text')).toHaveText("Root partial");
  await expect(page.locator(".msg-assistant .msg-content")).not.toContainText(["Helper saved answer"]);
  helper = { ...helper, status: "completed" };
  const { runtime_metadata, ...details } = helper;
  messages.push({ role: "system", type: "native_subagent", seq: 3, runtime_metadata, tool_input: details });
  send({ type: "subagent", ...helper, seq: 3 });
  const root: Message = { role: "assistant", type: "text", seq: 4, content: "Root final answer", runtime_metadata: identity("root", "same") };
  messages.push(root);
  send({ ...root, type: "assistant_text" });
  revision += 1;
  done = true;
  send({ type: "dispatch_state", dispatch_state: { generation: "native-service", revision, paused: false, active_request_id: null, items: [] } });
  send({ type: "status", status: "idle" });
  await expect(page.locator('[data-testid="streaming-text"]')).toHaveCount(0);
  await expect(card).toHaveClass(/subagent-completed/);
  await expect(card.locator(".subagent-description")).toHaveText("helper finished");
  await card.locator(".subagent-summary-row").click();
  const savedDetails = card.getByRole("region", { name: "Saved helper details" });
  await expect(savedDetails.getByText("Helper saved answer", { exact: true })).toBeVisible();
  await expect(savedDetails.getByText(/parent has consumed/)).toBeVisible();
  await card.locator(".subagent-summary-row").click();
  reconnectGeneration = connections + 1;
  socket!.close({ code: 1012, reason: "Original stream reconnect" });
  await expect.poll(() => connections).toBeGreaterThanOrEqual(reconnectGeneration);
  await expect.poll(() => reconnectDetails).toBeGreaterThan(0);
  // This name exists only in the reconnected HTTP snapshot. Its rendered
  // appearance proves that hydration applied; the old DOM cannot satisfy it.
  await expect(card.locator(".subagent-name")).toHaveText("/root/reviewer restored");
  await expect(card).toHaveCount(1);
  await expect(card.locator(".subagent-summary")).toHaveText("Helper saved answer");
  await page.reload();
  await open();
  await expect(card).toHaveCount(1);
  await expect(card).toHaveClass(/subagent-completed/);
  await expect(card.locator(".subagent-summary")).toHaveText("Helper saved answer");
  await expect(page.locator(".msg-assistant .msg-content")).toHaveText(["Root progress", "Root final answer"]);
  await card.locator(".subagent-summary-row").click();
  await expect(card.getByRole("region", { name: "Saved helper details" }).getByText("Helper saved answer", { exact: true })).toBeVisible();
  expect(detailReads.length).toBeGreaterThanOrEqual(2);
  expect(sent).toEqual([]);
  expect(httpWrites).toEqual([]);
});

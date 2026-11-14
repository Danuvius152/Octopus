import { accountToken, enterAccountCredentials } from "./current-account";
import { expect, test, type Page, type WebSocketRoute } from "./test";
import { systemAgentId } from "./system-agent";

/** Deterministic HTTP/WS boundary script. Queue execution is covered by the
 * real driver in test_session_dispatch.py; this checks the browser contract
 * without starting a CLI or model. */
test("Stop keeps returns paused through correction and reconnect until explicit Resume", async ({ page }) => {
  const session = {
    id: "paused-dispatch-browser", agent_id: "dispatch-agent", name: "Dispatch conversation",
    backend: "codex", working_dir: "/tmp", created_at: "2026-10-13T00:00:00Z", archived: false,
  };
  const pending = [
    { request_id: "bg-return", operation: "message" as const, source: "bg", summary: "Background command finished", priority: false },
    { request_id: "agent-return", operation: "message" as const, source: "delegation", summary: "Child agent returned", priority: false },
  ];
  const snapshots = {
    initial: { generation: "browser-service", revision: 1, paused: false, active_request_id: "original", active_operation: "message" as const, items: pending },
    paused: { generation: "browser-service", revision: 2, paused: true, active_request_id: null, active_operation: null, items: pending },
    correcting: { generation: "browser-service", revision: 3, paused: true, active_request_id: "correction", active_operation: "message" as const, items: pending },
    corrected: { generation: "browser-service", revision: 4, paused: true, active_request_id: null, active_operation: null, items: pending },
    resumed: { generation: "browser-service", revision: 6, paused: false, active_request_id: "bg-return", active_operation: "message" as const, items: pending.slice(1) },
  };
  let phase: keyof typeof snapshots = "initial";
  let socket: WebSocketRoute;
  let connections = 0;
  let resumed = 0;
  const requests: Record<string, unknown>[] = [];
  const interrupted = { kind: "outcome", operation: "message", request_id: "original", status: "interrupted", recovery: {} };
  const final = { kind: "outcome", operation: "message", request_id: "correction", status: "success", recovery: {} };
  const context = { request_context: { request_id: "correction", source: "human", work_id: null } };
  const messages = [
    { role: "system", type: "result", content: interrupted, is_error: true, seq: 0 },
    { role: "user", type: "text", content: "correct the direction", tool_input: context, seq: 1 },
    { role: "assistant", type: "text", content: "Direction corrected", seq: 2 },
    { role: "system", type: "result", content: final, is_error: false, seq: 3 },
  ];
  const send = (frame: Record<string, unknown>) => socket.send(JSON.stringify({ session_id: session.id, ...frame }));

  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) {
      await route.continue();
      return;
    }
    let data: unknown = [];
    if (path === "/api/auth/state") data = { accounts_exist: true };
    else if (path === "/api/auth/login") data = { token: "browser-token" };
    else if (path === "/api/auth/identity") data = { user_id: "browser-user", label: "Operator", is_admin: true };
    else if (path === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend: "codex", is_system: true, pinned: true, archived: false }];
    else if (path === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
    else if (path === "/api/sessions") data = [{ ...session, status: phase === "initial" || phase === "correcting" || phase === "resumed" ? "running" : "idle" }];
    else if (path === `/api/sessions/${session.id}`) data = {
      ...session, status: phase === "initial" || phase === "correcting" || phase === "resumed" ? "running" : "idle",
      messages: phase === "corrected" || phase === "resumed" ? messages : [],
      next_message_seq: phase === "corrected" || phase === "resumed" ? 4 : 0,
      oldest_loaded_seq: 0, has_more_messages: false, pending_questions: [], subagents: [],
      active_attempt: snapshots[phase].active_request_id === null ? null : {
        request_id: snapshots[phase].active_request_id, attempt_id: `attempt-${snapshots[phase].active_request_id}`,
      },
      dispatch_state: snapshots[phase],
    };
    else if (path.endsWith("/dispatch/resume")) {
      expect(route.request().method()).toBe("POST");
      expect(route.request().headers().authorization).toBe("Bearer browser-token");
      resumed += 1;
      phase = "resumed";
      // A newer live snapshot arrives before an older HTTP response.
      send({ type: "dispatch_state", dispatch_state: snapshots.resumed });
      send({ type: "status", status: "running" });
      data = { ...snapshots.resumed, revision: 5, items: pending };
    }
    await route.fulfill({ json: data });
  });
  await page.routeWebSocket("**/ws?*", (ws) => {
    socket = ws;
    connections += 1;
    ws.onMessage((raw) => {
      const request = JSON.parse(String(raw));
      requests.push(request);
      if (request.type === "interrupt") {
        phase = "paused";
        send({ type: "dispatch_state", dispatch_state: snapshots.paused });
        send({ type: "result", seq: 0, content: interrupted, is_error: true });
        send({ type: "status", status: "idle" });
      } else if (request.type === "send_message") {
        expect(request.content).toBe("correct the direction");
        phase = "correcting";
        send({ type: "user_message", seq: 1, content: request.content, tool_input: context });
        send({ type: "dispatch_state", dispatch_state: snapshots.correcting });
        send({ type: "status", status: "running" });
      }
    });
  });

  await page.goto("/");
  await enterAccountCredentials(page);
  await page.locator(".agent-item", { hasText: "Octo" }).click();
  await page.locator(".session-item", { hasText: session.name }).click();
  await expect(page.locator(".queue-content")).toHaveText(["Background command finished", "Child agent returned"]);
  await page.getByRole("button", { name: "Stop", exact: true }).click();
  await expect(page.getByText("Paused · 2 pending")).toBeVisible();
  expect(resumed).toBe(0);
  await page.locator(".chat-input-bar textarea").fill("correct the direction");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await expect(page.locator(".status-badge")).toHaveText("running");
  await expect(page.getByText("Paused · 2 pending")).toBeVisible();
  await expect(page.locator(".dispatch-pause-hint")).toContainText("takes priority");
  phase = "corrected";
  send({ type: "assistant_text", seq: 2, content: "Direction corrected" });
  send({ type: "result", seq: 3, content: final, is_error: false });
  send({ type: "dispatch_state", dispatch_state: snapshots.corrected });
  send({ type: "status", status: "idle" });
  await expect(page.locator(".status-badge")).toHaveText("idle");
  socket!.close({ code: 1012, reason: "Reconnect regression" });
  await expect.poll(() => connections).toBe(2);
  await expect(page.locator(".conn-status.on")).toBeVisible();
  await expect(page.getByText("Paused · 2 pending")).toBeVisible();
  await expect(page.locator(".queue-content")).toHaveText(["Background command finished", "Child agent returned"]);
  expect(requests.map((request) => request.type)).toEqual(["interrupt", "send_message"]);
  expect(resumed).toBe(0);
  await page.getByRole("button", { name: "Resume processing" }).click();
  await expect(page.locator(".queue-list-label")).toContainText("Queued (1)");
  await expect(page.locator(".queue-content")).toHaveText(["Child agent returned"]);
  expect(resumed).toBe(1);
});

test("real idle Stop and Resume synchronize dispatch pause across browser clients", async ({ page, context, request }) => {
  const api = "http://localhost:8765/api";
  const headers = { Authorization: `Bearer ${accountToken()}` };
  const created = await request.post(`${api}/sessions`, {
    headers, data: { agent_id: await systemAgentId(request, `${api}/sessions`, headers), name: `Idle remote dispatch ${Date.now()}`, backend: "codex" },
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  const observer = await context.newPage();
  const openSession = async (client: Page) => {
    await client.goto("/");
    if (!await client.evaluate(() => localStorage.getItem("octopus_token"))) await enterAccountCredentials(client);
    await client.locator(".agent-item", { hasText: "Octo" }).click();
    await client.locator(".session-item", { hasText: session.name }).click();
    await expect(client.locator(".conn-status.on")).toBeVisible();
  };
  try {
    await openSession(page);
    await openSession(observer);
    const current = await request.get(`${api}/sessions/${session.id}`, { headers });
    expect(current.ok()).toBeTruthy();
    const snapshot = (await current.json()).dispatch_state;
    expect(snapshot.active_request_id).toBeNull();
    await page.evaluate(({ sessionId, snapshot }) => new Promise<void>((resolve, reject) => {
      const socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws?token=${encodeURIComponent(localStorage.getItem("octopus_token") ?? "")}`);
      const probe = { socket, frames: [] as Record<string, unknown>[] };
      (window as unknown as { dispatchProbe: typeof probe }).dispatchProbe = probe;
      socket.onmessage = (event) => probe.frames.push(JSON.parse(event.data));
      socket.onerror = () => reject(new Error("Dispatch probe connection failed"));
      socket.onopen = () => {
        socket.send(JSON.stringify({ type: "interrupt", session_id: sessionId,
          client_command_id: crypto.randomUUID(), request_id: null, attempt_id: null,
          generation: snapshot.generation, revision: snapshot.revision }));
        resolve();
      };
    }), { sessionId: session.id, snapshot });
    await expect(page.getByText("Paused · 0 pending")).toBeVisible();
    await expect(observer.getByText("Paused · 0 pending")).toBeVisible();
    const readPause = async () => {
      const response = await request.get(`${api}/sessions/${session.id}`, { headers });
      expect(response.ok()).toBeTruthy();
      return (await response.json()).dispatch_state.paused;
    };
    await expect.poll(readPause).toBe(true);
    await page.getByRole("button", { name: "Resume processing" }).click();
    await expect.poll(readPause).toBe(false);
    await expect(page.getByText("Paused · 0 pending")).toHaveCount(0);
    await expect(observer.getByText("Paused · 0 pending")).toHaveCount(0);
    await expect.poll(() => page.evaluate((sessionId: string) => {
      const probe = (window as unknown as { dispatchProbe: { frames: Record<string, unknown>[] } }).dispatchProbe;
      return probe.frames.filter((frame) => frame.type === "dispatch_state" && frame.session_id === sessionId)
        .map((frame) => (frame.dispatch_state as { paused: boolean }).paused);
    }, session.id)).toEqual([true, false]);
  } finally {
    await page.evaluate(() => {
      (window as unknown as { dispatchProbe?: { socket: WebSocket } }).dispatchProbe?.socket.close();
    }).catch(() => {});
    await observer.close();
    await request.delete(`${api}/sessions/${session.id}`, { headers });
  }
});

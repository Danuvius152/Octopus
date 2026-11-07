import { expect, test, type Page, type WebSocketRoute } from "./test";
import { enterAccountCredentials } from "./current-account";

// The real Owner/SQLite/native-process ordering is covered by test_queued_compact.py.
// These shared server snapshots exercise the browser's claim/cancel race and refresh.
for (const backend of ["codex", "claude-code"]) {
  test(`${backend} queued compact follows server claim across tabs and refresh`, async ({ page, context }) => {
    const session = { id: "queued-compact-browser", agent_id: "compact-agent", name: "Queued compaction",
      backend, status: "running", can_steer: true, can_compact: true, archived: false,
      working_dir: "/tmp", created_at: "2026-11-07T00:00:00Z" };
    const ordinary = { request_id: "ordinary", operation: "message" as const, source: "bg", summary: "Background return", priority: false };
    const compact = { request_id: "compact-original", operation: "compact" as const, source: "human", summary: "/compact", priority: false };
    let state = { generation: "compact-service", revision: 1, paused: false,
      active_request_id: "original-work", active_operation: "message" as "message" | "compact", items: [ordinary, compact] };
    const sockets: WebSocketRoute[] = [];
    const requests: Record<string, unknown>[] = [];
    const publish = () => {
      for (const socket of sockets) {
        socket.send(JSON.stringify({ type: "dispatch_state", session_id: session.id, dispatch_state: state }));
      }
    };
    await context.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (!path.startsWith("/api/")) { await route.continue(); return; }
      let data: unknown = [];
      if (path === "/api/auth/state") data = { accounts_exist: true };
      else if (path === "/api/auth/login") data = { token: "browser-token" };
      else if (path === "/api/auth/identity") data = { user_id: "compact-user", label: "Operator", is_admin: true };
      else if (path === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend, is_system: true, pinned: true, archived: false }];
      else if (path === "/api/backends") data = { available: [backend], default: backend, models: { [backend]: [] } };
      else if (path === "/api/sessions") data = [session];
      else if (path === `/api/sessions/${session.id}/submissions`) data = { items: [] };
      else if (path === `/api/sessions/${session.id}`) data = { ...session, messages: [], next_message_seq: 0,
        oldest_loaded_seq: null, has_more_messages: false, pending_questions: [], subagents: [],
        active_attempt: { request_id: state.active_request_id, attempt_id: "attempt-" + state.active_request_id },
        dispatch_state: state, context_generation: state.generation, context_state: null };
      await route.fulfill({ json: data });
    });
    await context.routeWebSocket("**/ws?*", (socket) => {
      sockets.push(socket);
      socket.onClose(() => sockets.splice(sockets.indexOf(socket), 1));
      socket.onMessage((raw) => {
        const command = JSON.parse(String(raw));
        requests.push(command);
        if (command.type === "cancel_compact") {
          expect(command).toMatchObject({ session_id: session.id, request_id: compact.request_id, generation: state.generation });
          state = { ...state, revision: 2, active_request_id: compact.request_id, active_operation: "compact", items: [ordinary] };
          publish();
          socket.send(JSON.stringify({ type: "command_receipt", session_id: session.id,
            operation: "cancel_compact", client_command_id: command.client_command_id, accepted: false,
            code: "already_started", message: "Compaction already started. Use Stop to interrupt it." }));
        } else if (command.type === "send_message") {
          socket.send(JSON.stringify({ type: "submission", session_id: session.id,
            client_request_id: command.client_request_id, accepted: true, request_id: "queued-human" }));
        }
      });
    });
    const open = async (tab: Page, login = false) => {
      await tab.goto("/");
      if (login) await enterAccountCredentials(tab);
      await tab.locator(".agent-item", { hasText: "Octo" }).click();
      await tab.locator(".session-item", { hasText: session.name }).click();
    };
    await open(page, true);
    const observer = await context.newPage();
    try {
      await open(observer);
      for (const tab of [page, observer]) {
        await expect(tab.getByText("Waiting to compact after current work")).toBeVisible();
        await expect(tab.getByRole("button", { name: "Queue message", exact: true })).toBeVisible();
      }
      await page.getByRole("button", { name: "Cancel pending compaction" }).click();
      await expect(page.getByLabel("Command confirmations").getByRole("alert")).toContainText("Compaction already started. Use Stop");
      for (const tab of [page, observer]) {
        await expect(tab.getByText("Compacting native context…")).toBeVisible();
        await expect(tab.getByRole("button", { name: "Cancel pending compaction" })).toHaveCount(0);
      }
      await observer.reload();
      await observer.locator(".agent-item", { hasText: "Octo" }).click();
      await observer.locator(".session-item", { hasText: session.name }).click();
      await expect(observer.getByText("Compacting native context…")).toBeVisible();
      await observer.locator(".chat-input-bar textarea").fill("Continue after compact");
      await observer.getByRole("button", { name: "Queue message", exact: true }).click();
      await expect.poll(() => requests.map((command) => command.type)).toEqual(["cancel_compact", "send_message"]);
      expect(requests[1].content).toBe("Continue after compact");
      expect(requests.some((command) => command.type === "interrupt")).toBe(false);
    } finally {
      await observer.close();
    }
  });
}

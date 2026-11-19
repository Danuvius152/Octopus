import { enterAccountCredentials } from "./current-account";
import { expect, test } from "./test";

test("returning to a session shares its pending detail, BG, Research and delegation GETs", async ({ page }) => {
  const agent = { id: "navigation-agent", name: "Octo", backend: "codex", pinned: true, is_system: true };
  const sessions = ["a", "b"].map((id) => ({ id, agent_id: agent.id, name: `Session ${id.toUpperCase()}`,
    backend: "codex", status: "idle", archived: false, working_dir: "/tmp", created_at: "2026-11-02T00:00:00Z" }));
  const suffixes = ["", "/bg-tasks", "/research", "/delegations"];
  const counts = new Map<string, number>();
  const sessionReads = (id: string) => suffixes.map((suffix) => counts.get(`/api/sessions/${id}${suffix}`) ?? 0);
  let releaseA!: () => void;
  const holdA = new Promise<void>((resolve) => { releaseA = resolve; });
  const research = { id: "saved-research", session_id: "a", status: "running", phase: "search", question: "Research from the shared response" };
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) { await route.continue(); return; }
    let data: unknown = [];
    if (path === "/api/auth/state") data = { accounts_exist: true };
    else if (path === "/api/auth/login") data = { token: "navigation-token" };
    else if (path === "/api/auth/identity") data = { user_id: "navigation-user", label: "Reader", is_admin: false };
    else if (path === "/api/agents") data = [agent];
    else if (path === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
    else if (path === "/api/sessions") data = sessions;
    else {
      const session = sessions.find(({ id }) => suffixes.some((suffix) => path === `/api/sessions/${id}${suffix}`));
      if (session) {
        counts.set(path, (counts.get(path) ?? 0) + 1);
        if (session.id === "a") await holdA;
        if (path === `/api/sessions/${session.id}`) data = { ...session,
          messages: [{ role: "assistant", type: "text", seq: 0, content: `${session.name} saved history` }],
          next_message_seq: 1, oldest_loaded_seq: 0, has_more_messages: false, active_attempt: null,
          dispatch_state: { generation: "navigation-service", revision: 1, paused: false,
            active_request_id: null, active_operation: null, items: [] },
        };
        if (path === "/api/sessions/a/research") data = [research];
      }
    }
    await route.fulfill({ json: data });
  });
  await page.routeWebSocket("**/ws?*", () => {});
  try {
    await page.goto("/");
    await enterAccountCredentials(page);
    await page.locator(".agent-item", { hasText: agent.name }).click();
    const entry = (id: string) => page.locator(`.session-item[data-session-id="${id}"]`);
    const transcript = page.locator(".chat-messages");

    // Establish the connection's generation before measuring compatible reads.
    const warmReads = Promise.all(suffixes.map(async (suffix) => {
      const response = await page.waitForResponse((response) => new URL(response.url()).pathname === `/api/sessions/b${suffix}`);
      await response.finished();
    }));
    await entry("b").click();
    await expect(transcript.getByText("Session B saved history")).toBeVisible();
    await warmReads;
    counts.clear();
    await entry("a").click();
    await expect.poll(() => sessionReads("a")).toEqual([1, 1, 1, 1]);
    await entry("b").click();
    await expect(transcript.getByText("Session B saved history")).toBeVisible();
    await expect.poll(() => sessionReads("b")).toEqual([1, 1, 1, 1]);
    await entry("a").click();
    await expect(entry("a")).toHaveClass(/\bactive\b/);

    releaseA();
    await expect(transcript.getByText("Session A saved history")).toBeVisible();
    await expect(page.getByText(research.question, { exact: true })).toBeVisible();
    expect(sessionReads("a")).toEqual([1, 1, 1, 1]);
  } finally {
    releaseA();
  }
});

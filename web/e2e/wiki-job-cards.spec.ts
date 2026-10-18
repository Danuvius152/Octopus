import { expect, test, type WebSocketRoute } from "@playwright/test";

const API = "http://localhost:8765/api";
const headers = { Authorization: "Bearer changeme" };

test("Wiki cards persist, cancel their displayed attempt, and catch up after reconnect", async ({ page, context, request }) => {
  test.setTimeout(60_000);
  const created = await request.post(`${API}/sessions`, {
    headers, data: { name: "Persistent Wiki cards", working_dir: "/tmp" },
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  const fixture = `${API}/_e2e/wiki-cards/${session.id}`;
  const prepared = await request.post(fixture, { headers });
  expect(prepared.ok(), await prepared.text()).toBeTruthy();
  const scope = await prepared.json();
  const jobs: string[] = [];
  const sockets: WebSocketRoute[] = [];
  let disconnected = false;
  await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => {
    if (disconnected) { socket.close(); return; }
    sockets.push(socket);
    socket.connectToServer();
  });
  const openOriginalSession = async () => {
    const agent = page.locator(".agent-item", { hasText: "Octo" });
    await expect(agent).toBeVisible();
    const row = page.locator(".session-item", { hasText: session.name });
    if (!(await row.isVisible())) await agent.click();
    await row.click();
    await expect(page.locator(".conn-status.on")).toBeVisible();
  };
  const submit = async (key: string) => {
    const result = await request.post(`${API}/wiki/maintain`, {
      headers: { Authorization: `Bearer ${scope.token}` },
      data: { idempotency_key: key, paths: ["note.md"], scope: "Browser persistence control" },
    });
    expect(result.ok(), await result.text()).toBeTruthy();
    const job = await result.json();
    jobs.push(job.id);
    expect(job.status).toBe("queued");
    expect(job.attempt).toBe(0);
    expect((await request.post(`${fixture}/deliver`, { headers })).ok()).toBeTruthy();
    return job;
  };
  try {
    await page.goto("/");
    await page.locator('input[type="password"]').fill("changeme");
    await page.locator(".btn-login").click();
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await page.locator(".session-item", { hasText: session.name }).click();
    await expect(page.locator(".conn-status.on")).toBeVisible();
    const first = await submit("browser-card-first");
    const card = page.locator(`[data-wiki-job="${first.id}"]`);
    await expect(card).toHaveAttribute("data-status", "queued");
    const control = page.waitForResponse((value) => value.url().endsWith(`/wiki/jobs/${first.id}/cancel`));
    await card.getByRole("button", { name: "Cancel maintenance" }).click();
    const response = await control;
    expect(response.ok()).toBeTruthy();
    expect((await response.json()).settlement_phase).toBe("confirmed");
    expect(response.request().postDataJSON()).toEqual({
      expected_attempt: 0, expected_vault_id: scope.vault_id,
    });
    expect((await request.post(`${fixture}/deliver`, { headers })).ok()).toBeTruthy();
    const received = await (await request.get(`${API}/sessions/${session.id}/wiki-jobs`, { headers })).json();
    expect(received[0].settlement_phase).toBe("confirmed");
    await expect(card).toHaveAttribute("data-status", "cancelled");
    await expect(card.getByRole("button", { name: "Retry unfinished work" })).toBeVisible();

    const second = await submit("browser-card-second");
    const secondCard = page.locator(`[data-wiki-job="${second.id}"]`);
    await expect(secondCard).toHaveAttribute("data-status", "queued");
    disconnected = true;
    await context.setOffline(true);
    for (const socket of sockets.splice(0)) socket.close();
    await expect(page.locator(".conn-status.on")).toHaveCount(0);
    const cancelled = await request.post(`${API}/wiki/jobs/${second.id}/cancel`, {
      headers, data: { expected_attempt: 0, expected_vault_id: scope.vault_id },
    });
    expect(cancelled.ok(), await cancelled.text()).toBeTruthy();
    expect((await request.post(`${fixture}/deliver`, { headers })).ok()).toBeTruthy();
    disconnected = false;
    await context.setOffline(false);
    await expect(page.locator(".conn-status.on")).toBeVisible();
    await expect(secondCard).toHaveAttribute("data-status", "cancelled");

    await page.reload();
    await openOriginalSession();
    await expect(page.locator(`[data-wiki-job="${first.id}"]`)).toHaveAttribute("data-status", "cancelled");
    await expect(page.locator(`[data-wiki-job="${second.id}"]`)).toHaveAttribute("data-status", "cancelled");
    expect((await request.post(`${API}/sessions/${session.id}/archive?replace=false`, { headers })).ok()).toBeTruthy();
    const archived = await request.get(`${API}/sessions/${session.id}/wiki-jobs`, { headers });
    expect(archived.ok()).toBeTruthy();
    const persisted = await archived.json();
    expect(persisted).toHaveLength(2);
    expect(persisted.every((value: { origin_request_id: string; status: string; settlement_phase: string }) =>
      value.origin_request_id === scope.request_id && value.status === "cancelled" && value.settlement_phase === "confirmed")).toBe(true);
    const detail = await (await request.get(`${API}/sessions/${session.id}`, { headers })).json();
    expect(detail.messages).toEqual([]);
    expect((await request.post(`${API}/sessions/${session.id}/unarchive`, { headers })).ok()).toBeTruthy();
    await page.reload();
    await openOriginalSession();
    await expect(page.locator("[data-wiki-job]")).toHaveCount(2);
  } finally {
    disconnected = false;
    await context.setOffline(false);
    for (const job of jobs) {
      await request.post(`${API}/wiki/jobs/${job}/cancel`, { headers });
    }
    expect((await request.delete(fixture, { headers })).ok()).toBeTruthy();
    await request.delete(`${API}/sessions/${session.id}`, { headers });
  }
});

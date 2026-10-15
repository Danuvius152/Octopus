import { expect, test } from "@playwright/test";

const API = "http://localhost:8765/api";
const headers = { Authorization: "Bearer changeme" };

test("a rejected draft survives archive and restores its text and attachment to a new conversation", async ({ page, request }) => {
  const sent: { session_id: string; client_request_id: string; content: string; attachment_ids: string[]; wiki_mode?: string }[] = [];
  await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => {
    const server = socket.connectToServer();
    socket.onMessage((message) => {
      const data = JSON.parse(String(message));
      if (data.type !== "send_message") { server.send(message); return; }
      sent.push(data);
      socket.send(JSON.stringify({ type: "submission", session_id: data.session_id,
        client_request_id: data.client_request_id, accepted: false, message: "This conversation is closed to new messages." }));
    });
  });
  const firstResponse = await request.post(`${API}/sessions`, { headers, data: { name: "Rejected draft original", working_dir: "/tmp" } });
  expect(firstResponse.ok()).toBeTruthy();
  const first = await firstResponse.json();
  let nextId: string | undefined;
  try {
    await page.goto("/");
    await page.locator('input[type="password"]').fill("changeme");
    await page.locator(".btn-login").click();
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await page.locator(".session-item", { hasText: first.name }).click();
    await expect(page.locator(".conn-status.on")).toBeVisible();
    const input = page.locator(".chat-input-bar textarea");
    const text = "  /wiki maintain Library/note.md  ";
    await input.fill(text);
    await page.getByTestId("attachment-file-input").setInputFiles({ name: "retained.txt", mimeType: "text/plain", buffer: Buffer.from("original attachment bytes") });
    await expect(page.locator(".attachment-pending")).toContainText("retained.txt");
    await expect(page.locator(".attachment-pending")).not.toContainText("uploading");
    await page.getByRole("button", { name: "Send message" }).click();
    await expect(page.getByRole("button", { name: "Restore draft" })).toBeVisible();
    await input.fill("new editing must survive");
    await expect(page.getByRole("button", { name: "Restore draft" })).toBeDisabled();
    await expect(input).toHaveValue("new editing must survive");
    await input.fill("");
    const archived = await request.post(`${API}/sessions/${first.id}/archive?replace=false`, { headers });
    expect(archived.ok()).toBeTruthy();
    await expect(page.getByRole("button", { name: "Restore draft" })).toBeVisible();
    await page.getByRole("button", { name: "New session for Octo", exact: true }).click();
    await page.getByPlaceholder("Session name").fill("Restored draft destination");
    const created = page.waitForResponse((response) => response.url().includes("/api/agents/") && response.url().endsWith("/sessions") && response.request().method() === "POST");
    await page.getByPlaceholder("Session name").press("Enter");
    const nextResponse = await created;
    expect(nextResponse.ok()).toBeTruthy();
    const next = await nextResponse.json();
    nextId = next.id;
    await page.getByRole("button", { name: "Restore draft" }).click();
    await expect(input).toHaveValue(text);
    await expect(page.getByRole("button", { name: "Use local Wiki for this message" })).toHaveCount(0);
    await expect(page.locator(".attachment-pending")).toContainText("retained.txt");
    await expect(page.locator(".attachment-pending")).not.toContainText("uploading");
    expect(sent).toHaveLength(1);
    await page.getByRole("button", { name: "Send message" }).click();
    await expect.poll(() => sent.length).toBe(2);
    expect(sent[1].content).toBe(text.trim());
    expect(sent[1]).not.toHaveProperty("wiki_mode");
    expect(sent[1].session_id).toBe(next.id);
    expect(sent[1].attachment_ids[0]).not.toBe(sent[0].attachment_ids[0]);
    const downloaded = await request.get(`${API}/sessions/${next.id}/attachments/${sent[1].attachment_ids[0]}`, { headers });
    expect(downloaded.ok()).toBeTruthy();
    expect(await downloaded.text()).toBe("original attachment bytes");
  } finally {
    await request.delete(`${API}/sessions/${first.id}`, { headers }).catch(() => {});
    if (nextId) await request.delete(`${API}/sessions/${nextId}`, { headers }).catch(() => {});
  }
});

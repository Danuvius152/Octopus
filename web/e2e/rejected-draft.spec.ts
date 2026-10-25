import { TEST_BEARER, enterAccountCredentials } from "./current-account";
import { expect, test } from "./test";
import { systemAgentId } from "./system-agent";

const API = "http://localhost:8765/api";
const headers = { Authorization: `Bearer ${TEST_BEARER}` };

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
  const firstResponse = await request.post(`${API}/sessions`, { headers, data: { agent_id: await systemAgentId(request, `${API}/sessions`, headers), name: "Rejected draft original", } });
  expect(firstResponse.ok()).toBeTruthy();
  const first = await firstResponse.json();
  let nextId: string | undefined;
  try {
    await page.goto("/");
    await enterAccountCredentials(page);
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

test("session-owned uploads preserve the complete draft across conversations", async ({ page, request }) => {
  const sessions = [];
  for (const suffix of ["A", "B"]) {
    const response = await request.post(`${API}/sessions`, { headers, data: {
      agent_id: await systemAgentId(request, `${API}/sessions`, headers),
      name: `Attachment ownership ${suffix}`, backend: "codex",
    } });
    expect(response.ok()).toBeTruthy();
    sessions.push(await response.json());
  }
  const [first, second] = sessions;
  try {
    await page.goto("/");
    await enterAccountCredentials(page);
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await page.locator(".session-item", { hasText: first.name }).click();
    await expect(page.locator(".conn-status.on")).toBeVisible();
    const input = page.locator(".chat-input-bar textarea");
    await input.fill("Use the complete evidence");
    const firstUpload = page.waitForResponse((response) => response.url().endsWith(`/sessions/${first.id}/attachments`) && response.request().method() === "POST");
    await page.getByTestId("attachment-file-input").setInputFiles({
      name: "evidence.txt", mimeType: "text/plain", buffer: Buffer.from("complete required evidence"),
    });
    const firstFile = await (await firstUpload).json();
    const secondUpload = page.waitForResponse((response) => response.url().endsWith(`/sessions/${second.id}/attachments`) && response.request().method() === "POST");
    await page.locator(".session-item", { hasText: second.name }).click();
    const secondFile = await (await secondUpload).json();
    expect(secondFile.id).not.toBe(firstFile.id);
    await expect(input).toHaveValue("Use the complete evidence");
    await expect(page.locator(".attachment-pending")).not.toContainText("uploading");

    // A forged mixed request must be rejected as a whole over the real socket.
    const refusal = await page.evaluate(async ({ sessionId, ids }) => {
      return await new Promise<Record<string, unknown>>((resolve, reject) => {
        const socket = new WebSocket(`${location.origin.replace(/^http/, "ws")}/ws?token=${encodeURIComponent(localStorage.getItem("octopus_token") ?? "")}`);
        const deadline = setTimeout(() => { socket.close(); reject(new Error("Attachment refusal not received")); }, 5000);
        socket.onopen = () => socket.send(JSON.stringify({ type: "send_message", session_id: sessionId,
          content: "Must not run with missing input", attachment_ids: ids, client_request_id: "foreign-file-input" }));
        socket.onmessage = (event) => {
          const value = JSON.parse(event.data);
          if (value.type !== "submission" || value.client_request_id !== "foreign-file-input") return;
          clearTimeout(deadline); socket.close(); resolve(value);
        };
      });
    }, { sessionId: second.id, ids: [secondFile.id, firstFile.id] });
    expect(refusal).toMatchObject({ accepted: false, code: "attachment_unavailable" });
    const before = await (await request.get(`${API}/sessions/${second.id}`, { headers })).json();
    expect(before.messages).toHaveLength(0);

    await page.getByRole("button", { name: "Send message" }).click();
    await expect(page.locator(".msg-user .attachment-chip", { hasText: "evidence.txt" })).toBeVisible();
    const saved = await (await request.get(`${API}/sessions/${second.id}`, { headers })).json();
    const submitted = saved.messages.find((message: { role: string }) => message.role === "user");
    expect(submitted.content).toBe("Use the complete evidence");
    expect(submitted.attachments).toMatchObject([{ id: secondFile.id }]);
    const bytes = await request.get(`${API}/sessions/${second.id}/attachments/${secondFile.id}`, { headers });
    expect(await bytes.text()).toBe("complete required evidence");
    expect((await request.get(`${API}/sessions/${second.id}/attachments/${firstFile.id}`, { headers })).status()).toBe(404);
  } finally {
    for (const session of sessions) await request.delete(`${API}/sessions/${session.id}`, { headers }).catch(() => {});
  }
});

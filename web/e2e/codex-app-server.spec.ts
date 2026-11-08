import { randomUUID } from "node:crypto";
import { test, expect } from "./test";
import { TEST_BEARER, enterAccountCredentials } from "./current-account";
import type { components } from "../src/api/contracts";

type Submission = components["schemas"]["ClientSubmissionStatus"];
type Input = components["schemas"]["ClientSubmissionRequest"];
const API = "http://localhost:8765/api";
const headers = { Authorization: `Bearer ${TEST_BEARER}` };
let sessionId: string | undefined;

test.afterAll(async ({ request }) => {
  if (sessionId) {
    expect((await request.delete(`${API}/sessions/${sessionId}`, { headers })).status()).toBe(204);
    expect((await request.get(`${API}/sessions/${sessionId}`, { headers })).status()).toBe(404);
  }
});

test("App Server preserves native history and original submissions across reload @llm", async ({ page, request }) => {
  test.setTimeout(180_000);
  const backends = await request.get(`${API}/backends`, { headers });
  expect(backends.ok()).toBeTruthy();
  expect((await backends.json()).available).toContain("codex");
  const name = `Native E2E ${randomUUID()}`;
  const phrase = `amber-${randomUUID()}`;
  const sent: Input[] = [];
  page.on("websocket", (socket) => socket.on("framesent", ({ payload }) => {
    const frame = JSON.parse(String(payload));
    if (frame.type === "send_message") {
      sent.push({ client_request_id: frame.client_request_id, content: frame.content,
        attachment_ids: frame.attachment_ids ?? [], wiki_mode: frame.wiki_mode ?? null });
    }
  }));
  await page.goto("/");
  await enterAccountCredentials(page);
  await expect(page.locator(".agent-list-header")).toBeVisible();
  await page.locator(".agent-item", { hasText: "Octo" }).locator(".btn-session-add").click();
  await page.locator(".btn-session-advanced").click();
  await page.locator(".session-backend-select").selectOption("codex");
  await page.locator('.session-create input[placeholder="Session name"]').fill(name);
  await page.locator("button.btn-create").click();
  await expect(page.locator(".chat-header .crumb-current")).toHaveText(name);
  const sessions = await (await request.get(`${API}/sessions`, { headers })).json();
  const session = sessions.find((session: { name: string }) => session.name === name);
  expect(session.backend).toBe("codex");
  sessionId = session.id;
  const settings = await request.patch(`${API}/sessions/${sessionId}`, {
    headers, data: { reasoning_effort: "low" },
  });
  expect(settings.ok()).toBeTruthy();
  expect((await settings.json()).reasoning_effort).toBe("low");

  async function send(content: string) {
    await page.locator(".chat-input-bar textarea").fill(content);
    const count = sent.length;
    await page.locator("button.btn-send").click();
    await expect.poll(() => sent.length).toBe(count + 1);
    const body = sent[count];
    const url = `${API}/sessions/${sessionId}/submissions/${body.client_request_id}`;
    await expect.poll(async () => {
      const status = await request.get(url, { headers });
      if (status.status() === 404) return "admitting";
      expect(status.ok()).toBeTruthy();
      return (await status.json() as Submission).execution.state;
    }, { timeout: 75_000 }).toBe("done");
    const final = await (await request.get(url, { headers })).json() as Submission;
    expect(final.outcome?.status).toBe("success");
    const nativeInputs = final.execution.native_inputs;
    expect(nativeInputs).toHaveLength(1);
    if (!nativeInputs || nativeInputs.length !== 1) throw new Error("Missing original native input evidence");
    const nativeInput = nativeInputs[0];
    expect(nativeInput).toMatchObject({
      transport: "codex-app-server", state: "accepted",
    });
    return { body, final, nativeInput };
  }

  const first = await send(`Remember the phrase ${phrase}. Reply with that exact phrase. Do not use any tools or helpers.`);
  await expect(page.locator(".msg-user .msg-content")).toContainText(phrase);
  await expect(page.locator(".msg-assistant .msg-content").last()).toContainText(phrase);
  await expect(page.locator(".result-badge")).toBeVisible();
  // This fresh thread has no earlier rollout or tool responses. Verify usage
  // and original-process retirement before reload or a second model request.
  let model = "";
  await expect.poll(async () => {
    const report = await request.get(`${API}/monitor/codex_turns?window=1h`, { headers });
    expect(report.ok()).toBeTruthy();
    const rows = (await report.json()).rows as {
      session_id: string; request_id: string; model: string; weighted_tps: number | null; output_tokens: number;
    }[];
    const sample = rows.find((row) => row.session_id === sessionId && row.request_id === first.final.request_id);
    model = sample?.model ?? "";
    if (!sample || sample.output_tokens <= 0) return 0;
    return sample.weighted_tps ?? 0;
  }, { timeout: 15_000 }).toBeGreaterThan(0);
  await expect.poll(async () => {
    const report = await request.get(`${API}/monitor/codex_runs?window=1h`, { headers });
    expect(report.ok()).toBeTruthy();
    const rows = (await report.json()).rows as {
      session_id: string; request_id: string; root_turns: number;
      metrics_unavailable: number; metrics_collection_complete: number; resource_state: string;
    }[];
    return rows.filter((row) => row.session_id === sessionId && row.request_id === first.final.request_id)
      .map(({ root_turns, metrics_unavailable, metrics_collection_complete, resource_state }) => ({
        root_turns, metrics_unavailable, metrics_collection_complete, resource_state,
      }));
  }, { timeout: 15_000 }).toEqual([{
    root_turns: 1, metrics_unavailable: 0, metrics_collection_complete: 1, resource_state: "proven",
  }]);
  const firstNative = first.nativeInput;
  const observed = await (await request.get(`${API}/sessions/${sessionId}`, { headers })).json() as components["schemas"]["SessionDetail"];
  expect(observed.context_state).toMatchObject({ validity: "valid", sample: {
    backend: "codex", native_session_id: firstNative.thread_id,
    source: { request_id: first.final.request_id, native_turn_id: firstNative.turn_id },
  } });
  expect(observed.context_state?.sample?.used_tokens).toBeGreaterThanOrEqual(0);
  await page.getByRole("button", { name: /^Context remaining: / }).click();
  const contextDetails = page.getByRole("dialog", { name: "Context details" });
  await expect(contextDetails).toBeVisible();
  await expect(contextDetails).toContainText("tokens");
  await page.keyboard.press("Escape");

  async function reloadOriginalSession() {
    await page.reload();
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await page.locator(".session-item", { hasText: name }).click();
    await expect(page.locator(".chat-header .crumb-current")).toHaveText(name);
  }

  await reloadOriginalSession();
  await expect(page.locator(".msg-assistant .msg-content").last()).toContainText(phrase);
  const restored = await (await request.get(`${API}/sessions/${sessionId}`, { headers })).json() as components["schemas"]["SessionDetail"];
  expect(restored.context_state).toEqual(observed.context_state);
  // A retry of the browser's original identity returns the original receipt.
  const retry = await request.post(`${API}/sessions/${sessionId}/submissions`, { headers, data: first.body });
  expect(retry.ok()).toBeTruthy();
  const repeated = await retry.json() as Submission;
  expect(repeated.request_id).toBe(first.final.request_id);
  // POST omits nulls; the typed GET materializes the optional start target.
  expect(repeated.execution.native_inputs).toHaveLength(1);
  if (!repeated.execution.native_inputs) throw new Error("Retry lost original native input evidence");
  expect(repeated.execution.native_inputs.map((input) => ({
    ...input, target_turn_id: input.target_turn_id ?? null,
  }))).toEqual(first.final.execution.native_inputs);

  const second = await send("Reply only with the phrase I asked you to remember. Do not use any tools or helpers.");
  const secondNative = second.nativeInput;
  expect(secondNative.thread_id).toBeTruthy();
  expect(secondNative.thread_id).toBe(firstNative.thread_id);
  expect(secondNative.turn_id).not.toBe(firstNative.turn_id);
  expect(secondNative.attempt_id).not.toBe(firstNative.attempt_id);
  expect(second.final.request_id).not.toBe(first.final.request_id);
  await expect(page.locator(".msg-assistant .msg-content").last()).toContainText(phrase);
  await reloadOriginalSession();
  await expect(page.locator(".msg-user .msg-content")).toHaveCount(2);
  await expect(page.locator(".msg-assistant .msg-content").last()).toContainText(phrase);
  const saved = await (await request.get(`${API}/sessions/${sessionId}`, { headers })).json();
  expect(saved.claude_session_id).toBe(firstNative.thread_id);

  // The first turn's actual model is also visible in Monitor after reopening.
  await page.locator("button.btn-manage-monitor").click();
  await expect(page.getByRole("row").filter({ has: page.getByRole("cell", { name: model, exact: true }) }).first())
    .toContainText("tok/s");
});

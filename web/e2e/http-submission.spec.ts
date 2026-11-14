import { accountToken, enterAccountCredentials } from "./current-account";
import { expect, test } from "./test";
import { systemAgentId } from "./system-agent";

const API = "http://localhost:8765/api";
const headers = { get Authorization() { return `Bearer ${accountToken()}`; } };

for (const noCrypto of [false, true]) {
  test(noCrypto ? "keeps the draft without submitting when Web Crypto is unavailable"
    : "submits over insecure HTTP and keeps the rejected draft", async ({ page, request }) => {
    const origin = "http://192.0.2.10:5174";
    // Serve the actual application at an insecure, non-loopback origin. All
    // network traffic stays on the isolated Playwright servers. Only the
    // destination changes; let the browser receive the response directly.
    await page.route(`${origin}/**`, async (route) => {
      await route.continue({ url: route.request().url().replace(origin, "http://localhost:5174") });
    });
    const submissions: Record<string, unknown>[] = [];
    await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => {
      socket.onMessage((message) => {
        const data = JSON.parse(String(message));
        if (data.type !== "send_message") return;
        submissions.push(data);
        socket.send(JSON.stringify({ type: "submission", session_id: data.session_id,
          client_request_id: data.client_request_id, accepted: false, message: "Session not found" }));
      });
    });
    const response = await request.post(`${API}/sessions`, { headers, data: { agent_id: await systemAgentId(request, `${API}/sessions`, headers), name: `HTTP draft ${noCrypto}`, } });
    expect(response.ok()).toBeTruthy();
    const session = await response.json();
    try {
      await page.goto(origin);
      expect(await page.evaluate(() => window.isSecureContext)).toBe(false);
      expect(await page.evaluate(() => typeof globalThis.crypto?.randomUUID)).toBe("undefined");
      await enterAccountCredentials(page);
      await expect(page.locator(".agent-list-header")).toBeVisible();
      await page.locator(".agent-item", { hasText: "Octo" }).click();
      await page.locator(".session-item", { hasText: session.name }).click();
      await expect(page.locator(".conn-status.on")).toBeVisible();
      // Vite's development client itself uses Web Crypto; remove the API
      // after it loads to exercise the product's two submission entry points.
      if (noCrypto) await page.evaluate(() => Object.defineProperty(globalThis, "crypto", { value: undefined }));
      const input = page.locator(".chat-input-bar textarea");
      await input.fill("keep my HTTP draft");
      await page.getByRole("button", { name: "Send message" }).click();
      if (noCrypto) {
        await expect(page.getByRole("alert")).toContainText("Secure randomness is unavailable");
        expect(submissions).toHaveLength(0);
      } else {
        await expect(page.getByRole("alert")).toContainText("Session not found");
        expect(submissions).toHaveLength(1);
        expect(submissions[0].client_request_id).toEqual(expect.any(String));
        await page.getByRole("button", { name: "Restore draft" }).click();
      }
      await expect(input).toHaveValue("keep my HTTP draft");
      await expect(page.locator(".agent-list-header")).toBeVisible();
    } finally {
      await page.unrouteAll({ behavior: "wait" });
      await request.delete(`${API}/sessions/${session.id}`, { headers });
    }
  });
}

import { randomUUID } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import type { Locator } from "@playwright/test";

import { accountToken, enterAccountCredentials } from "./current-account";
import { expect, test } from "./test";

const API = "http://localhost:8765/api";
const headers = { get Authorization() { return `Bearer ${accountToken()}`; } };

async function renderedMath(root: Locator) {
  await expect(root.locator("math")).toHaveCount(2);
  await expect(root.locator("annotation")).toHaveText(["x^2", String.raw`\frac{1}{2}`]);
  await expect(root.locator(".katex-html").first()).toBeVisible();
  const display = root.locator(".katex-display");
  await expect(display).toBeVisible();
  const box = await display.boundingBox();
  expect(box?.height).toBeGreaterThan(15);
  expect(box?.width).toBeGreaterThan(20);
}

test("renders math and local fonts in chat and Markdown file preview", async ({ page, request }) => {
  const workspace = await request.get(`${API}/auth/workspace`, { headers });
  expect(workspace.ok()).toBeTruthy();
  const directory = mkdtempSync(path.join((await workspace.json()).workspace, "math-e2e-"));
  const content = "Before $x^2$ after.\n\n$$\n\\frac{1}{2}\n$$";
  writeFileSync(path.join(directory, "equations.md"), content);
  let sessionId: string | undefined;
  try {
    const name = `Math ${randomUUID()}`;
    const imported = await request.post(`${API}/sessions/import`, { headers, data: {
      name, working_dir: directory, messages: [{ role: "assistant", type: "text", content }],
    } });
    expect(imported.ok()).toBeTruthy();
    sessionId = (await imported.json()).id;
    await page.goto("/");
    await enterAccountCredentials(page);
    await expect(page.locator(".agent-list-header")).toBeVisible();
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await page.locator(".session-item .session-name", { hasText: name }).click();
    await renderedMath(page.locator(".msg-assistant .msg-content"));

    // Isolate model-based filename resolution; the actual file/meta reads,
    // Markdown renderer, CSS and font requests still run through the product.
    await page.route(`**/api/sessions/${sessionId}/showme/resolve`, (route) =>
      route.fulfill({ json: { path: "equations.md" } }));
    await page.locator(".chat-input-bar textarea").fill("/showme equations.md");
    await page.locator("button.btn-send").click();
    const dialog = page.getByRole("dialog");
    await renderedMath(dialog.locator("article"));
    const fonts = await page.evaluate(async () => {
      await document.fonts.ready;
      return Array.from(document.fonts, (font) => ({ family: font.family, status: font.status }));
    });
    expect(fonts.some((font) => font.family.startsWith("KaTeX_") && font.status === "loaded")).toBe(true);
  } finally {
    if (sessionId) expect((await request.delete(`${API}/sessions/${sessionId}`, { headers })).status()).toBe(204);
    rmSync(directory, { recursive: true, force: true });
  }
});

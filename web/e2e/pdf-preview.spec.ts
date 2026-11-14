import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { accountToken, enterAccountCredentials } from "./current-account";
import { expect, test, type APIRequestContext, type Page } from "./test";

const API = "http://localhost:8765/api";
const headers = { get Authorization() { return `Bearer ${accountToken()}`; } };
const document = readFileSync(new URL("../../tests/fixtures/viewer.pdf", import.meta.url));
const filename = "指南 %.pdf";

test.use({ channel: "chromium" });

async function withPdf(page: Page, request: APIRequestContext, inspect: () => Promise<void>) {
  const workspace = await request.get(`${API}/auth/workspace`, { headers });
  expect(workspace.ok()).toBeTruthy();
  const directory = mkdtempSync(path.join((await workspace.json()).workspace, "pdf-preview-"));
  writeFileSync(path.join(directory, filename), document);
  let sessionId: string | undefined;
  try {
    const name = path.basename(directory);
    const created = await request.post(`${API}/sessions/import`, { headers, data: {
      name, working_dir: directory, messages: [{ role: "user", type: "text", content: "PDF viewer" }],
    } });
    expect(created.ok()).toBeTruthy();
    sessionId = (await created.json()).id;
    await page.goto("/");
    await enterAccountCredentials(page);
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await page.locator(".session-item .session-name", { hasText: name }).click();
    // Only model filename resolution is substituted; file metadata, auth,
    // bytes, headers and browser PDF handling remain real.
    await page.route(`**/api/sessions/${sessionId}/showme/resolve`, route => route.fulfill({ json: { path: filename } }));
    await page.locator(".chat-input-bar textarea").fill(`/showme ${filename}`);
    await page.locator("button.btn-send").click();
    await expect(page.getByRole("dialog", { name: filename })).toBeVisible();
    await inspect();
  } finally {
    if (sessionId) expect((await request.delete(`${API}/sessions/${sessionId}`, { headers })).status()).toBe(204);
    rmSync(directory, { recursive: true, force: true });
  }
}

async function downloadPdf(page: Page) {
  const pending = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download file" }).click();
  const download = await pending;
  expect(download.suggestedFilename()).toBe(filename);
  const saved = await download.path();
  expect(saved).not.toBeNull();
  expect(readFileSync(saved!)).toEqual(document);
}

test("PDF renders inline in Chromium and explicit Download saves the original name and bytes", async ({ page, request }, info) => {
  await withPdf(page, request, async () => {
    expect(await page.evaluate(() => navigator.pdfViewerEnabled)).toBe(true);
    const iframe = page.getByTitle("PDF preview");
    await expect(iframe).toBeVisible();
    const src = (await iframe.getAttribute("src"))!;
    expect(new URL(src).searchParams.get("preview")).toBe("true");
    const response = await request.get(src);
    expect(response.status()).toBe(200);
    expect(response.headers()["content-disposition"]).toMatch(/^inline;/);
    expect(await response.body()).toEqual(document);
    await expect.poll(() => page.frames().some(frame => frame.url().startsWith("chrome-extension://") && frame.url().endsWith("/index.html"))).toBe(true);
    const native = page.frames().find(frame => frame.url().startsWith("chrome-extension://") && frame.url().endsWith("/index.html"))!;
    await expect(native.locator("pdf-viewer")).toBeVisible();
    await expect(native.locator("viewer-toolbar")).not.toHaveAttribute("loading_");
    await page.screenshot({ path: info.outputPath("pdf-preview.png") });
    await downloadPdf(page);
  });
});

const withoutPdf = test.extend({
  context: async ({ playwright }, provideContext) => {
    const profile = mkdtempSync(path.join(os.tmpdir(), "octopus-pdf-disabled-"));
    mkdirSync(path.join(profile, "Default"));
    writeFileSync(path.join(profile, "Default", "Preferences"), JSON.stringify({ plugins: { always_open_pdf_externally: true } }));
    const context = await playwright.chromium.launchPersistentContext(profile, { channel: "chromium", headless: true, serviceWorkers: "block" });
    try {
      await provideContext(context);
    } finally {
      await context.close();
      rmSync(profile, { recursive: true, force: true });
    }
  },
});

withoutPdf("disabled native PDF handling retains an explicit open and download", async ({ page, request }) => {
  await withPdf(page, request, async () => {
    expect(await page.evaluate(() => navigator.pdfViewerEnabled)).toBe(false);
    await expect(page.getByText(/PDF preview is unavailable/)).toBeVisible();
    await expect(page.getByTitle("PDF preview")).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Open in new tab" })).toBeVisible();
    const opened = page.waitForEvent("download");
    await page.getByRole("link", { name: "Open in new tab" }).click();
    const file = await opened;
    expect(file.suggestedFilename()).toBe(filename);
    expect(readFileSync((await file.path())!)).toEqual(document);
    await downloadPdf(page);
  });
});

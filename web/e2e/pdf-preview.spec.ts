import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import type { ImportSessionRequest } from "../src/api";
import { editExtraRoots } from "./account-resources";
import { accountToken, enterAccountCredentials, openAsCurrentAccount } from "./current-account";
import { expect, test, type APIRequestContext, type Page } from "./test";

const API = "http://localhost:8765/api";
const headers = { get Authorization() { return `Bearer ${accountToken()}`; } };
const document = readFileSync(new URL("../../tests/fixtures/viewer.pdf", import.meta.url));
const filename = "指南 %.pdf";

test.use({ channel: "chromium" });

async function expectNativePdf(page: Page) {
  const nativeFrame = () => page.frames().find(frame => frame.url().startsWith("chrome-extension://") && frame.url().endsWith("/index.html"));
  await expect.poll(() => !!nativeFrame()).toBe(true);
  const native = nativeFrame()!;
  await expect(native.locator("pdf-viewer")).toBeVisible();
  await expect(native.locator("viewer-toolbar")).not.toHaveAttribute("loading_");
}

test("one workspace chat opens tool deliveries from its workspace and an authorized repository", async ({ page, request, account }) => {
  if (!account) throw new Error("The file source story requires an offline account");
  const setExtraRoots = await editExtraRoots(request, account.user_id);
  const workspace = await request.get(`${API}/auth/workspace`, { headers });
  expect(workspace.ok()).toBeTruthy();
  const repo = mkdtempSync(path.join(os.tmpdir(), "viewer-repo-"));
  const directory = mkdtempSync(path.join((await workspace.json()).workspace, "file-sources-"));
  try {
    await setExtraRoots([repo]);
    const workspaceReport = path.join(directory, "report.md");
    const repoReport = path.join(repo, "team", "report.md");
    const repoImage = path.join(repo, "logo.svg");
    const repoPdf = path.join(repo, "report.pdf");
    mkdirSync(path.dirname(repoReport));
    writeFileSync(workspaceReport, "# Workspace report");
    writeFileSync(repoReport, "# Repository report\n\n![Repository diagram](../logo.svg)\n\n[Appendix](appendix.md)");
    writeFileSync(path.join(repo, "team", "appendix.md"), "# Repository appendix");
    writeFileSync(repoImage, '<svg xmlns="http://www.w3.org/2000/svg" width="13" height="8"><rect width="13" height="8"/></svg>');
    writeFileSync(repoPdf, document);
    writeFileSync(path.join(directory, "report.pdf"), Buffer.concat([document, Buffer.from("\n% workspace document\n")]));

    const messages: ImportSessionRequest["messages"] = [workspaceReport, repoReport, repoImage, repoPdf].map((savedPath, index) => ({
      role: "tool", type: "tool_result", content: `Generated file saved at: ${savedPath}`, attachments: [],
      tool_input: { native_item: { type: "imageGeneration", status: "completed", savedPath } },
      runtime_metadata: { request_id: "file-deliveries", attempt_id: "delivery-attempt", native: {
        thread_id: "delivery-thread", turn_id: "delivery-turn", item_id: `delivery-${index}`,
        root_thread_id: "delivery-thread", root_turn_id: "delivery-turn",
      } },
    }));
    const name = "Chat with tool deliveries";
    const created = await request.post(`${API}/sessions/import`, { headers, data: { name, working_dir: directory, messages } });
    expect(created.status()).toBe(201);
    const files = `/api/sessions/${(await created.json()).id}/files`;
    await openAsCurrentAccount(page);
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await page.locator(".session-item").getByText(name, { exact: true }).click();
    const chatUrl = page.url();
    const dialog = page.getByRole("dialog");
    const closeViewer = () => dialog.getByRole("button", { name: "Close", exact: true }).click();
    const openDelivery = async (target: string) => {
      const action = page.locator(".msg-tool-result", { has: page.getByText(target, { exact: true }) })
        .getByRole("button", { name: "Open in viewer" });
      await expect(action).toBeVisible();
      await action.click();
    };
    const openPositioned = async (target: string) => {
      await page.locator(".chat-input-bar textarea").fill(`/showme ${JSON.stringify(target)}:12`);
      await page.locator("button.btn-send").click();
      await expect(dialog.getByRole("note")).toHaveText("Text positions aren't available for this file type.");
    };
    const expectSource = (url: URL, source: string) => {
      expect(url.pathname).toBe(files);
      expect(url.searchParams.get("source")).toBe(source);
    };

    await openDelivery(workspaceReport);
    await expect(dialog.getByRole("heading", { name: "Workspace report" })).toBeVisible();
    expectSource(new URL((await dialog.getByRole("link", { name: "Download file" }).getAttribute("href"))!), directory);
    await closeViewer();

    await openDelivery(repoReport);
    await expect(dialog.getByRole("heading", { name: "Repository report" })).toBeVisible();
    const diagram = dialog.getByRole("img", { name: "Repository diagram" });
    await expect.poll(() => diagram.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(13);
    const diagramUrl = new URL((await diagram.getAttribute("src"))!);
    expectSource(diagramUrl, repo);
    expect(diagramUrl.searchParams.get("source_directory")).toBe("team");
    await dialog.getByRole("link", { name: "Appendix" }).click();
    await expect(dialog.getByRole("heading", { name: "Repository appendix" })).toBeVisible();
    await closeViewer();

    const imageRead = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === files && url.searchParams.get("path") === "logo.svg";
    });
    await openDelivery(repoImage);
    const image = dialog.getByRole("img", { name: "logo.svg" });
    await expect.poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(13);
    const imageUrl = new URL((await imageRead).url());
    expectSource(imageUrl, repo);
    expect(imageUrl.searchParams.get("canonical_path_id")).toBeTruthy();
    // SVG stays opaque even if a browser opens the displayed image as a document.
    const displayedSvg = (await image.getAttribute("src"))!;
    expect(displayedSvg).toMatch(/^data:image\/svg\+xml;base64,/);
    const imageDocument = await page.context().newPage();
    try {
      await imageDocument.goto(displayedSvg);
      expect(await imageDocument.evaluate(() => window.origin)).toBe("null");
    } finally {
      await imageDocument.close();
    }
    await dialog.getByRole("button", { name: "Zoom in" }).click();
    await expect(dialog.getByRole("button", { name: "Zoom out" })).toBeVisible();
    await dialog.getByRole("button", { name: "Zoom out" }).click();
    await expect(dialog.getByRole("link", { name: "Open in new tab" })).toHaveCount(0);
    const svgDownload = page.waitForEvent("download");
    await dialog.getByRole("link", { name: "Download file" }).click();
    const svgAttachment = await svgDownload;
    expect(svgAttachment.suggestedFilename()).toBe("logo.svg");
    expect(readFileSync((await svgAttachment.path())!)).toEqual(readFileSync(repoImage));
    await closeViewer();

    await openPositioned(repoImage);
    await expect.poll(() => dialog.getByRole("img", { name: "logo.svg" }).evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(13);
    await closeViewer();

    await openDelivery(repoPdf);
    const iframe = dialog.getByTitle("PDF preview");
    await expect(iframe).toBeVisible();
    await expectNativePdf(page);
    await expect(dialog.getByRole("status")).toHaveCount(0);
    const pdfUrl = new URL((await iframe.getAttribute("src"))!);
    expectSource(pdfUrl, repo);
    expect(pdfUrl.searchParams.get("canonical_path_id")).toBeTruthy();
    const range = await request.get(pdfUrl.href, { headers: { Range: "bytes=0-7" } });
    expect(range.status()).toBe(206);
    expect(await range.body()).toEqual(document.subarray(0, 8));
    expect(range.headers()["cache-control"]).toBe("no-store");
    const downloadLink = dialog.getByRole("link", { name: "Download file" });
    const downloadUrl = new URL((await downloadLink.getAttribute("href"))!);
    expectSource(downloadUrl, repo);
    expect(downloadUrl.searchParams.get("canonical_path_id")).toBe(pdfUrl.searchParams.get("canonical_path_id"));
    const downloading = page.waitForEvent("download");
    await downloadLink.click();
    const download = await downloading;
    expect(download.suggestedFilename()).toBe("report.pdf");
    expect(readFileSync((await download.path())!)).toEqual(document);
    await closeViewer();

    await openPositioned(repoPdf);
    await expect(iframe).toBeVisible();
    await expectNativePdf(page);

    await setExtraRoots([]);
    for (const url of [diagramUrl, imageUrl, pdfUrl, downloadUrl]) {
      const denied = await request.get(url.href, { headers });
      expect(denied.status()).toBe(403);
      const error = { detail: "The file source is no longer available." };
      expect(await denied.json()).toEqual(url === pdfUrl ? { ...error, status: 403 } : error);
    }
    await closeViewer();

    await setExtraRoots([repo]);
    await page.evaluate(() => {
      performance.clearResourceTimings();
      performance.setResourceTimingBufferSize(0);
    });
    const isInlineRead = (url: URL) => url.pathname === files && url.searchParams.get("preview") === "true";
    let resumeRead!: () => void;
    const readMayContinue = new Promise<void>((resolve) => { resumeRead = resolve; });
    await page.route(isInlineRead, async (route) => {
      await readMayContinue;
      await route.continue();
    }, { times: 1 });
    const deniedRead = page.waitForResponse((response) => isInlineRead(new URL(response.url())));
    await openDelivery(repoPdf);
    try {
      await expect(dialog.getByRole("status")).toHaveText("Loading…");
      await setExtraRoots([]);
    } finally {
      resumeRead();
    }
    expect((await deniedRead).status()).toBe(403);
    await expect(dialog.getByRole("alert")).toHaveText("You don't have access to this file.");
    await expect(dialog.getByRole("button", { name: "Edit path" })).toBeVisible();
    await closeViewer();

    await setExtraRoots([repo]);
    await page.route(isInlineRead, (route) => route.abort("failed"), { times: 1 });
    await openDelivery(repoPdf);
    await expect(dialog.getByRole("alert")).toHaveText(/Check your connection and retry/);
    await expect(dialog.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
    await closeViewer();

    await page.clock.install();
    const nativeReads: string[] = [];
    page.on("request", (req) => {
      if (isInlineRead(new URL(req.url()))) nativeReads.push(req.url());
    });
    const retry = dialog.getByRole("button", { name: "Retry", exact: true });
    const unfinished = dialog.getByText("The PDF preview has not finished loading.");
    let releaseStalled!: () => void;
    const stalled = new Promise<void>((resolve) => { releaseStalled = resolve; });
    await page.route(isInlineRead, async (route) => {
      await stalled;
      await route.abort("aborted").catch(() => {});
    }, { times: 1 });
    try {
      await openDelivery(repoPdf);
      await expect(dialog.getByRole("status")).toHaveText("Loading…");
      await expect(iframe).toBeAttached();
      await expect.poll(() => nativeReads.length).toBe(1);
      await page.clock.fastForward(5_001);
      await expect(unfinished).toBeVisible();
      expect(nativeReads).toHaveLength(1);
      await retry.click();
      await expect(unfinished).toHaveCount(0);
      await expectNativePdf(page);
    } finally {
      releaseStalled();
    }
    await closeViewer();

    nativeReads.length = 0;
    let accepted!: (status: number) => void;
    const acceptedRead = new Promise<number>((resolve) => { accepted = resolve; });
    let deliver!: () => void;
    const delivery = new Promise<void>((resolve) => { deliver = resolve; });
    await page.route(isInlineRead, async (route) => {
      const response = await route.fetch();
      accepted(response.status());
      await delivery;
      await route.fulfill({ response });
    }, { times: 1 });
    try {
      await openDelivery(repoPdf);
      expect(await acceptedRead).toBe(200);
      await setExtraRoots([]);
      expect((await request.get(pdfUrl.href, { headers: { Range: "bytes=0-0" } })).status()).toBe(403);
      await page.clock.fastForward(5_001);
      await expect(unfinished).toBeVisible();
      await expect(retry).toBeVisible();
      expect(nativeReads).toHaveLength(1);
      await expect(iframe).toBeAttached();
    } finally {
      deliver();
    }
    await expectNativePdf(page);
    await expect(unfinished).toHaveCount(0);
    await closeViewer();

    await openDelivery(workspaceReport);
    await expect(dialog.getByRole("heading", { name: "Workspace report" })).toBeVisible();
    expect(page.url()).toBe(chatUrl);
  } finally {
    rmSync(directory, { recursive: true, force: true });
    rmSync(repo, { recursive: true, force: true });
  }
});

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
    await expectNativePdf(page);
    await page.screenshot({ path: info.outputPath("pdf-preview.png") });
    await downloadPdf(page);
    await page.setViewportSize({ width: 320, height: 520 });
    const viewer = page.getByRole("dialog");
    await viewer.getByRole("button", { name: "File actions" }).click();
    const open = viewer.getByRole("menuitem", { name: "Open in new tab" });
    const preview = new URL(src);
    preview.searchParams.delete("frame");
    await expect(open).toHaveAttribute("href", preview.href);
    await expect(open).toHaveAttribute("target", "_blank");
    const download = new URL((await viewer.getByRole("menuitem", { name: "Download file" }).getAttribute("href"))!);
    expect(download.searchParams.has("preview")).toBe(false);
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

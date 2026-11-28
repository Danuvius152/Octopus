import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import type { FrameLocator, Locator } from "@playwright/test";

import type { ImportSessionRequest } from "../src/api";
import { editExtraRoots } from "./account-resources";
import { accountToken, enterAccountCredentials, openAsCurrentAccount } from "./current-account";
import { expect, test, type APIRequestContext, type Page } from "./test";

const API = "http://localhost:8765/api";
const headers = { get Authorization() { return `Bearer ${accountToken()}`; } };
const document = readFileSync(new URL("../../tests/fixtures/viewer.pdf", import.meta.url));
const filename = "指南 %.pdf";

test.use({ channel: "chromium" });

const withoutNativePdf = test.extend({
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

function pdfWithPages(...labels: string[]) {
  return pdfWithRotation(labels, 0);
}

function pdfWithRotation(labels: string[], rotation: number) {
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    `<< /Type /Pages /Count ${labels.length} /Kids [${labels.map((_, index) => `${4 + index * 2} 0 R`).join(" ")}] >>`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ];
  for (const [index, label] of labels.entries()) {
    const stream = label ? `BT /F1 22 Tf 45 180 Td (${label}) Tj ET\n` : "";
    objects.push(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 320 240] /Rotate ${rotation} /Resources << /Font << /F1 3 0 R >> >> /Contents ${5 + index * 2} 0 R >>`);
    objects.push(`<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}endstream`);
  }
  return pdfFromObjects(objects);
}

function pdfFromObjects(objects: string[]) {
  let pdf = "%PDF-1.4\n";
  const offsets: string[] = [];
  for (const [index, object] of objects.entries()) {
    offsets.push(`${String(Buffer.byteLength(pdf)).padStart(10, "0")} 00000 n \n`);
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
  }
  return Buffer.from(`${pdf}xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.join("")}` +
    `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${Buffer.byteLength(pdf)}\n%%EOF\n`);
}

const drawScan = "q 320 0 0 240 0 0 cm /Scan Do Q\n";

function scanImage(damaged = false) {
  const image = damaged ? "/Filter /DCTDecode /Length 12 >>\nstream\ninvalid JPEG"
    : "/Filter /ASCIIHexDecode /Length 7 >>\nstream\n00FF00>";
  const dimensions = damaged ? "/Width 300 /Height 300" : "/Width 1 /Height 1";
  return `<< /Type /XObject /Subtype /Image /ColorSpace /DeviceRGB /BitsPerComponent 8 ${dimensions} ${image}\nendstream`;
}

function pdfWithScan({ damaged = false } = {}) {
  const content = drawScan + "0 0 1 rg 24 24 32 32 re f\nBT /F1 22 Tf 45 180 Td (Visible page text) Tj ET\n";
  return pdfFromObjects([
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 320 240] /Resources << /XObject << /Scan 5 0 R >> /Font << /F1 6 0 R >> >> /Contents 4 0 R >>",
    `<< /Length ${content.length} >>\nstream\n${content}endstream`,
    scanImage(damaged),
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ]);
}

function pdfWithStamp() {
  return pdfFromObjects([
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 320 240] /Resources << >> /Annots [4 0 R] >>",
    "<< /Type /Annot /Subtype /Stamp /Rect [0 0 320 240] /F 4 /AP << /N 5 0 R >> >>",
    `<< /Type /XObject /Subtype /Form /BBox [0 0 320 240] /Resources << /XObject << /Scan 6 0 R >> >> /Length ${drawScan.length} >>\nstream\n${drawScan}endstream`,
    scanImage(),
  ]);
}

const isPdfPreviewRead = (url: URL) => url.pathname.endsWith("/files") &&
  url.searchParams.get("path") === filename && url.searchParams.get("preview") === "true";

async function expectPdf(dialog: Locator, text = "PDF preview ready") {
  const reader = dialog.getByTitle("PDF preview").contentFrame();
  await expect(reader.getByText(text, { exact: true })).toBeVisible();
  await expect(reader.locator("canvas")).toBeVisible();
  return reader;
}

function failPdfCanvasDraw(frame: FrameLocator, pageNumber: number) {
  return frame.locator("body").evaluate((_, failedPage) => {
    const getContext = HTMLCanvasElement.prototype.getContext;
    Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
      value(this: HTMLCanvasElement, ...args: unknown[]) {
        if (this.closest(`[aria-label="PDF page ${failedPage}"]`)) throw new Error("Canvas drawing unavailable");
        return Reflect.apply(getContext, this, args);
      },
    });
  }, pageNumber);
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
    await dialog.getByRole("button", { name: "Open file: Appendix", exact: true }).press("Enter");
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

    const isInlineRead = (url: URL) => url.pathname === files && url.searchParams.get("preview") === "true";
    const pdfRead = page.waitForResponse((response) => isInlineRead(new URL(response.url())));
    await openDelivery(repoPdf);
    const iframe = dialog.getByTitle("PDF preview");
    await expect(iframe).toBeVisible();
    await expectPdf(dialog);
    await expect(dialog.getByRole("status")).toHaveCount(0);
    const authenticatedRead = await pdfRead;
    const pdfUrl = new URL(authenticatedRead.url());
    expectSource(pdfUrl, repo);
    expect(pdfUrl.searchParams.get("canonical_path_id")).toBeTruthy();
    expect(pdfUrl.searchParams.has("token")).toBe(false);
    const range = await request.get(pdfUrl.href, { headers: { ...headers, Range: "bytes=0-7" } });
    expect(range.status()).toBe(206);
    expect(await range.body()).toEqual(document.subarray(0, 8));
    expect(range.headers()["cache-control"]).toBe("no-store");
    expect(range.headers()["etag"]).toBeTruthy();
    expect(range.headers()["etag"]).toBe(authenticatedRead.headers()["etag"]);
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
    await expectPdf(dialog);

    await setExtraRoots([]);
    for (const url of [diagramUrl, imageUrl, pdfUrl, downloadUrl]) {
      const denied = await request.get(url.href, { headers });
      expect(denied.status()).toBe(403);
      const error = { detail: "The file source is no longer available." };
      expect(await denied.json()).toEqual(error);
    }
    await closeViewer();

    await setExtraRoots([repo]);
    await page.evaluate(() => {
      performance.clearResourceTimings();
      performance.setResourceTimingBufferSize(0);
    });
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
    await expect(dialog.getByRole("link", { name: "Download original PDF" })).toHaveCount(0);
    await expect(dialog.getByRole("button", { name: "Edit path" })).toBeVisible();
    await closeViewer();

    await setExtraRoots([repo]);
    await page.route(isInlineRead, (route) => route.abort("failed"), { times: 1 });
    await openDelivery(repoPdf);
    await expect(dialog.getByRole("alert")).toHaveText(/Check your connection and retry/);
    await expect(dialog.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
    await closeViewer();

    await page.clock.install();
    const previewReads: string[] = [];
    page.on("request", (req) => {
      if (isInlineRead(new URL(req.url()))) previewReads.push(req.url());
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
      await expect.poll(() => previewReads.length).toBe(1);
      await page.clock.fastForward(5_001);
      await expect(unfinished).toBeVisible();
      expect(previewReads).toHaveLength(1);
      await retry.click();
      await expect(unfinished).toHaveCount(0);
      await expectPdf(dialog);
    } finally {
      releaseStalled();
    }
    await closeViewer();

    previewReads.length = 0;
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
      expect((await request.get(pdfUrl.href, { headers: { ...headers, Range: "bytes=0-0" } })).status()).toBe(403);
      await page.clock.fastForward(5_001);
      await expect(unfinished).toBeVisible();
      await expect(retry).toBeVisible();
      expect(previewReads).toHaveLength(1);
      await expect(iframe).toBeAttached();
    } finally {
      deliver();
    }
    await expectPdf(dialog);
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

async function withPdf(page: Page, request: APIRequestContext, inspect: (pdfPath: string) => Promise<void>, {
  fromMarkdown = false, bytes = document,
}: { fromMarkdown?: boolean; bytes?: Buffer } = {}) {
  const workspace = await request.get(`${API}/auth/workspace`, { headers });
  expect(workspace.ok()).toBeTruthy();
  const directory = mkdtempSync(path.join((await workspace.json()).workspace, "pdf-preview-"));
  const pdfPath = path.join(directory, filename);
  writeFileSync(pdfPath, bytes);
  if (fromMarkdown) {
    writeFileSync(path.join(directory, "guide.md"), [
      "# Original guide", "[Read the PDF](%E6%8C%87%E5%8D%97%20%25.pdf)", "[Read later](later.md)",
      ...Array.from({ length: 40 }, (_, index) => `Guide paragraph ${index + 1}`),
    ].join("\n\n"));
    writeFileSync(path.join(directory, "later.md"), "# Later text");
  }
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
    const target = fromMarkdown ? "guide.md" : filename;
    await page.locator(".chat-input-bar textarea").fill(`/showme ${target}`);
    await page.locator("button.btn-send").click();
    await expect(page.getByRole("dialog", { name: target })).toBeVisible();
    await inspect(pdfPath);
  } finally {
    if (sessionId) expect((await request.delete(`${API}/sessions/${sessionId}`, { headers })).status()).toBe(204);
    rmSync(directory, { recursive: true, force: true });
  }
}

withoutNativePdf("an unreadable PDF opened from Markdown leaves the original text active", async ({ page, request }) => {
  const damaged = Buffer.from("%PDF-1.7\nIncomplete PDF.\n");
  await withPdf(page, request, async (pdfPath) => {
    const reader = page.getByRole("dialog");
    await reader.getByRole("button", { name: "Open file: Read the PDF" }).click();
    await expect(reader.getByRole("alert")).toHaveText(/Couldn't load the preview/);
    await expect(reader.getByRole("heading", { name: "Original guide" })).toBeVisible();
    await expect(reader.getByRole("tab", { name: "guide.md" })).toHaveAttribute("aria-selected", "true");
    await expect(reader.getByTitle("PDF preview")).toHaveCount(0);
    const open = reader.getByRole("link", { name: "Open original PDF" });
    await expect(open).toHaveAttribute("target", "_blank");
    const openUrl = new URL((await open.getAttribute("href"))!);
    expect(openUrl.searchParams.get("preview")).toBe("true");
    const inline = await request.get(openUrl.href);
    expect(inline.status()).toBe(200);
    expect(inline.headers()["content-disposition"]).toMatch(/^inline;/);
    expect(await inline.body()).toEqual(damaged);
    const download = page.waitForEvent("download");
    const downloadLink = reader.getByRole("link", { name: "Download original PDF" });
    await downloadLink.click();
    const saved = await download;
    expect(saved.suggestedFilename()).toBe(filename);
    expect(readFileSync((await saved.path())!)).toEqual(damaged);
    const downloadUrl = new URL((await downloadLink.getAttribute("href"))!);
    expect(downloadUrl.searchParams.has("preview")).toBe(false);
    writeFileSync(pdfPath, Buffer.alloc(2 * 1024 * 1024 + 1));
    for (const url of [openUrl, downloadUrl]) {
      expect((await request.get(url.href)).status()).toBe(413);
      url.searchParams.delete("token");
      expect((await request.get(url.href)).status()).toBe(401);
    }
  }, { fromMarkdown: true, bytes: damaged });
});

withoutNativePdf("a first-page drawing failure leaves Markdown readable", async ({ page, request }) => {
  await withPdf(page, request, async () => {
    const reader = page.getByRole("dialog");
    let release!: () => void;
    const paused = new Promise<void>((resolve) => { release = resolve; });
    await page.route(isPdfPreviewRead, async (route) => {
      await paused;
      await route.continue();
    }, { times: 1 });
    try {
      await reader.getByRole("button", { name: "Open file: Read the PDF" }).click();
      const frame = reader.getByTitle("PDF preview");
      await expect(frame).toHaveAttribute("inert", "");
      await failPdfCanvasDraw(frame.contentFrame(), 1);
      await expect(reader.getByRole("heading", { name: "Original guide" })).toBeVisible();
    } finally {
      release();
    }
    await expect(reader.getByRole("alert")).toContainText("Couldn't draw the first PDF page.");
    await expect(reader.getByRole("tab", { name: "guide.md" })).toHaveAttribute("aria-selected", "true");
    await expect(reader.getByRole("heading", { name: "Original guide" })).toBeVisible();
    await expect(reader.getByTitle("PDF preview")).toHaveCount(0);
    await expect(reader.getByRole("link", { name: "Open original PDF" })).toBeVisible();
    await expect(reader.getByRole("link", { name: "Download original PDF" })).toBeVisible();
  }, { fromMarkdown: true });
});

test("a transient linked PDF read failure keeps the original file actions available", async ({ page, request }) => {
  await withPdf(page, request, async () => {
    const reader = page.getByRole("dialog");
    await page.route(isPdfPreviewRead, (route) => route.fulfill({
      status: 503, json: { detail: "PDF bytes temporarily unavailable." },
    }), { times: 1 });
    await reader.getByRole("button", { name: "Open file: Read the PDF" }).click();
    await expect(reader.getByRole("alert")).toContainText("PDF bytes temporarily unavailable.");
    await expect(reader.getByRole("heading", { name: "Original guide" })).toBeVisible();
    await expect(reader.getByRole("link", { name: "Open original PDF" })).toBeVisible();
    await expect(reader.getByRole("link", { name: "Download original PDF" })).toBeVisible();
  }, { fromMarkdown: true });
});

test("a PDF canvas stays readable when its selectable text stream fails", async ({ page, request }) => {
  await page.addInitScript(() => {
    const postMessage = Worker.prototype.postMessage;
    Object.defineProperty(Worker.prototype, "postMessage", {
      value(this: Worker, message: { action?: string }, ...rest: unknown[]) {
        if (message.action === "GetTextContent") {
          window.document.documentElement.dataset.pdfTextStreamBlocked = "true";
          throw new Error("PDF text stream unavailable");
        }
        return Reflect.apply(postMessage, this, [message, ...rest]);
      },
    });
  });
  await withPdf(page, request, async () => {
    const reader = page.getByRole("dialog");
    await reader.getByRole("button", { name: "Open file: Read the PDF" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-pdf-text-stream-blocked", "true");
    await expect(reader.getByRole("tab", { name: filename })).toHaveAttribute("aria-selected", "true");
    const canvas = reader.getByTitle("PDF preview").contentFrame().locator("canvas");
    await expect(canvas).toBeVisible();
    const pixel = await canvas.evaluate((node: HTMLCanvasElement) =>
      [...node.getContext("2d")!.getImageData(node.width / 2, node.height * .6, 1, 1).data]);
    expect(pixel).toEqual([0, 153, 0, 255]);
  }, { fromMarkdown: true });
});

test("a later PDF page that cannot draw keeps the other page readable", async ({ page, request }) => {
  await withPdf(page, request, async () => {
    const reader = page.getByRole("dialog");
    const pdf = await expectPdf(reader, "First PDF page");
    await failPdfCanvasDraw(pdf, 2);
    const next = pdf.getByRole("button", { name: "Next page" });
    const failure = pdf.getByRole("alert");
    await next.click();
    await expect(failure).toContainText("Couldn't draw this page.");
    await expect(pdf.locator("canvas")).toBeHidden();
    await expect(reader.getByRole("tab", { name: filename })).toHaveAttribute("aria-selected", "true");
    await pdf.getByRole("button", { name: "Zoom in" }).click();
    await expect(pdf.getByText("125%", { exact: true })).toBeVisible();
    await expect(failure).toContainText("Couldn't draw this page.");
    await pdf.getByRole("button", { name: "Previous page" }).click();
    await expectPdf(reader, "First PDF page");
    await next.click();
    await expect(failure).toContainText("Couldn't draw this page.");
  }, { bytes: pdfWithPages("First PDF page", "Second PDF page") });
});

withoutNativePdf("PDF.js displays the readable part of a page with a damaged image", async ({ page, request }) => {
  await withPdf(page, request, async () => {
    const reader = page.getByRole("dialog");
    await reader.getByRole("button", { name: "Open file: Read the PDF" }).click();
    await expect(reader.getByRole("tab", { name: filename })).toHaveAttribute("aria-selected", "true");
    const pdf = await expectPdf(reader, "Visible page text");
    const pixels = await pdf.locator("canvas").evaluate((node: HTMLCanvasElement) => {
      const context = node.getContext("2d")!;
      return [[0.125, 5 / 6], [0.5, 0.5]].map(([x, y]) =>
        [...context.getImageData(node.width * x, node.height * y, 1, 1).data]);
    });
    expect(pixels).toEqual([[0, 0, 255, 255], [255, 255, 255, 255]]);
  }, { fromMarkdown: true, bytes: pdfWithScan({ damaged: true }) });
});

test("a valid scanned PDF page activates after its image is decoded", async ({ page, request }) => {
  await withPdf(page, request, async () => {
    const reader = page.getByRole("dialog");
    await reader.getByRole("button", { name: "Open file: Read the PDF" }).click();
    await expect(reader.getByRole("tab", { name: filename })).toHaveAttribute("aria-selected", "true");
    expect(page.workers().some((worker) => worker.url().includes("pdf.worker"))).toBe(true);
    const pixel = await reader.getByTitle("PDF preview").contentFrame().locator("canvas").evaluate((node: HTMLCanvasElement) =>
      [...node.getContext("2d")!.getImageData(node.width / 2, node.height / 2, 1, 1).data]);
    expect(pixel).toEqual([0, 255, 0, 255]);
  }, { fromMarkdown: true, bytes: pdfWithScan() });
});

test("a valid PDF stamp is rendered before its page activates", async ({ page, request }) => {
  await withPdf(page, request, async () => {
    const reader = page.getByRole("dialog");
    await reader.getByRole("button", { name: "Open file: Read the PDF" }).click();
    await expect(reader.getByRole("tab", { name: filename })).toHaveAttribute("aria-selected", "true");
    const pixel = await reader.getByTitle("PDF preview").contentFrame().locator("canvas").evaluate((node: HTMLCanvasElement) =>
      [...node.getContext("2d")!.getImageData(node.width / 2, node.height / 2, 1, 1).data]);
    expect(pixel).toEqual([0, 255, 0, 255]);
  }, { fromMarkdown: true, bytes: pdfWithStamp() });
});

test("PDF rendering remains available if the browser cannot start its worker", async ({ page, request }) => {
  await page.addInitScript(() => {
    const NativeWorker = window.Worker;
    window.Worker = class extends NativeWorker {
      constructor(url: string | URL, options?: WorkerOptions) {
        if (String(url).includes("pdf.worker")) throw new DOMException("PDF worker unavailable", "SecurityError");
        super(url, options);
      }
    };
  });
  await withPdf(page, request, async () => {
    await expectPdf(page.getByRole("dialog"));
  });
});

withoutNativePdf("a valid blank PDF page activates after rendering", async ({ page, request }) => {
  await withPdf(page, request, async () => {
    const reader = page.getByRole("dialog");
    await reader.getByRole("button", { name: "Open file: Read the PDF" }).click();
    await expect(reader.getByRole("tab", { name: filename })).toHaveAttribute("aria-selected", "true");
    const canvas = reader.getByTitle("PDF preview").contentFrame().locator("canvas");
    await expect(canvas).toBeVisible();
    const pixel = await canvas.evaluate((node: HTMLCanvasElement) =>
      [...node.getContext("2d")!.getImageData(node.width / 2, node.height / 2, 1, 1).data]);
    expect(pixel).toEqual([255, 255, 255, 255]);
  }, { fromMarkdown: true, bytes: pdfWithPages("") });
});

test("dragging over rotated PDF text selects the drawn words", async ({ page, request }) => {
  const label = "Rotated PDF text";
  await withPdf(page, request, async () => {
    const pdf = await expectPdf(page.getByRole("dialog"), label);
    const canvas = pdf.locator("canvas");
    const bounds = (await canvas.boundingBox())!;
    const ink = await canvas.evaluate((node: HTMLCanvasElement) => {
      const { width, height } = node;
      const { data } = node.getContext("2d")!.getImageData(0, 0, width, height);
      let left = width;
      let right = 0;
      let top = height;
      let bottom = 0;
      for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
          if (data[(y * width + x) * 4] < 128) {
            left = Math.min(left, x);
            right = Math.max(right, x);
            top = Math.min(top, y);
            bottom = Math.max(bottom, y);
          }
        }
      }
      return { x: (left + right) / (2 * width), top: top / height, bottom: bottom / height };
    });
    expect(ink.bottom).toBeGreaterThan(ink.top);
    const x = bounds.x + ink.x * bounds.width;
    await page.mouse.move(x, bounds.y + ink.top * bounds.height - 2);
    await page.mouse.down();
    await page.mouse.move(x, bounds.y + (ink.top + 0.9 * (ink.bottom - ink.top)) * bounds.height, { steps: 14 });
    await page.mouse.up();
    await expect.poll(() => canvas.evaluate((node) => node.ownerDocument.getSelection()?.toString())).toMatch(/^Rotated PDF/);
  }, { bytes: pdfWithRotation([label], 90) });
});

test("PDF controls support Tab and Shift+Tab through the dialog", async ({ page, request }) => {
  await withPdf(page, request, async () => {
    const reader = page.getByRole("dialog");
    const pdf = await expectPdf(reader, "First PDF page");
    const path = reader.getByRole("button", { name: "Full file path" });
    await path.focus();
    await page.keyboard.press("Tab");
    await expect(pdf.getByRole("button", { name: "Next page" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(pdf.getByRole("button", { name: "Zoom out" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(pdf.getByRole("button", { name: "Zoom in" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(pdf.getByRole("region", { name: "PDF page 1" })).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(pdf.getByRole("button", { name: "Zoom in" })).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(pdf.getByRole("button", { name: "Zoom out" })).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(pdf.getByRole("button", { name: "Next page" })).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(path).toBeFocused();
    for (let index = 0; index < 5; index++) await page.keyboard.press("Tab");
    await expect(reader.getByRole("tab", { selected: true })).toBeFocused();
  }, { bytes: pdfWithPages("First PDF page", "Second PDF page") });
});

for (const action of ["Next page", "Zoom in"] as const) {
  test(`using ${action} in a PDF blocks an earlier opening from taking focus`, async ({ page, request }) => {
    await withPdf(page, request, async () => {
      const reader = page.getByRole("dialog");
      await reader.getByRole("button", { name: "Open file: Read the PDF" }).click();
      const pdf = await expectPdf(reader, "First PDF page");
      const pdfTab = reader.getByRole("tab", { name: filename });
      const isLater = (url: URL) => url.pathname.endsWith("/files") && url.searchParams.get("path") === "later.md";
      let release!: () => void;
      const paused = new Promise<void>((resolve) => { release = resolve; });
      const pending = page.waitForRequest((read) => isLater(new URL(read.url())));
      await page.route(isLater, async (route) => {
        await paused;
        await route.continue();
      }, { times: 1 });
      try {
        await reader.getByRole("tab", { name: "guide.md" }).click();
        await reader.getByRole("button", { name: "Open file: Read later" }).click();
        await pending;
        await reader.getByRole("button", { name: "Close guide.md" }).click();
        await expect(pdfTab).toHaveAttribute("aria-selected", "true");
        await pdf.getByRole("button", { name: action }).click();
        const result = action === "Next page" ? "Second PDF page" : "125%";
        await expect(pdf.getByText(result, { exact: true })).toBeVisible();
      } finally {
        release();
      }
      await expect(reader.getByRole("tab", { name: "later.md" })).toHaveAttribute("aria-selected", "false");
      await expect(pdfTab).toHaveAttribute("aria-selected", "true");
    }, { fromMarkdown: true, bytes: pdfWithPages("First PDF page", "Second PDF page") });
  });
}

test("retrying stalled PDF metadata keeps reading available until the new preview is drawn", async ({ page, request }) => {
  await withPdf(page, request, async () => {
    const reader = page.getByRole("dialog");
    const pendingMeta = (url: URL) => url.pathname.endsWith("/files/meta") && url.searchParams.get("path") === filename;
    let releaseMeta!: () => void;
    let finishMeta!: () => void;
    let releaseBytes!: () => void;
    const pausedMeta = new Promise<void>((resolve) => { releaseMeta = resolve; });
    const metaFinished = new Promise<void>((resolve) => { finishMeta = resolve; });
    const pausedBytes = new Promise<void>((resolve) => { releaseBytes = resolve; });
    await page.clock.install();
    const firstRead = page.waitForRequest((read) => pendingMeta(new URL(read.url())));
    await page.route(pendingMeta, async (route) => {
      try {
        await pausedMeta;
        await route.fulfill({ status: 404 }).catch(() => {});
      } finally {
        finishMeta();
      }
    }, { times: 1 });
    await page.route(isPdfPreviewRead, async (route) => {
      await pausedBytes;
      await route.continue();
    }, { times: 1 });
    try {
      await reader.getByRole("button", { name: "Open file: Read the PDF" }).click();
      await firstRead;
      await page.clock.fastForward(5_001);
      await expect(reader.getByRole("heading", { name: "Original guide" })).toBeVisible();
      await expect(reader.getByText(`${filename} is taking longer to load`)).toBeVisible();
      await expect(reader.getByRole("button", { name: `Cancel opening ${filename}` })).toBeVisible();
      await expect(reader.getByTitle("PDF preview")).toHaveCount(0);
      const retry = reader.getByRole("button", { name: `Retry opening ${filename}` });
      await expect(retry).toBeVisible();
      const replacementRead = page.waitForRequest((read) => isPdfPreviewRead(new URL(read.url())));
      await retry.click();
      await replacementRead;
      await expect(reader.getByTitle("PDF preview")).toHaveAttribute("inert", "");
      await expect(reader.getByRole("heading", { name: "Original guide" })).toBeVisible();
      releaseBytes();
      await expectPdf(reader);
      releaseMeta();
      await metaFinished;
      await expect(reader.getByRole("tab", { name: filename })).toHaveAttribute("aria-selected", "true");
      await expect(reader.getByRole("alert")).toHaveCount(0);
    } finally {
      releaseBytes();
      releaseMeta();
    }
  }, { fromMarkdown: true });
});

test("cancelling a linked PDF retires its frame and ignores the late response", async ({ page, request }) => {
  await withPdf(page, request, async () => {
    const reader = page.getByRole("dialog");
    const link = reader.getByRole("button", { name: "Open file: Read the PDF" });
    const guide = reader.getByRole("tab", { name: "guide.md" });
    const frame = reader.getByTitle("PDF preview");
    let authorize!: (status: number) => void;
    const authorized = new Promise<number>((resolve) => { authorize = resolve; });
    let release!: () => void;
    const paused = new Promise<void>((resolve) => { release = resolve; });
    let finish!: () => void;
    const finished = new Promise<void>((resolve) => { finish = resolve; });
    await page.route(isPdfPreviewRead, async (route) => {
      const response = await route.fetch();
      authorize(response.status());
      await paused;
      try {
        await route.fulfill({ response });
      } catch {
        // Cancelling the read can close the original intercepted request.
      } finally {
        finish();
      }
    }, { times: 1 });
    try {
      await link.press("Enter");
      expect(await authorized).toBe(200);
      await expect(frame).toBeAttached();
      await reader.getByRole("button", { name: `Cancel opening ${filename}` }).click();
      await expect(frame).toHaveCount(0);
      await expect(guide).toBeFocused();
    } finally {
      release();
    }
    await finished;
    await expect(guide).toHaveAttribute("aria-selected", "true");
    await expect(frame).toHaveCount(0);
    await expect(reader.getByRole("alert")).toHaveCount(0);
    await link.press("Enter");
    await expectPdf(reader);
  }, { fromMarkdown: true });
});

withoutNativePdf("a prepared PDF keeps its page, zoom and scroll across tab switches", async ({ page, request }) => {
  await page.setViewportSize({ width: 900, height: 470 });
  await withPdf(page, request, async () => {
    const reader = page.getByRole("dialog");
    const guide = reader.getByRole("tab", { name: "guide.md" });
    const pdfTab = reader.getByRole("tab", { name: filename });
    const iframe = reader.getByTitle("PDF preview");
    let release!: () => void;
    const paused = new Promise<void>((resolve) => { release = resolve; });
    const pending = page.waitForRequest((read) => isPdfPreviewRead(new URL(read.url())));
    await page.route(isPdfPreviewRead, async (route) => {
      await paused;
      await route.continue();
    }, { times: 1 });
    let frameNode;
    try {
      await reader.getByRole("button", { name: "Open file: Read the PDF" }).press("Enter");
      await pending;
      frameNode = (await iframe.elementHandle())!;
      await expect(iframe).toHaveAttribute("inert", "");
      await expect(guide).toHaveAttribute("aria-selected", "true");
      await expect(reader.getByRole("heading", { name: "Original guide" })).toBeVisible();
    } finally {
      release();
    }
    await expect(pdfTab).toHaveAttribute("aria-selected", "true");
    const pdf = await expectPdf(reader, "First PDF page");
    expect(await iframe.evaluate((node, prepared) => node === prepared, frameNode)).toBe(true);
    await pdf.getByRole("button", { name: "Next page" }).click();
    await expect(pdf.getByText("Second PDF page", { exact: true })).toBeVisible();
    await pdf.getByRole("button", { name: "Zoom in" }).click();
    await expect(pdf.getByText("125%", { exact: true })).toBeVisible();
    const canvas = pdf.locator("canvas");
    await expect(canvas).toBeVisible();
    const pageCanvas = (await canvas.elementHandle())!;
    const pageRegion = pdf.getByRole("region", { name: "PDF page 2" });
    const scrollTop = await pageRegion.evaluate((element) => {
      element.scrollTop = 60;
      return element.scrollTop;
    });
    expect(scrollTop).toBeGreaterThan(0);
    await guide.click();
    await expect(guide).toBeFocused();
    await expect(iframe).toHaveAttribute("aria-hidden", "true");
    await expect(iframe).toHaveAttribute("inert", "");
    await expect(iframe).toHaveAttribute("tabindex", "-1");
    expect(await iframe.evaluate((node) => {
      const box = node.getBoundingClientRect();
      return node.ownerDocument.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2) === node;
    })).toBe(false);
    await pdf.locator(".pdf-controls button").last().evaluate((button: HTMLButtonElement) => button.focus());
    await expect(guide).toBeFocused();
    await pdfTab.click();
    expect(await iframe.evaluate((node, prepared) => node === prepared, frameNode)).toBe(true);
    expect(await canvas.evaluate((node, retained) => node === retained, pageCanvas)).toBe(true);
    await expect(pdf.getByText("Page 2 of 2", { exact: true })).toBeVisible();
    await expect(pdf.getByText("125%", { exact: true })).toBeVisible();
    await expect(pageRegion).toHaveJSProperty("scrollTop", scrollTop);
    await pdf.getByRole("button", { name: "Zoom in" }).focus();
    for (let index = 0; index < 3; index++) await page.keyboard.press("Shift+Tab");
    await expect(reader.getByRole("button", { name: "Full file path" })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(reader).toBeHidden();
    await expect(page.locator(".chat-input-bar textarea")).toBeFocused();
  }, { fromMarkdown: true, bytes: pdfWithPages("First PDF page", "Second PDF page") });
});

test("a background PDF reload replaces only its own preview on success or failure", async ({ page, request }) => {
  await withPdf(page, request, async (pdfPath) => {
    const reader = page.getByRole("dialog");
    const guide = reader.getByRole("tab", { name: "guide.md" });
    const pdfTab = reader.getByRole("tab", { name: filename });
    const reload = reader.getByRole("button", { name: "Reload file" });
    const frames = reader.getByTitle("PDF preview");
    await reader.getByRole("button", { name: "Open file: Read the PDF" }).click();
    await expectPdf(reader, "Original PDF");
    await guide.click();
    const guideBody = reader.getByRole("tabpanel").locator(".file-viewer-body");
    const guideScroll = await guideBody.evaluate((node) => {
      node.scrollTop = 350;
      return node.scrollTop;
    });
    expect(guideScroll).toBeGreaterThan(0);

    for (const next of ["Updated PDF", "invalid"] as const) {
      await pdfTab.click();
      const oldFrame = (await frames.elementHandle())!;
      writeFileSync(pdfPath, next === "invalid" ? "%PDF-1.7\nIncomplete PDF.\n" : pdfWithPages(next));
      let release!: () => void;
      const paused = new Promise<void>((resolve) => { release = resolve; });
      const pending = page.waitForRequest((read) => isPdfPreviewRead(new URL(read.url())));
      await page.route(isPdfPreviewRead, async (route) => {
        await paused;
        await route.continue();
      }, { times: 1 });
      try {
        await reload.click();
        await pending;
        await expect(frames).toHaveCount(2);
        expect(await oldFrame.evaluate((node) => node.closest("[inert]"))).toBeNull();
        await guide.click();
        await expect(guide).toHaveAttribute("aria-selected", "true");
      } finally {
        release();
      }
      await expect(frames).toHaveCount(next === "invalid" ? 0 : 1);
      expect(await oldFrame.evaluate((node) => node.isConnected)).toBe(false);
      await expect(guide).toHaveAttribute("aria-selected", "true");
      await expect(guideBody).toHaveJSProperty("scrollTop", guideScroll);
      await pdfTab.click();
      if (next === "invalid") {
        await expect(reader.getByRole("alert")).toHaveText("Couldn't load the preview.");
        writeFileSync(pdfPath, pdfWithPages("Recovered PDF"));
        await reader.getByRole("button", { name: "Retry", exact: true }).click();
        await expectPdf(reader, "Recovered PDF");
      } else {
        await expectPdf(reader, next);
      }
    }
  }, { fromMarkdown: true, bytes: pdfWithPages("Original PDF") });
});

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
    const viewer = page.getByRole("dialog");
    const pdf = await expectPdf(viewer);
    const pixel = await pdf.locator("canvas").evaluate((node: HTMLCanvasElement) =>
      [...node.getContext("2d")!.getImageData(node.width / 2, node.height * .6, 1, 1).data]);
    expect(pixel).toEqual([0, 153, 0, 255]);
    const src = (await viewer.getByRole("link", { name: "Open in new tab" }).getAttribute("href"))!;
    expect(new URL(src).searchParams.get("preview")).toBe("true");
    const response = await request.get(src);
    expect(response.status()).toBe(200);
    expect(response.headers()["content-disposition"]).toMatch(/^inline;/);
    expect(await response.body()).toEqual(document);
    await page.screenshot({ path: info.outputPath("pdf-preview.png") });
    await downloadPdf(page);
    await page.setViewportSize({ width: 320, height: 520 });
    await viewer.getByRole("button", { name: "File actions" }).click();
    const open = viewer.getByRole("menuitem", { name: "Open in new tab" });
    await expect(open).toHaveAttribute("href", src);
    await expect(open).toHaveAttribute("target", "_blank");
    const download = new URL((await viewer.getByRole("menuitem", { name: "Download file" }).getAttribute("href"))!);
    expect(download.searchParams.has("preview")).toBe(false);
  });
});

withoutNativePdf("disabled native PDF handling still renders PDF.js and retains the original file actions", async ({ page, request }) => {
  await withPdf(page, request, async () => {
    expect(await page.evaluate(() => navigator.pdfViewerEnabled)).toBe(false);
    const reader = page.getByRole("dialog");
    await reader.getByRole("button", { name: "Open file: Read the PDF" }).click();
    const pdf = await expectPdf(reader);
    const pixel = await pdf.locator("canvas").evaluate((node: HTMLCanvasElement) =>
      [...node.getContext("2d")!.getImageData(node.width / 2, node.height * .6, 1, 1).data]);
    expect(pixel).toEqual([0, 153, 0, 255]);
    await expect(reader.getByRole("tab", { name: filename })).toHaveAttribute("aria-selected", "true");
    const opened = page.waitForEvent("download");
    await reader.getByRole("link", { name: "Open in new tab" }).click();
    const file = await opened;
    expect(file.suggestedFilename()).toBe(filename);
    expect(readFileSync((await file.path())!)).toEqual(document);
    await downloadPdf(page);
  }, { fromMarkdown: true });
});

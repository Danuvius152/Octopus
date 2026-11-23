import { mkdtempSync, mkdirSync, renameSync, rmSync, symlinkSync, unlinkSync, utimesSync, writeFileSync } from "node:fs";
import path from "node:path";

import { accountToken, enterAccountCredentials } from "./current-account";
import { expect, test, type APIRequestContext, type Page } from "./test";

const API = "http://localhost:8765/api";
const headers = { get Authorization() { return `Bearer ${accountToken()}`; } };

const previewTest = test.extend<{ previewDirectory: string }>({
  previewDirectory: async ({ request }, runTest) => {
    const workspace = await request.get(`${API}/auth/workspace`, { headers });
    expect(workspace.ok()).toBeTruthy();
    const directory = mkdtempSync(path.join((await workspace.json()).workspace, "markdown-preview-"));
    try {
      await runTest(directory);
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  },
});

async function createSession(request: APIRequestContext, workingDir: string, name: string) {
  const created = await request.post(`${API}/sessions/import`, { headers, data: { name, working_dir: workingDir } });
  expect(created.status()).toBe(201);
  return (await created.json()).id as string;
}

async function openSession(page: Page, name: string) {
  await page.locator(".session-item").getByText(name, { exact: true }).click();
}

async function signIn(page: Page) {
  await page.goto("/");
  await enterAccountCredentials(page);
  await page.locator(".agent-item", { hasText: "Octo" }).click();
}

async function showAlias(page: Page) {
  await page.locator(".chat-input-bar textarea").fill("/showme alias.md");
  await page.locator("button.btn-send").click();
}

const spacing = Array.from({ length: 32 }, (_, number) => `Paragraph ${number + 1}.`).join("\n\n");
const png = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a/r8AAAAASUVORK5CYII=",
  "base64",
);

previewTest("Markdown assets and anchors follow the authorized source after its session is archived", async ({ page, request, previewDirectory: directory }) => {
  const source = path.join(directory, "source");
  const other = path.join(directory, "other");
  const alias = path.join(source, "alias.md");
  const appendix = " 中文%20#图.md";
  mkdirSync(path.join(source, "docs"), { recursive: true });
  mkdirSync(path.join(source, "assets", "nested"), { recursive: true });
  mkdirSync(path.join(other, "docs", "jump"), { recursive: true });
  symlinkSync("../assets/nested", path.join(source, "docs", "jump"));
  symlinkSync("docs/readme.md", alias);
  symlinkSync("docs/readme.md", path.join(other, "alias.md"));
  writeFileSync(path.join(source, "replacement.md"), "# Unexpected alias target");
  writeFileSync(path.join(source, "assets", "diagram.png"), png);
  writeFileSync(path.join(other, "docs", "diagram.png"), png);
  writeFileSync(path.join(source, "docs", appendix), [
    "# Appendix", spacing, "## 目标小节", spacing,
  ].join("\n\n"));
  writeFileSync(path.join(other, "docs", appendix), "# Other appendix");
  const content = [
    "# Source document",
    "[Jump to second heading](#检查结果-1)",
    "[Open appendix](%20中文%2520%23图.md?ignored=1#目标小节)",
    "![Diagram](jump/../diagram.png?token=author)",
    "First reference [^中文]",
    spacing, "## 检查结果", spacing, "## 检查结果",
    "Second reference [^中文]", spacing, "[^中文]: Footnote text",
  ].join("\n\n");
  for (const root of [source, other]) writeFileSync(path.join(root, "docs", "readme.md"), content);

  const sourceId = await createSession(request, source, "Markdown source");
  const otherId = await createSession(request, other, "Markdown other");
  await signIn(page);
  await openSession(page, "Markdown other");
  await showAlias(page);
  const dialog = page.getByRole("dialog");
  const article = dialog.getByRole("article");
  await expect(article.getByRole("heading", { name: "Source document" })).toBeVisible();
  const otherDiagram = article.getByRole("img", { name: "Diagram" });
  await expect.poll(() => otherDiagram.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(1);
  expect(new URL((await otherDiagram.getAttribute("src"))!).pathname).toBe(`/api/sessions/${otherId}/files`);
  await article.getByRole("link", { name: "Open appendix" }).click();
  await expect(article.getByRole("heading", { name: "Other appendix" })).toBeVisible();
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await openSession(page, "Markdown source");

  const sourceFiles = `/api/sessions/${sourceId}/files`;
  await page.route((url) => url.pathname === `${sourceFiles}/meta` && url.searchParams.get("path") === "alias.md",
    async (route) => {
      const meta = await route.fetch();
      expect(meta.status()).toBe(200);
      expect((await meta.json()).path).toBe("docs/readme.md");
      unlinkSync(alias);
      symlinkSync("replacement.md", alias);
      const archived = await request.post(`${API}/sessions/${sourceId}/archive?replace=false`, { headers });
      expect(archived.status()).toBe(204);
      await route.fulfill({ response: meta });
    }, { times: 1 });
  await showAlias(page);
  await expect(article.getByRole("heading", { name: "Source document" })).toBeVisible();

  const diagram = article.getByRole("img", { name: "Diagram" });
  await expect.poll(() => diagram.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(1);
  const diagramUrl = new URL((await diagram.getAttribute("src"))!);
  expect(diagramUrl.pathname).toBe(sourceFiles);
  expect(diagramUrl.searchParams.get("path")).toBe("jump/../diagram.png");
  expect(diagramUrl.searchParams.get("source_directory")).toBe("docs");
  expect(diagramUrl.searchParams.getAll("token")).toHaveLength(1);

  const headings = article.getByRole("heading", { name: "检查结果" });
  await expect(headings.nth(1)).not.toBeInViewport();
  await article.getByRole("link", { name: "Jump to second heading" }).click();
  await expect(headings.nth(1)).toBeInViewport();
  await expect(headings.nth(0)).not.toBeInViewport();

  const references = article.locator("a[data-footnote-ref]");
  const backLinks = article.locator("a[data-footnote-backref]");
  for (const index of [0, 1]) {
    await references.nth(index).click();
    await expect(article.getByText("Footnote text")).toBeInViewport();
    await backLinks.nth(index).click();
    await expect(references.nth(index)).toBeInViewport();
    await expect(references.nth(1 - index)).not.toBeInViewport();
  }

  const appendixRead = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === sourceFiles && url.searchParams.get("path") === `docs/${appendix}`;
  });
  await article.getByRole("link", { name: "Open appendix" }).click();
  expect((await appendixRead).status()).toBe(200);
  await expect(article.getByRole("heading", { name: "目标小节" })).toBeInViewport();
  await expect(article.getByRole("heading", { name: "Appendix" })).not.toBeInViewport();
});

previewTest("heading fragments and generated footnotes reach their own targets", async ({ page, request, previewDirectory: directory }) => {
  writeFileSync(path.join(directory, "alias.md"), [
    "[Jump to note-named heading](#user-content-fn-a)",
    "[Jump to prefixed heading](#file-viewer-heading-start)",
    "Reference to the footnote [^a]",
    spacing, "## User Content Fn A", spacing,
    "## Start", spacing, "## File Viewer Heading Start", spacing,
    "[^a]: The actual footnote.",
  ].join("\n\n"));
  await createSession(request, directory, "Anchor collisions");
  await signIn(page);
  await openSession(page, "Anchor collisions");
  await showAlias(page);

  const article = page.getByRole("dialog").getByRole("article");
  await article.getByRole("link", { name: "Jump to note-named heading" }).click();
  await expect(article.getByRole("heading", { name: "User Content Fn A" })).toBeInViewport();
  await article.getByRole("link", { name: "Jump to prefixed heading" }).click();
  await expect(article.getByRole("heading", { name: "File Viewer Heading Start" })).toBeInViewport();

  const reference = article.locator("a[data-footnote-ref]");
  await reference.click();
  await expect(article.getByText("The actual footnote.")).toBeInViewport();
  await article.locator("a[data-footnote-backref]").click();
  await expect(reference).toBeInViewport();
});

// Keep browser HTTP caching enabled for this story. The loopback-only proxy
// configuration preserves the offline network boundary without Playwright routing.
const cacheTest = previewTest.extend({
  proxy: { server: "http://127.0.0.1:1", bypass: "localhost,127.0.0.1,[::1]" },
  _offlineNetwork: async ({ proxy }, runTest) => {
    if (!proxy) throw new Error("The cache story requires a loopback-only proxy");
    await runTest();
  },
});

cacheTest("reopening a Markdown preview rechecks the canonical path with an HTTP cache present", async ({ page, request, previewDirectory: directory }) => {
  const document = path.join(directory, "readme.md");
  writeFileSync(document, "# Original Markdown");
  const old = new Date("2020-01-01");
  utimesSync(document, old, old);
  writeFileSync(path.join(directory, "redirected.md"), "# Redirected Markdown");
  symlinkSync("readme.md", path.join(directory, "alias.md"));
  const sessionId = await createSession(request, directory, "Cache preview");
  await page.exposeFunction("redirectMarkdownBeforeBytes", () => {
    unlinkSync(document);
    symlinkSync("redirected.md", document);
  });
  await page.addInitScript(() => {
    let redirectOnRead = false;
    let redirect: Promise<void> | undefined;
    window.addEventListener("redirect-on-markdown-read", () => { redirectOnRead = true; });
    const fetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const url = new URL(String(input), location.href);
      if (redirectOnRead && url.pathname.endsWith("/files") && url.searchParams.get("path") === "readme.md") {
        // The viewer has already accepted metadata for this read. Concurrent
        // effects share the same filesystem change before fetching bytes.
        redirect ??= (window as unknown as { redirectMarkdownBeforeBytes(): Promise<void> }).redirectMarkdownBeforeBytes();
        await redirect;
      }
      return fetch(input, init);
    };
  });
  await signIn(page);
  await openSession(page, "Cache preview");
  await showAlias(page);
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Original Markdown" })).toBeVisible();

  await page.evaluate(() => window.dispatchEvent(new Event("redirect-on-markdown-read")));
  const attemptedRead = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === `/api/sessions/${sessionId}/files` && url.searchParams.get("path") === "readme.md";
  });
  await dialog.getByRole("button", { name: "Reload file" }).click();
  expect((await attemptedRead).status()).toBe(409);
  await expect(dialog.getByText(/file path changed/i)).toBeVisible();
  await dialog.getByRole("button", { name: "Reload file" }).click();
  await expect(dialog.getByRole("heading", { name: "Redirected Markdown" })).toBeVisible();
});

cacheTest("an open Markdown preview keeps relative resources tied to its document directory", async ({ page, request, previewDirectory: directory }) => {
  const original = path.join(directory, "original");
  const replacement = path.join(directory, "replacement");
  for (const [root, title, width] of [[original, "Original", 12], [replacement, "Replacement", 24]] as const) {
    mkdirSync(root);
    writeFileSync(path.join(root, "alias.md"), `# ${title} document\n\n![Diagram](diagram.svg)\n\n[Open appendix](appendix.md)`);
    const diagram = path.join(root, "diagram.svg");
    writeFileSync(diagram, `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="12"><rect width="${width}" height="12"/></svg>`);
    const old = new Date("2020-01-01");
    utimesSync(diagram, old, old);
    writeFileSync(path.join(root, "appendix.md"), `# ${title} appendix`);
  }
  await createSession(request, original, "Relative resources");
  await signIn(page);
  await openSession(page, "Relative resources");
  await showAlias(page);

  const dialog = page.getByRole("dialog");
  const article = dialog.getByRole("article");
  await expect(article.getByRole("heading", { name: "Original document" })).toBeVisible();
  const diagram = article.getByRole("img", { name: "Diagram" });
  await expect.poll(() => diagram.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(12);
  const originalImage = (await diagram.getAttribute("src"))!;

  renameSync(original, path.join(directory, "retired"));
  symlinkSync(replacement, original);
  await article.getByRole("link", { name: "Open appendix" }).click();
  await expect(dialog.getByText(/document directory changed/i)).toBeVisible();
  expect(await page.evaluate(async (url) => (await fetch(url)).status, originalImage)).toBe(409);

  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await showAlias(page);
  await expect(article.getByRole("heading", { name: "Replacement document" })).toBeVisible();
  await expect.poll(() => diagram.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(24);
  await article.getByRole("link", { name: "Open appendix" }).click();
  await expect(article.getByRole("heading", { name: "Replacement appendix" })).toBeVisible();
});

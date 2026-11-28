import { mkdtempSync, mkdirSync, renameSync, rmSync, symlinkSync, unlinkSync, utimesSync, writeFileSync } from "node:fs";
import { createServer, request as forwardRequest } from "node:http";
import os from "node:os";
import path from "node:path";

import { accountToken, enterAccountCredentials, openAsCurrentAccount, type TestAccount } from "./current-account";
import { cleanupAccount, createTestAccount, editExtraRoots } from "./account-resources";
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

async function signIn(page: Page, url = "/") {
  await page.goto(url);
  await enterAccountCredentials(page);
  await page.locator(".agent-item", { hasText: "Octo" }).click();
}

async function showFile(page: Page, target: string) {
  await page.locator(".chat-input-bar textarea").fill(`/showme ${target}`);
  await page.locator("button.btn-send").click();
}

async function showAlias(page: Page) {
  await showFile(page, "alias.md");
}

const browserZoomTest = previewTest.extend({
  context: async ({ playwright, baseURL }, provideContext) => {
    // Keep Chromium's profile and singleton socket paths within the Unix socket limit.
    const profile = mkdtempSync(path.join(os.homedir(), ".cache", "octopus-tests", "zoom-"));
    try {
      const context = await playwright.chromium.launchPersistentContext(profile, {
        channel: "chromium", serviceWorkers: "block", baseURL, env: { ...process.env, TMPDIR: profile },
      });
      try { await provideContext(context); }
      finally { await context.close(); }
    } finally {
      rmSync(profile, { recursive: true, force: true });
    }
  },
});

browserZoomTest("reader typography preserves paragraphs and source lines and reflows at narrow widths", async ({ page, context, request, previewDirectory: directory }) => {
  const filename = "阅读 中文台账.md";
  const file = path.join(directory, filename);
  const prose = "这里是包含中文的普通段落，浏览器会按照正文宽度重新排版。".repeat(6);
  writeFileSync(file, [
    "# Reader typography", "", "Inline `snippet`", "",
    "| Column A | Column B | Column C |", "| --- | --- | --- |",
    `| A long unbreakable table value \`${"a".repeat(60)}\` | b | c |`, "",
    "```js", `const fenced = "${"x".repeat(120)}";`, "```", "",
    ...Array.from({ length: 60 }, (_, index) => `Paragraph ${index + 1} — ${prose}\n`),
    `- Long list item — ${prose.repeat(8)}`,
    ...Array.from({ length: 30 }, (_, index) => `  - Nested bullet ${index + 1}`), "",
  ].join("\n"));
  const id = await createSession(request, directory, "Reader layout");
  const fileRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith(`/api/sessions/${id}/files`)) fileRequests.push(request.url());
  });
  await page.setViewportSize({ width: 1080, height: 800 });
  await signIn(page);
  await openSession(page, "Reader layout");
  await showFile(page, JSON.stringify(filename));
  const viewer = page.getByRole("dialog");
  const size = viewer.getByRole("combobox", { name: "Text size" });
  const article = viewer.getByRole("article");
  const fenced = article.locator("pre");
  const table = article.getByRole("table");
  const inline = article.getByText("snippet", { exact: true });
  const title = viewer.getByRole("heading", { name: filename });
  await expect(article.getByRole("heading", { name: "Reader typography" })).toBeVisible();
  await expect(article).toHaveCSS("font-size", "18px");
  await expect(article).toHaveCSS("line-height", "31.5px");
  await expect(article).toHaveCSS("max-width", "704px");
  await expect(title).toHaveCSS("font-size", "16px");
  await expect(viewer.getByText(file, { exact: true })).toHaveCSS("font-size", "14px");
  expect(await article.evaluate((element) => getComputedStyle(element).fontFamily)).toContain("Noto Sans SC Variable");
  expect(await fenced.evaluate((element) => getComputedStyle(element).fontFamily)).toContain("IBM Plex Mono");
  expect(Number.parseFloat(await inline.evaluate((element) => getComputedStyle(element).fontSize))).toBeCloseTo(16.92);
  await expect(table.getByRole("cell").first()).toHaveCSS("font-size", "18px");
  await expect(fenced).toHaveCSS("font-size", "16px");
  await expect(fenced).toHaveCSS("line-height", "25.6px");

  const paragraph = article.getByText(/^Paragraph 31 —/);
  await paragraph.evaluate((element) => element.scrollIntoView({ block: "start" }));
  const readingTop = (await paragraph.boundingBox())!.y;
  const requests = fileRequests.length;
  await size.selectOption("larger");
  await expect(article).toHaveCSS("font-size", "22px");
  await expect(fenced).toHaveCSS("font-size", "20px");
  expect((await paragraph.boundingBox())!.y).toBeCloseTo(readingTop, 0);
  await viewer.getByRole("button", { name: "Source", exact: true }).click();
  const source = viewer.locator("pre");
  const sourceLine = source.getByText(/^Paragraph 31 —/);
  const numbered = sourceLine.locator("xpath=ancestor-or-self::*[@data-line][1]");
  await numbered.evaluate((row) => {
    const viewport = row.closest("pre")!.parentElement!;
    viewport.scrollTop += row.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 2;
  });
  const line = await numbered.getAttribute("data-line");
  const sourceTop = (await numbered.boundingBox())!.y;
  await size.selectOption("compact");
  await expect(source).toHaveCSS("font-size", "14px");
  await expect(source).toHaveCSS("line-height", "22.4px");
  await expect(numbered).toHaveAttribute("data-line", line!);
  expect(Math.abs((await numbered.boundingBox())!.y - sourceTop)).toBeLessThan(1);
  expect(fileRequests).toHaveLength(requests);

  await page.setViewportSize({ width: 320, height: 520 });
  await expect(viewer.getByRole("button", { name: "Close", exact: true })).toBeInViewport();
  expect(await source.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
  await viewer.getByRole("button", { name: "Reading", exact: true }).click();
  await size.selectOption("standard");
  const settings = await context.newPage();
  await settings.goto("chrome://settings/appearance");
  const setZoom = (zoom: number) => settings.evaluate((zoom) => {
    const chrome = (window as unknown as { chrome: { settingsPrivate: { setDefaultZoom: (zoom: number) => void } } }).chrome;
    chrome.settingsPrivate.setDefaultZoom(zoom);
  }, zoom);
  try {
    for (const zoom of [1, 2]) {
      await page.setViewportSize({ width: 320 * zoom, height: 520 * zoom });
      await setZoom(zoom);
      await page.bringToFront();
      await expect.poll(() => page.evaluate(() => [window.innerWidth, window.devicePixelRatio])).toEqual([320, zoom]);
      const fits = await paragraph.evaluate((element) => ({
        right: element.getBoundingClientRect().right,
        lines: element.clientHeight / Number.parseFloat(getComputedStyle(element).lineHeight),
        pageWidth: document.documentElement.scrollWidth,
      }));
      expect(fits.right).toBeLessThanOrEqual(320);
      expect(fits.lines).toBeGreaterThan(2);
      expect(fits.pageWidth).toBe(320);
      const tableRegion = article.getByRole("region", { name: "Table", exact: true });
      for (const [label, scroller] of [["table", tableRegion], ["fenced code", fenced]] as const) {
        const [contentWidth, scrollportWidth] = await scroller.evaluate((element) => [element.scrollWidth, element.clientWidth]);
        expect(contentWidth, label).toBeGreaterThan(scrollportWidth);
      }
      const menu = viewer.getByRole("button", { name: "File actions" });
      await menu.press("Enter");
      await expect(viewer.getByRole("menuitem", { name: "Copy path" })).toBeInViewport();
      await expect(viewer.getByRole("menuitem", { name: "Download file" })).toBeVisible();
      await expect(viewer.getByRole("menuitem", { name: "Open in new tab" })).toHaveCount(0);
      await page.keyboard.press("Escape");
      await expect(menu).toBeFocused();
      await expect(menu).toHaveCSS("outline-width", "2px");
      await expect(viewer).toBeVisible();
    }
  } finally {
    await setZoom(1);
    await settings.close();
  }
  await page.setViewportSize({ width: 320, height: 520 });
  await size.selectOption("larger");
  const listItem = article.getByRole("listitem").filter({ hasText: /^Long list item —/ });
  const readingOffset = await listItem.evaluate((item) => {
    const viewport = item.closest("article")!.parentElement!;
    const nestedTop = item.querySelector("ul")!.getBoundingClientRect().top;
    viewport.scrollTop += nestedTop - viewport.getBoundingClientRect().top - 30;
    const text = item.firstChild as Text;
    const range = document.createRange();
    for (let offset = text.length - 1; offset >= 0; offset--) {
      range.setStart(text, offset);
      range.setEnd(text, offset + 1);
      const { top, bottom } = range.getBoundingClientRect();
      if (top < viewport.getBoundingClientRect().top + 10 && bottom > viewport.getBoundingClientRect().top) return offset;
    }
    throw new Error("The viewport should start within the long list item's text");
  });
  await size.selectOption("compact");
  expect(await listItem.evaluate((item, offset) => {
    const viewport = item.closest("article")!.parentElement!.getBoundingClientRect();
    const range = document.createRange();
    range.setStart(item.firstChild!, offset);
    range.setEnd(item.firstChild!, offset + 1);
    const { top, bottom } = range.getBoundingClientRect();
    return bottom > viewport.top && top < viewport.bottom;
  }, readingOffset)).toBe(true);
  const pathToggle = viewer.getByRole("button", { name: "Full file path" });
  await pathToggle.click();
  await expect(pathToggle).toHaveAttribute("aria-expanded", "true");
  const fullPath = viewer.getByText(file, { exact: true });
  await fullPath.evaluate((element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
  });
  expect(await page.evaluate(() => window.getSelection()?.toString())).toBe(file);
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await viewer.getByRole("button", { name: "File actions" }).click();
  await viewer.getByRole("menuitem", { name: "Copy path" }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(file);
  await viewer.getByRole("button", { name: "File actions" }).press("Enter");
  await viewer.getByRole("menuitem", { name: "Edit path" }).press("Enter");
  await expect(viewer.getByRole("textbox", { name: "File path" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(viewer).toBeHidden();
  await expect(page.locator(".chat-input-bar textarea")).toBeFocused();
});

const readerAccountsTest = previewTest.extend<{ otherAccount: TestAccount }>({
  otherAccount: async ({ request }, provideAccount) => {
    const admin = process.env.OCTOPUS_E2E_ACCOUNT_TOKEN;
    if (!admin) throw new Error("Missing runner provisioning account");
    const account = await createTestAccount(request, admin);
    await provideAccount(account);
    await cleanupAccount(request, account);
  },
});

readerAccountsTest("reader text size survives reload and follows the account in the same browser", async ({
  page, request, previewDirectory: directory, otherAccount,
}) => {
  const filename = "reader-preference.txt";
  writeFileSync(path.join(directory, filename), "Account A reader preference");
  await createSession(request, directory, "Reader account A");
  const otherHeaders = { Authorization: `Bearer ${otherAccount.token}` };
  const workspace = await request.get(`${API}/auth/workspace`, { headers: otherHeaders });
  expect(workspace.ok()).toBeTruthy();
  const cwd = (await workspace.json()).workspace;
  writeFileSync(path.join(cwd, filename), "Account B reader preference");
  expect((await request.post(`${API}/sessions/import`, {
    headers: otherHeaders, data: { name: "Reader account B", working_dir: cwd },
  })).status()).toBe(201);
  // Change the stored login in this browser, retaining the same origin's preferences.
  const switchTo = async (token: string, account: string) => {
    await page.evaluate((bearer) => localStorage.setItem("octopus_token", bearer), token);
    await page.reload();
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await openSession(page, `Reader account ${account}`);
    await showFile(page, filename);
    await expect(page.getByRole("dialog").getByText(`Account ${account} reader preference`)).toBeVisible();
  };
  await signIn(page);
  await openSession(page, "Reader account A");
  await showFile(page, filename);
  const size = page.getByRole("combobox", { name: "Text size" });
  const text = page.getByRole("dialog").locator("pre");
  await expect(size).toHaveValue("standard");
  await expect(text).toHaveCSS("font-size", "16px");
  await size.selectOption("large");
  await expect(text).toHaveCSS("font-size", "18px");
  await switchTo(otherAccount.token, "B");
  await expect(size).toHaveValue("standard");
  await size.selectOption("compact");
  await switchTo(accountToken(), "A");
  await expect(size).toHaveValue("large");
  await switchTo(otherAccount.token, "B");
  await expect(size).toHaveValue("compact");
});

previewTest("showme searches conversation descriptions in the viewer and recovers after cwd revocation", async ({ page, request, account, previewDirectory }) => {
  if (!account) throw new Error("Expected an isolated account");
  const cwd = mkdtempSync(path.join(os.tmpdir(), "octopus-context-search-"));
  try {
    writeFileSync(path.join(cwd, "plan.md"), "# Plan from the conversation");
    writeFileSync(path.join(cwd, "中文 计划.md"), "# Literal Chinese filename");
    writeFileSync(path.join(previewDirectory, "context-search-recovery.md"), "# File from an available source");
    const setExtraRoots = await editExtraRoots(request, account.user_id);
    await setExtraRoots([cwd]);
    const id = await createSession(request, cwd, "Context search session");
    const history = async () => {
      const response = await request.get(`${API}/sessions/${id}/messages?before_seq=10000`, { headers });
      expect(response.ok()).toBeTruthy();
      return (await response.json()).messages;
    };
    const initialHistory = await history();
    const resolutions: { text: string; mode?: string; status: number }[] = [];
    page.on("response", response => {
      if (new URL(response.url()).pathname === `/api/sessions/${id}/showme/resolve`) {
        resolutions.push({ ...response.request().postDataJSON(), status: response.status() });
      }
    });
    const submissions: string[] = [];
    page.on("websocket", socket => socket.on("framesent", ({ payload }) => {
      if (String(payload).includes('"send_message"')) submissions.push(String(payload));
    }));

    await signIn(page);
    await openSession(page, "Context search session");
    const viewer = page.getByRole("dialog");
    const close = () => viewer.getByRole("button", { name: "Close", exact: true }).click();
    const waitForConfirmation = () => page.waitForResponse(response =>
      new URL(response.url()).pathname === `/api/sessions/${id}/showme/resolve` && response.ok());
    const positioned = waitForConfirmation();
    await showFile(page, "中文 计划.md#L1");
    const fileResult = await (await positioned).json();
    expect(Object.keys(fileResult).sort()).toEqual(["metadata", "target"]);
    expect(fileResult.target).toEqual({ path: "中文 计划.md", position: { kind: "lines", start: 1 } });
    await expect(viewer.locator('[data-line="1"][data-located="true"]')).toHaveText("# Literal Chinese filename");
    await close();

    await showFile(page, "请打开 plan.md");
    await expect(viewer.getByRole("alert")).toHaveText("File not found.");
    expect(resolutions).toEqual([{ text: "中文 计划.md#L1", status: 200 }, { text: "请打开 plan.md", status: 404 }]);
    const clarified = waitForConfirmation();
    await viewer.getByRole("button", { name: "Search by description" }).press("Enter");
    expect(await (await clarified).json()).toEqual({ message: "Which plan did you mean?", description: "请打开 plan.md" });
    await expect(viewer.getByRole("status")).toHaveText("Which plan did you mean?");
    const description = viewer.getByRole("textbox", { name: "File description" });
    await expect(description).toHaveValue("请打开 plan.md");
    await expect(description).toBeFocused();
    await description.fill("the implementation plan.md");
    const inferred = waitForConfirmation();
    await viewer.getByRole("button", { name: "Search conversation" }).click();
    const inferredResult = await (await inferred).json();
    expect(Object.keys(inferredResult).sort()).toEqual(["description", "metadata", "target"]);
    expect(inferredResult.description).toBe("the implementation plan.md");
    await expect(viewer.getByRole("heading", { name: "Plan from the conversation" })).toBeVisible();
    await close();

    await showFile(page, "the report from v1.2");
    await expect(viewer.getByRole("heading", { name: "Plan from the conversation" })).toBeVisible();
    await close();

    await setExtraRoots([]);
    await showFile(page, "the report from v1.2");
    await expect(viewer.getByRole("alert")).toHaveText("The session working directory is no longer available.");
    await expect(description).toHaveValue("the report from v1.2");
    await viewer.getByRole("button", { name: "Choose a file" }).click();
    await viewer.getByRole("searchbox", { name: "Search filenames" }).fill("context-search-recovery.md");
    await viewer.getByRole("button", { name: /context-search-recovery\.md/ }).click();
    await expect(viewer.getByRole("heading", { name: "File from an available source" })).toBeVisible();
    expect(resolutions.slice(2)).toEqual([
      { text: "请打开 plan.md", mode: "description", status: 200 },
      { text: "the implementation plan.md", mode: "description", status: 200 },
      { text: "the report from v1.2", status: 200 },
      { text: "the report from v1.2", status: 403 },
    ]);
    expect(await history()).toEqual(initialHistory);
    expect(submissions).toEqual([]);
  } finally {
    rmSync(cwd, { recursive: true, force: true });
  }
});

previewTest("showme opens and browses registered application data outside the workspace", async ({ page, request, previewDirectory }) => {
  const agentList = await request.get(`${API}/agents`, { headers });
  expect(agentList.ok()).toBeTruthy();
  const agents = await agentList.json() as { id: string }[];
  const created = await request.post(`${API}/applications`, {
    headers, data: { name: "Viewer data example", description: "Application data for the file viewer", agent_id: agents[0].id },
  });
  expect(created.status(), await created.text()).toBe(201);
  const { app_dir: code } = await created.json() as { app_dir: string };
  const source = `${code}.data`;
  const reportName = "Application report.md";
  const report = path.join(source, reportName);
  mkdirSync(source, { recursive: true });
  writeFileSync(report, "# Application report\n\n[View details](details.md)");
  writeFileSync(path.join(source, "details.md"), "# Data details");
  const sessionName = "Read application data";
  const id = await createSession(request, previewDirectory, sessionName);
  const messages = async () => {
    const response = await request.get(`${API}/sessions/${id}/messages?before_seq=10000`, { headers });
    expect(response.ok()).toBeTruthy();
    return (await response.json()).messages;
  };
  const initialMessages = await messages();
  const confirmations: { text: string; mode?: string }[] = [];
  page.on("request", req => {
    if (new URL(req.url()).pathname === `/api/sessions/${id}/showme/resolve`) {
      confirmations.push(req.postDataJSON());
    }
  });

  await signIn(page);
  await openSession(page, sessionName);
  const relative = path.relative(previewDirectory, report);
  await showFile(page, relative);
  const viewer = page.getByRole("dialog");
  const close = () => viewer.getByRole("button", { name: "Close", exact: true }).click();
  await expect(viewer.getByRole("heading", { name: "Application report", exact: true })).toBeVisible();
  await viewer.getByRole("button", { name: /^Open file: View details/ }).click();
  await expect(viewer.getByRole("heading", { name: "Data details", exact: true })).toBeVisible();
  await close();

  await showFile(page, "");
  await viewer.getByRole("combobox", { name: "Source" }).selectOption(source);
  await viewer.getByRole("searchbox", { name: "Search filenames" }).fill(reportName);
  await viewer.getByRole("button").filter({ has: page.getByText(reportName, { exact: true }) }).click();
  await expect(viewer.getByRole("heading", { name: "Application report", exact: true })).toBeVisible();
  expect(confirmations).toEqual([{ text: relative }]);
  expect(await messages()).toEqual(initialMessages);
});

previewTest("bare showme browses sources and opens the selected filename without a chat turn", async ({ page, request, account, previewDirectory }) => {
  if (!account) throw new Error("Expected an isolated account");
  const fixture = mkdtempSync(path.join(os.tmpdir(), "octopus-picker-"));
  const cwd = path.join(fixture, "current");
  const repo = path.join(fixture, "repo");
  const team = path.join(repo, "team");
  const archive = path.join(repo, "archive");
  const report = "Report 中文 %#.md";
  try {
    for (const directory of [cwd, team, archive]) mkdirSync(directory, { recursive: true });
    writeFileSync(path.join(cwd, "current.md"), "# Current working directory");
    writeFileSync(path.join(previewDirectory, "workspace.md"), "# Workspace fallback");
    writeFileSync(path.join(team, report), "# Team report\n\n![Team diagram](../diagram.svg)");
    writeFileSync(path.join(archive, report), "# Archived report");
    writeFileSync(path.join(repo, "diagram.svg"), '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="blue" /></svg>');
    symlinkSync(archive, path.join(repo, "linked-archive"));
    for (let i = 0; i < 120; i++) writeFileSync(path.join(archive, `Budget-${i}.md`), "");
    const setExtraRoots = await editExtraRoots(request, account.user_id);
    await setExtraRoots([cwd, repo, team]);
    const id = await createSession(request, cwd, "File picker session");
    const history = async () => {
      const response = await request.get(`${API}/sessions/${id}/messages?before_seq=10000`, { headers });
      expect(response.ok()).toBeTruthy();
      return (await response.json()).messages;
    };
    const before = await history();
    const resolutions: { text: string; status: number }[] = [];
    const submissions: string[] = [];
    page.on("response", res => {
      if (new URL(res.url()).pathname.endsWith("/showme/resolve")) {
        resolutions.push({ text: res.request().postDataJSON().text, status: res.status() });
      }
    });
    page.on("websocket", socket => socket.on("framesent", ({ payload }) => {
      if (String(payload).includes('"send_message"')) submissions.push(String(payload));
    }));

    await signIn(page);
    await openSession(page, "File picker session");
    const input = page.locator(".chat-input-bar textarea");
    await input.fill("/showm");
    await expect(page.getByRole("option", { name: /showme.*Choose a file or enter its path/ })).toBeVisible();
    await showFile(page, "");
    const dialog = page.getByRole("dialog");
    const close = async () => {
      await dialog.getByRole("button", { name: "Close", exact: true }).click();
      await expect(dialog).toHaveCount(0);
    };
    const pickerTitle = dialog.getByRole("heading", { name: "Choose a file" });
    const source = dialog.getByRole("combobox", { name: "Source" });
    const search = dialog.getByRole("searchbox", { name: "Search filenames" });
    const breadcrumb = dialog.getByRole("navigation", { name: "Directory" });
    await expect(pickerTitle).toBeFocused();
    await expect(source).toHaveValue(cwd);
    await expect(dialog.getByRole("button", { name: "current.md" })).toBeVisible();
    await source.selectOption(repo);
    await dialog.getByRole("button", { name: "team", exact: true }).click();
    await expect(breadcrumb.getByText("team", { exact: true })).toHaveAttribute("aria-current", "location");
    await expect(dialog.getByRole("button", { name: report, exact: true })).toBeVisible();
    await search.fill("No filename like this");
    await expect(dialog.getByText("No matching files.")).toBeVisible();
    await expect(search).toHaveValue("No filename like this");
    await search.fill("budget");
    await expect(dialog.getByText(/Search limit reached/)).toBeVisible();
    await search.fill("rEpOrT 中文");
    const matches = dialog.getByRole("list").getByRole("button");
    await expect(matches).toHaveCount(2);
    await expect(dialog.getByText(/Some entries were skipped/)).toBeVisible();
    await expect(matches.filter({ hasText: "archive" })).toContainText(report);
    await matches.filter({ hasText: "team" }).click();
    await expect(dialog.getByRole("heading", { name: "Team report" })).toBeVisible();
    await expect(dialog.getByText(path.join(team, report), { exact: true })).toBeVisible();
    const image = dialog.getByRole("img", { name: "Team diagram" });
    await expect.poll(() => image.evaluate(node => (node as HTMLImageElement).naturalWidth)).toBe(16);
    expect(new URL(await image.getAttribute("src") ?? "", page.url()).searchParams.get("source")).toBe(repo);
    expect(resolutions).toHaveLength(0);
    await close();

    const explicitPath = `${JSON.stringify(path.join(team, report))} --source ${JSON.stringify(repo)}`;
    await showFile(page, explicitPath);
    await expect(dialog.getByRole("heading", { name: "Team report" })).toBeVisible();
    await close();
    await showFile(page, "missing.md");
    await expect(dialog.getByRole("alert")).toHaveText("File not found.");
    await setExtraRoots([repo, team]);
    await dialog.getByRole("button", { name: "Choose a file" }).click();
    await expect(pickerTitle).toBeFocused();
    await expect(dialog.getByText("The session working directory is no longer available.")).toBeVisible();
    await expect(source.getByRole("option", { name: cwd })).toHaveCount(0);
    const workspace = await request.get(`${API}/auth/workspace`, { headers });
    expect(workspace.ok()).toBeTruthy();
    await source.selectOption((await workspace.json()).workspace);
    await search.fill("workspace.md");
    await dialog.getByRole("button", { name: /workspace.md/ }).click();
    await expect(dialog.getByRole("heading", { name: "Workspace fallback" })).toBeVisible();
    await close();
    expect(resolutions).toEqual([{ text: explicitPath, status: 200 }, { text: "missing.md", status: 404 }]);
    expect(submissions).toEqual([]);
    expect(await history()).toEqual(before);
  } finally {
    rmSync(fixture, { recursive: true, force: true });
  }
});

previewTest("ten historical file links open from a saved reply and preserve the chat", async ({ page, request, account, previewDirectory }) => {
  const repository = mkdtempSync(path.join(os.tmpdir(), "octopus-chat-link-repository-"));
  try {
    if (!account) throw new Error("Expected an isolated account");
    const setExtraRoots = await editExtraRoots(request, account.user_id);
    await setExtraRoots([repository]);
    const encodePath = (filename: string) => filename.split("/").map(encodeURIComponent).join("/");
    const report = path.join(previewDirectory, "原始 报告 %23#:.md");
    writeFileSync(report, "# Historical report\n");
    const fixtures = [
      ["账户夹具", "web/e2e/test.ts", 10, "absolute"],
      ["帧等待", "web/e2e/chat-follow-output.spec.ts", 155, "file"],
      ["当前发布规则", "docs/testing.md", 389, "fragment"],
      ["65 秒回归", "tests/test_question_codex_real.py", 85, "absolute"],
      ["报告测试", "tests/test_check_hooks.py", 290, "absolute"],
      ["全局夹具", "tests/conftest.py", 171, "absolute"],
      ["参数矩阵", "web/tests/e2eOwnership.test.ts", 122, "absolute"],
      ["注册响应", "server/routers/auth.py", 172, "absolute"],
      ["发布 trace 配置", "web/e2e/run.ts", 92, "relative"],
    ] as const;
    const files = fixtures.map(([label, filename, line, format]) => {
      const absolute = path.join(format === "relative" ? previewDirectory : repository, filename);
      mkdirSync(path.dirname(absolute), { recursive: true });
      const marker = `Historical source for ${label}`;
      writeFileSync(absolute, "\n".repeat(line - 1) + `# ${marker}\n`);
      const href = format === "file" ? `file:${encodePath(absolute)}:${line}`
        : format === "fragment" ? `${encodePath(absolute)}#L${line}`
        : format === "relative" ? `${filename}:${line}` : `${encodePath(absolute)}:${line}`;
      return { label, absolute, line, marker, link: `[${label}](${href})` };
    });
    const content = [
      ...Array.from({ length: 16 }, (_, index) => `Earlier paragraph ${index + 1}.\n`),
      `前文 [原始报告](${encodePath(report)})`,
      "",
      "| 优先级 | 建议 |", "| --- | --- |",
      `| 离线浏览器 | ${files[0].link} ${files[1].link} |`,
      `| 实机 | ${files[2].link} ${files[3].link} |`,
      `| pytest | ${files[4].link} ${files[5].link} |`,
      `| Vitest | ${files[6].link} |`,
      "",
      `后文 ${files[7].link} ${files[8].link}`,
      "",
      "[普通网页](./health) [页内片段](#details)",
      "[Malformed URI](bad%FF.md#L4) [External website](https://example.test/guide.md)",
      "",
      ...Array.from({ length: 28 }, (_, index) => `Later paragraph ${index + 1}.\n`),
    ].join("\n");
    const name = "Historical file reply";
    const imported = await request.post(`${API}/sessions/import`, { headers, data: {
      name, working_dir: previewDirectory, messages: [{ role: "assistant", type: "text", content }],
    } });
    expect(imported.status(), await imported.text()).toBe(201);
    const { id } = await imported.json();
    const readHistory = async () => {
      const saved = await request.get(`${API}/sessions/${id}/messages?before_seq=10000`, { headers });
      expect(saved.ok()).toBeTruthy();
      return (await saved.json()).messages;
    };
    const before = await readHistory();
    const fileRequests: string[] = [];
    const submissions: string[] = [];
    const commandPath = `/api/sessions/${id}/showme/resolve`;
    const commandResolutions: string[] = [];
    const completedResolutions: string[] = [];
    page.on("request", req => {
      const pathname = new URL(req.url()).pathname;
      if (pathname.startsWith(`/api/sessions/${id}/files`)) fileRequests.push(pathname);
      if (pathname === commandPath) commandResolutions.push(pathname);
    });
    page.on("response", res => {
      if (new URL(res.url()).pathname === commandPath && res.ok()) completedResolutions.push(res.url());
    });
    page.on("websocket", socket => socket.on("framesent", ({ payload }) => {
      if (String(payload).includes('"send_message"')) submissions.push(String(payload));
    }));

    await signIn(page);
    await openSession(page, name);
    const reply = page.locator(".msg-content.markdown");
    const buttonName = (label: string, line?: number) => `Open file: ${label}${line ? ` at line ${line}` : ""}`;
    const fileButton = (label: string, line?: number) => reply.getByRole("button", { name: buttonName(label, line), exact: true });
    await expect(reply.getByRole("button", { name: /^Open file:/ })).toHaveCount(10);
    await expect(reply.getByRole("link", { name: "Malformed URI" })).toHaveAttribute("href", "bad%FF.md#L4");
    await expect(reply.getByRole("link", { name: "External website" })).toHaveAttribute("href", "https://example.test/guide.md");
    await expect(reply.getByRole("table").getByRole("button")).toHaveCount(7);
    const firstCell = reply.getByRole("cell").filter({
      has: page.getByRole("button", { name: buttonName(files[0].label, files[0].line), exact: true }),
    });
    await expect(firstCell.getByRole("button")).toHaveCount(2);
    await expect(firstCell.getByRole("button", { name: buttonName(files[1].label, files[1].line), exact: true })).toBeVisible();
    await expect(reply.getByRole("link", { name: "页内片段" })).toHaveAttribute("href", "#details");
    expect(fileRequests).toEqual([]);
    const originalUrl = page.url();
    const dialog = page.getByRole("dialog");
    const close = async () => {
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
    };
    const first = fileButton("原始报告");
    await first.scrollIntoViewIfNeeded();
    await first.focus();
    const scrollPosition = () => page.locator(".chat-messages").evaluate(node => node.scrollTop);
    const readingPosition = await scrollPosition();
    expect(readingPosition).toBeGreaterThan(0);
    expect(await page.locator(".chat-messages").evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop))
      .toBeGreaterThan(30);
    await first.press("Enter");
    await expect(dialog.getByRole("heading", { name: "Historical report" })).toBeVisible();
    await expect(dialog.getByText(report, { exact: true })).toBeVisible();
    expect(await dialog.evaluate(node => node.contains(document.activeElement))).toBeTruthy();
    await close();
    await expect(first).toBeFocused();
    expect(await scrollPosition()).toBe(readingPosition);

    for (const [index, file] of files.entries()) {
      const button = fileButton(file.label, file.line);
      await expect(button).toHaveText(file.label);
      if (index === 0) {
        await button.focus();
        await button.press("Space");
      } else await button.click();
      await expect(dialog.getByText(file.absolute, { exact: true })).toBeVisible();
      const located = dialog.locator(`[data-line="${file.line}"][data-located="true"]`);
      await expect(located).toContainText(file.marker);
      await expect(located).toBeInViewport();
      await close();
      await expect(button).toBeFocused();
    }
    expect(commandResolutions).toEqual([]);

    const equivalent = files[6];
    await showFile(page, `${JSON.stringify(equivalent.absolute)}:${equivalent.line}`);
    await expect(dialog.getByText(equivalent.absolute, { exact: true })).toBeVisible();
    await expect(dialog.locator(`[data-line="${equivalent.line}"][data-located="true"]`)).toContainText(equivalent.marker);
    await close();
    await showFile(page, JSON.stringify(report));
    await expect(dialog.getByRole("heading", { name: "Historical report" })).toBeVisible();
    await expect(dialog.getByText(report, { exact: true })).toBeVisible();
    await close();

    expect(page.url()).toBe(originalUrl);
    expect(completedResolutions).toHaveLength(2);
    expect(submissions).toEqual([]);
    expect(before).toHaveLength(1);
    expect(await readHistory()).toEqual(before);
    const website = reply.getByRole("link", { name: "普通网页" });
    await expect(website).toHaveAttribute("href", "./health");
    const siteResponse = page.waitForResponse(res => new URL(res.url()).pathname === "/health" && res.request().isNavigationRequest());
    await website.click();
    expect((await siteResponse).ok()).toBeTruthy();
    await expect(page).toHaveURL(/\/health$/);
  } finally {
    rmSync(repository, { recursive: true, force: true });
  }
});

previewTest("opens an archived report and its image while keeping the foreground chat", async ({ page, context, request, previewDirectory: directory }) => {
  const archiveDirectory = path.join(directory, "archive");
  const liveDirectory = path.join(directory, "foreground");
  mkdirSync(archiveDirectory);
  mkdirSync(liveDirectory);
  writeFileSync(path.join(archiveDirectory, "report.md"), "# Archived source\n\n![Archived diagram](diagram.png)");
  writeFileSync(path.join(liveDirectory, "report.md"), "# Foreground source");
  writeFileSync(path.join(archiveDirectory, "diagram.png"), png);
  const archiveId = await createSession(request, archiveDirectory, "Archived file source");
  const liveId = await createSession(request, liveDirectory, "Foreground chat");
  expect((await request.post(`${API}/sessions/${archiveId}/archive?replace=false`, { headers })).status()).toBe(204);
  await signIn(page);
  await openSession(page, "Foreground chat");
  const foreground = page.locator(`[data-chat-session="${liveId}"]:not([inert])`);
  await expect(foreground).toBeVisible();
  const openArchive = async (tab: Page) => {
    await tab.getByRole("button", { name: "Account menu" }).click();
    await tab.getByRole("menuitem", { name: "Archived sessions" }).click();
    return tab.getByRole("dialog", { name: "Archived sessions" });
  };
  const archive = await openArchive(page);
  const archiveRow = (tab: Page) => tab.locator(".archived-session-row").filter({ hasText: "Archived file source" });
  const source = archiveRow(page);
  const openFile = source.getByRole("button", { name: "Open file", exact: true });
  await openFile.click();
  const viewer = page.getByRole("dialog", { name: "Open file" });
  const field = viewer.getByRole("textbox", { name: "File path" });
  await expect(field).toBeFocused();
  await field.fill("report.md");
  await viewer.getByRole("button", { name: "Open file", exact: true }).click();
  const report = page.getByRole("dialog", { name: "report.md" });
  await expect(report.getByRole("heading", { name: "Archived source" })).toBeVisible();
  const image = report.getByRole("img", { name: "Archived diagram" });
  await expect.poll(() => image.evaluate(element => (element as HTMLImageElement).naturalWidth)).toBe(1);
  expect(new URL((await image.getAttribute("src"))!).pathname).toBe(`/api/sessions/${archiveId}/files`);
  await expect(foreground).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(report).toHaveCount(0);
  await expect(openFile).toBeFocused();
  await openFile.click();
  await expect(field).toBeFocused();
  const otherTab = await context.newPage();
  try {
    await otherTab.goto("/");
    await openArchive(otherTab);
    await archiveRow(otherTab).getByRole("button", { name: "Unarchive", exact: true }).click();
    await expect(source).toHaveCount(0);
  } finally {
    await otherTab.close();
  }
  await expect(viewer).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(viewer).toHaveCount(0);
  await expect(archive.getByRole("button", { name: "Close", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(archive).toHaveCount(0);
  await expect(foreground.locator(".chat-input-bar textarea")).toBeVisible();
});

previewTest("showme reopens positioned Markdown and corrects literal paths separately", async ({ page, context, request, previewDirectory: directory }) => {
  const filename = "计划.md#附录 25%.md";
  const absolute = path.join(directory, filename);
  writeFileSync(absolute, "# Plan\n\n**Source target**\n\nFourth paragraph\n");
  const sessionId = await createSession(request, directory, "Positioned Markdown");
  await signIn(page);
  await openSession(page, "Positioned Markdown");
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const dialog = page.getByRole("dialog");
  const line = dialog.locator('[data-line="3"]');
  const source = dialog.getByRole("button", { name: "Source", exact: true });
  const reading = dialog.getByRole("button", { name: "Reading", exact: true });
  const close = async () => dialog.getByRole("button", { name: "Close", exact: true }).click();
  const readClipboard = () => page.evaluate(() => navigator.clipboard.readText());
  let reads = 0;
  page.on("request", (req) => {
    if (new URL(req.url()).pathname === `/api/sessions/${sessionId}/files`) reads++;
  });

  await showFile(page, `${JSON.stringify(filename)}#L3-L5`);
  await expect(source).toHaveAttribute("aria-pressed", "true");
  await expect(line).toContainText("**Source target**");
  await expect(line).toBeInViewport();
  expect(await line.evaluate((node) => getComputedStyle(node, "::before").content)).toContain("▸ 3");
  await expect(dialog.locator('[data-line="5"]')).toHaveAttribute("data-located", "true");
  const readsBeforeSwitching = reads;
  expect(readsBeforeSwitching).toBeGreaterThan(0);
  await reading.click();
  await expect(dialog.getByRole("heading", { name: "Plan", exact: true })).toBeVisible();
  await source.click();
  await expect(line).toContainText("**Source target**");
  expect(reads).toBe(readsBeforeSwitching);

  await dialog.getByRole("button", { name: "Copy /showme command" }).click();
  await expect(dialog.getByRole("status")).toHaveText("Command copied.");
  const command = await readClipboard();
  expect(command).toBe(`/showme ${JSON.stringify(absolute)} --source ${JSON.stringify(directory)}#L3-L5`);
  await dialog.getByRole("button", { name: "Copy path" }).click();
  await expect(dialog.getByRole("status")).toHaveText("Path copied.");
  expect(await readClipboard()).toBe(absolute);
  await close();

  await page.locator(".chat-input-bar textarea").fill(command);
  await page.locator("button.btn-send").click();
  await expect(line).toBeInViewport();
  await close();

  for (const [target, error] of [
    ["../../计划.md#附录 25%.md", "You don't have access to this file."],
    ["missing 25%#附录.md", "File not found."],
  ]) {
    await showFile(page, `${JSON.stringify(target)}:3`);
    await expect(dialog.getByRole("alert")).toHaveText(error);
    await dialog.getByRole("button", { name: "Edit path" }).click();
    const field = dialog.getByRole("textbox", { name: "File path" });
    await expect(field).toHaveValue(target);
    const position = dialog.getByRole("textbox", { name: "Position (optional)" });
    await expect(position).toHaveValue(":3");
    await field.fill("");
    await field.press("Control+V");
    await expect(field).toHaveValue(absolute);
    await position.fill("#L3-L5");
    await dialog.getByRole("button", { name: "Open file", exact: true }).click();
    await expect(line).toBeInViewport();
    await close();
  }

  for (const [suffix, note, view] of [
    [":0", "The requested file position is invalid.", "Reading"],
    ["#L9-L3", "The requested file position is invalid.", "Reading"],
    ["#missing", "The requested heading wasn't found.", "Reading"],
    [":99", "The requested lines are outside this file.", "Source"],
  ]) {
    await showFile(page, `${JSON.stringify(filename)}${suffix}`);
    await expect(dialog.getByRole("note")).toHaveText(note);
    await expect(dialog.getByRole("button", { name: view, exact: true })).toHaveAttribute("aria-pressed", "true");
    if (view === "Reading") await expect(dialog.getByRole("heading", { name: "Plan", exact: true })).toBeVisible();
    else await expect(line).toBeVisible();
    await dialog.getByRole("button", { name: "Copy /showme command" }).click();
    await expect(dialog.getByRole("status")).toHaveText("Command copied.");
    expect(await readClipboard()).toBe(`/showme ${JSON.stringify(absolute)} --source ${JSON.stringify(directory)}`);
    await close();
  }
});

previewTest("source copy preserves mixed newlines, wrapping and multiline syntax", async ({ page, context, request, previewDirectory: directory }) => {
  const longLine = `second ${"word ".repeat(90)}continuation`;
  const raw = `const marker = 1; /* first\r\n${longLine}\rthird */\n\nconst tail = 2;\n`;
  writeFileSync(path.join(directory, "sample.js"), raw);
  writeFileSync(path.join(directory, "notes.txt"), "plain first\rplain second\n");
  await createSession(request, directory, "Source clipboard");
  await page.setViewportSize({ width: 900, height: 700 });
  await page.addInitScript(() => {
    const send = Worker.prototype.postMessage;
    Worker.prototype.postMessage = function (this: Worker, message: unknown, options?: StructuredSerializeOptions | Transferable[]) {
      const data = message as { language?: unknown; text?: unknown } | null;
      const deliver = () => Reflect.apply(send, this, [message, options]);
      if (data?.language !== "javascript" || typeof data.text !== "string") {
        deliver();
        return;
      }
      document.documentElement.dataset.sourceSyntaxWaiting = "true";
      this.addEventListener("message", () => window.dispatchEvent(new Event("source-syntax-ready")), { once: true });
      window.addEventListener("start-source-syntax", deliver, { once: true });
    };
  });
  await signIn(page);
  await openSession(page, "Source clipboard");
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await showFile(page, "sample.js#L2-L4");

  const dialog = page.getByRole("dialog");
  const source = dialog.locator("pre code");
  const comment = source.locator('[data-line="2"] .hljs-comment');
  const expected = `first\r\n${longLine}\rthird */\n\nconst tail = 2;\n`;
  await expect(source.locator('[data-line="2"]')).toContainText(longLine);
  await expect(page.locator("html")).toHaveAttribute("data-source-syntax-waiting", "true");
  await source.evaluate((code) => {
    const start = code.querySelector('[data-line="1"]')?.firstChild;
    if (!start) throw new Error("The first line should be visible before highlighting");
    window.getSelection()!.setBaseAndExtent(code, code.childNodes.length, start, "const marker = 1; /* ".length);
  });
  await page.evaluate(() => new Promise<void>((resolve) => {
    window.addEventListener("source-syntax-ready", () => resolve(), { once: true });
    window.dispatchEvent(new Event("start-source-syntax"));
  }));
  await expect(comment).toHaveCount(0);
  await page.keyboard.press("Control+c");
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(expected);
  await page.evaluate(() => {
    window.getSelection()?.removeAllRanges();
    return navigator.clipboard.writeText("");
  });

  await expect(comment).toContainText(longLine);
  await expect(source.locator('[data-line="3"] .hljs-comment')).toContainText("third */");
  await expect(source.locator('[data-line="4"]')).toHaveAttribute("data-located", "true");
  const heights = await source.locator("[data-line]").evaluateAll((rows) => rows.map((row) => row.getBoundingClientRect().height));
  expect(heights).toHaveLength(5);
  expect(heights[1]).toBeGreaterThan(heights[2]);
  await source.evaluate((code) => {
    const start = code.querySelector('[data-line="1"] .hljs-comment')?.firstChild;
    if (!start) throw new Error("The first line of the block comment should be highlighted");
    window.getSelection()!.setBaseAndExtent(code, code.childNodes.length, start, 3);
  });
  await page.keyboard.press("Control+c");
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(expected);

  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await showFile(page, "notes.txt:2");
  const plain = dialog.getByText("plain second", { exact: true });
  await expect(plain).toBeInViewport();
  await expect(plain).toHaveAttribute("data-line", "2");
  await expect(plain).toHaveAttribute("data-located", "true");
});

previewTest("reading and source retain the chosen section and copy a long source range", async ({ page, context, request, previewDirectory: directory }) => {
  const rows = ["# Sections", ...Array.from({ length: 4000 }, (_, index) => `Line ${index + 2}`)];
  writeFileSync(path.join(directory, "sections.md"), rows.join("\r\n"));
  const sessionId = await createSession(request, directory, "Source sections");
  await signIn(page);
  await openSession(page, "Source sections");
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const dialog = page.getByRole("dialog");
  const navigation = dialog.getByRole("navigation", { name: "Source line sections" });
  let reads = 0;
  page.on("request", (req) => {
    if (new URL(req.url()).pathname === `/api/sessions/${sessionId}/files`) reads++;
  });

  await showFile(page, "sections.md:2500");
  await expect(dialog.getByText("Line 2500", { exact: true })).toBeInViewport();
  const readsBeforeSwitching = reads;
  await navigation.getByRole("button", { name: "Next lines" }).click();
  await expect(navigation).toContainText("4001–4001");
  await dialog.getByRole("button", { name: "Reading", exact: true }).click();
  await expect(dialog.getByRole("heading", { name: "Sections", exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "Source", exact: true }).click();
  await expect(navigation).toContainText("4001–4001");
  await expect(dialog.getByText("Line 4001", { exact: true })).toBeInViewport();
  expect(reads).toBe(readsBeforeSwitching);

  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await showFile(page, "sections.md#L1-L2001");
  await navigation.getByRole("button", { name: "Copy highlighted lines" }).click();
  await expect(dialog.getByRole("status")).toHaveText("Highlighted lines copied.");
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(rows.slice(0, 2001).join("\r\n") + "\r\n");
});

previewTest("copied showme commands and position edits retain the canonical file and authorized source", async ({ page, context, request, account, previewDirectory: directory }) => {
  if (!account) throw new Error("The file source story requires an offline account");
  const repo = mkdtempSync(path.join(os.tmpdir(), 'showme position "% source-'));
  try {
    const team = path.join(repo, "team");
    const alias = path.join(repo, "alias.md");
    const canonical = path.join(team, "plan.md");
    const guide = path.join(repo, "guide.md");
    mkdirSync(team);
    writeFileSync(canonical, [
      "## Target position", "![Shared diagram](../diagram.svg)", spacing, "## Second position",
      "[Open source line](./README:12)", "[Open literal colon](./README%3A12)",
    ].join("\n\n"));
    writeFileSync(path.join(team, "repointed.md"), "# Repointed alias");
    writeFileSync(guide, "[Follow team plan](./team/plan.md#target-position)");
    symlinkSync("team/plan.md", alias);
    writeFileSync(path.join(team, "README"), "first\n".repeat(11) + "The twelfth source line\n");
    writeFileSync(path.join(team, "colon.md"), "# Literal colon alias");
    symlinkSync("colon.md", path.join(team, "README:12"));
    writeFileSync(path.join(repo, "diagram.svg"), '<svg xmlns="http://www.w3.org/2000/svg" width="13" height="8"><rect width="13" height="8"/></svg>');
    const setExtraRoots = await editExtraRoots(request, account.user_id);
    await setExtraRoots([repo, team]);
    const sessionId = await createSession(request, directory, "Positioned source alias");
    await signIn(page);
    await openSession(page, "Positioned source alias");
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    const dialog = page.getByRole("dialog");
    const heading = dialog.getByRole("heading", { name: "Target position" });
    const diagram = dialog.getByRole("img", { name: "Shared diagram" });
    const expectDiagram = () => expect.poll(() => diagram.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(13);
    const close = () => dialog.getByRole("button", { name: "Close", exact: true }).click();
    const copyCommand = async (position: string) => {
      await dialog.getByRole("button", { name: "Copy /showme command" }).click();
      await expect(dialog.getByRole("status")).toHaveText("Command copied.");
      const command = await page.evaluate(() => navigator.clipboard.readText());
      expect(command).toBe(`/showme ${JSON.stringify(canonical)} --source ${JSON.stringify(repo)}${position}`);
      return command;
    };
    const reopen = async (command: string) => {
      const resolved = page.waitForResponse((resp) => new URL(resp.url()).pathname === `/api/sessions/${sessionId}/showme/resolve` && resp.ok());
      await page.locator(".chat-input-bar textarea").fill(command);
      await page.locator("button.btn-send").click();
      expect((await (await resolved).json()).metadata.source).toBe(repo);
    };

    await showFile(page, `${JSON.stringify(alias)}#target-position`);
    await expect(heading).toBeInViewport();
    await expectDiagram();
    symlinkSync("team/repointed.md", alias + ".new");
    renameSync(alias + ".new", alias);
    const command = await copyCommand("#target-position");
    await close();
    await reopen(command);
    await expect(heading).toBeInViewport();
    await expectDiagram();
    await close();

    await showFile(page, JSON.stringify(guide));
    await dialog.getByRole("button", { name: /^Open file: Follow team plan/ }).click();
    await expect(heading).toBeInViewport();
    await expectDiagram();
    const linked = await copyCommand("#target-position");

    await dialog.getByRole("button", { name: "Edit path" }).click();
    await expect(dialog.getByRole("textbox", { name: "File path" })).toHaveValue(canonical);
    const position = dialog.getByRole("textbox", { name: "Position (optional)" });
    await expect(position).toHaveValue("#target-position");
    await position.fill("#second-position");
    await dialog.getByRole("button", { name: "Open file", exact: true }).click();
    await expect(dialog.getByRole("heading", { name: "Second position" })).toBeInViewport();
    await expect(heading).not.toBeInViewport();
    await expectDiagram();
    await close();

    await reopen(linked);
    await expect(heading).toBeInViewport();
    await expectDiagram();

    await dialog.getByRole("button", { name: /^Open file: Open source line/ }).click();
    const line = dialog.getByText("The twelfth source line", { exact: true });
    await expect(line).toBeInViewport();
    await expect(line).toHaveAttribute("data-located", "true");
    await close();

    await reopen(linked);
    await dialog.getByRole("button", { name: /^Open file: Open literal colon/ }).click();
    await expect(dialog.getByRole("heading", { name: "Literal colon alias" })).toBeVisible();
  } finally {
    rmSync(repo, { recursive: true, force: true });
  }
});

for (const editBeforeDenial of [true, false]) {
  previewTest(`a position edit ${editBeforeDenial ? "before" : "after"} body denial retains its source`, async ({ page, request, account, previewDirectory: directory }) => {
    if (!account) throw new Error("The source revocation story requires an offline account");
    const repo = mkdtempSync(path.join(os.tmpdir(), "showme-edit-revocation-"));
    try {
      const team = path.join(repo, "team");
      const canonical = path.join(team, "plan.md");
      mkdirSync(team);
      writeFileSync(canonical, "# Plan\n\n## Alternative section");
      const setExtraRoots = await editExtraRoots(request, account.user_id);
      await setExtraRoots(editBeforeDenial ? [repo, team] : [repo]);
      const sessionId = await createSession(request, directory, "Position edit during revocation");
      await signIn(page);
      await openSession(page, "Position edit during revocation");

      const files = `/api/sessions/${sessionId}/files`;
      const planBody = (url: URL) => url.pathname === files && url.searchParams.get("path") === "team/plan.md";
      let release!: () => void;
      const canRead = new Promise<void>((resolve) => { release = resolve; });
      await page.route(planBody, async (route) => {
        await canRead;
        await route.continue();
      });
      const bodyStarted = page.waitForRequest((req) => planBody(new URL(req.url())));
      const bodyDenied = page.waitForResponse((res) => planBody(new URL(res.url())) && res.status() === 403);
      const dialog = page.getByRole("dialog");
      const edit = dialog.getByRole("button", { name: "Edit path" });
      const field = dialog.getByRole("textbox", { name: "File path" });
      try {
        await showFile(page, `${JSON.stringify(canonical)} --source ${JSON.stringify(repo)}#plan`);
        await bodyStarted;
        if (editBeforeDenial) {
          await edit.click();
          await expect(field).toHaveValue(canonical);
        }
        await setExtraRoots([team]);
      } finally {
        release();
      }
      await bodyDenied;
      await expect(dialog.getByRole("alert")).toHaveText("You don't have access to this file.");
      if (!editBeforeDenial) {
        await edit.click();
        await expect(field).toHaveValue(canonical);
      }
      const position = dialog.getByRole("textbox", { name: "Position (optional)" });
      await expect(position).toHaveValue("#plan");
      await position.fill("#alternative-section");
      const reopened = page.waitForResponse((res) => {
        const url = new URL(res.url());
        return url.pathname === `${files}/meta` && url.searchParams.get("path") === canonical;
      });
      await dialog.getByRole("button", { name: "Open file", exact: true }).click();
      const response = await reopened;
      expect(new URL(response.url()).searchParams.get("source")).toBe(repo);
      expect(response.status()).toBe(403);
      await expect(dialog.getByRole("alert")).toHaveText("You don't have access to this file.");
    } finally {
      rmSync(repo, { recursive: true, force: true });
    }
  });
}

previewTest("a position edit started before metadata uses the confirmed source", async ({ page, request, account, previewDirectory: directory }) => {
  if (!account) throw new Error("The source binding story requires an offline account");
  const repo = mkdtempSync(path.join(os.tmpdir(), "showme-editor-source-"));
  try {
    const team = path.join(repo, "team");
    const canonical = path.join(team, "plan.md");
    mkdirSync(team);
    writeFileSync(canonical, "# Plan\n\n## Alternative section");
    const setExtraRoots = await editExtraRoots(request, account.user_id);
    await setExtraRoots([repo]);
    const name = "Position edit before metadata";
    const imported = await request.post(`${API}/sessions/import`, { headers, data: {
      name, working_dir: directory,
      messages: [{ role: "assistant", type: "text", content: `[Open team plan](${encodeURI(canonical)})` }],
    } });
    expect(imported.status()).toBe(201);
    const { id: sessionId } = await imported.json();
    await signIn(page);
    await openSession(page, name);
    const dialog = page.getByRole("dialog");

    const planMeta = (url: URL) => url.pathname === `/api/sessions/${sessionId}/files/meta` && url.searchParams.get("path") === canonical;
    let release!: () => void;
    const canRead = new Promise<void>((resolve) => { release = resolve; });
    await page.route(planMeta, async (route) => {
      await canRead;
      await route.continue();
    });
    const edit = dialog.getByRole("button", { name: "Edit path" });
    const field = dialog.getByRole("textbox", { name: "File path" });
    const open = dialog.getByRole("button", { name: "Open file", exact: true });
    const initialMeta = page.waitForRequest((req) => planMeta(new URL(req.url())));
    try {
      await page.getByRole("button", { name: "Open file: Open team plan", exact: true }).click();
      await initialMeta;
      await edit.click();
      await expect(field).toHaveValue(canonical);
    } finally {
      release();
    }
    await expect(dialog.getByRole("heading", { name: "Plan", exact: true })).toBeVisible();
    await setExtraRoots([repo, team]);
    await dialog.getByRole("textbox", { name: "Position (optional)" }).fill("#alternative-section");
    const reopened = page.waitForResponse((res) => planMeta(new URL(res.url())) && res.ok());
    await open.click();
    const response = await reopened;
    expect(new URL(response.url()).searchParams.get("source")).toBe(repo);
    expect((await response.json()).source).toBe(repo);
    await expect(dialog.getByRole("heading", { name: "Alternative section", exact: true })).toBeInViewport();
  } finally {
    rmSync(repo, { recursive: true, force: true });
  }
});

previewTest("editing a denied Markdown target keeps its original source and parent directory", async ({
  page, request, account, previewDirectory: directory,
}) => {
  if (!account) throw new Error("The failed path edit requires an offline account");
  const repo = mkdtempSync(path.join(os.tmpdir(), "showme-candidate-retry-"));
  try {
    const docs = path.join(repo, "docs");
    const team = path.join(repo, "team");
    const guide = path.join(docs, "guide.md");
    mkdirSync(docs);
    mkdirSync(team);
    writeFileSync(guide, "# Guide\n\n[Open team plan](../team/plan.md#plan)");
    writeFileSync(path.join(team, "plan.md"), "# Plan");
    const setExtraRoots = await editExtraRoots(request, account.user_id);
    await setExtraRoots([repo, team]);
    const name = "Source binding for a candidate retry";
    const session = await createSession(request, directory, name);
    await signIn(page);
    await openSession(page, name);
    await showFile(page, `${JSON.stringify(guide)} --source ${JSON.stringify(repo)}`);

    const files = `/api/sessions/${session}/files`;
    const planMeta = (url: URL) => url.pathname === `${files}/meta` && url.searchParams.get("path") === "../team/plan.md";
    const planBody = (url: URL) => url.pathname === files && url.searchParams.get("path") === "team/plan.md";
    let release!: () => void;
    let byteRequests = 0;
    const canRead = new Promise<void>((resolve) => { release = resolve; });
    await page.route(planBody, async (route) => {
      byteRequests++;
      await canRead;
      await route.continue();
    });
    const dialog = page.getByRole("dialog");
    const link = dialog.getByRole("button", { name: /^Open file: Open team plan/ });
    const initialMeta = page.waitForResponse((res) => planMeta(new URL(res.url())));
    const bodyStarted = page.waitForRequest((req) => planBody(new URL(req.url())));
    const bodyDenied = page.waitForResponse((res) => planBody(new URL(res.url())) && res.status() === 403);
    try {
      await link.press("Enter");
      await bodyStarted;
      await dialog.getByRole("button", { name: "Edit path", exact: true }).click();
      await expect(dialog.getByRole("textbox", { name: "File path" })).toHaveValue(guide);
      await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
      const cancelOpening = dialog.getByRole("button", { name: "Cancel opening plan.md" });
      await cancelOpening.focus();
      await expect(cancelOpening).toBeFocused();
      await setExtraRoots([team]);
    } finally {
      release();
    }
    const denied = await bodyDenied;
    const alert = dialog.getByRole("alert");
    await expect(alert).toContainText("You don't have access to this file.");
    await expect(dialog.getByRole("tab", { name: "guide.md" })).toBeFocused();
    await expect(dialog.getByRole("button", { name: /Retry opening/ })).toHaveCount(0);
    await dialog.getByRole("button", { name: /Edit path for/ }).click();
    await expect(dialog.getByRole("textbox", { name: "File path" })).toHaveValue("../team/plan.md");
    await expect(dialog.getByRole("textbox", { name: "Position (optional)" })).toHaveValue("#plan");
    const confirmation = page.waitForResponse((res) => planMeta(new URL(res.url())));
    await dialog.getByRole("button", { name: "Open file", exact: true }).click();
    const [initial, reopened] = await Promise.all([initialMeta, confirmation]);
    expect(initial.status()).toBe(200);
    expect(reopened.status()).toBe(403);
    expect(new URL(denied.url()).searchParams.get("canonical_path_id")).toBe((await initial.json()).canonical_path_id);
    const originalBinding = new URL(initial.url()).searchParams;
    const reopenedBinding = new URL(reopened.url()).searchParams;
    expect(reopenedBinding.get("source")).toBe(repo);
    expect(reopenedBinding.get("source_directory")).toBe("docs");
    expect(reopenedBinding.get("source_directory_id")).toBeTruthy();
    expect(reopenedBinding.get("source_directory_id")).toBe(originalBinding.get("source_directory_id"));
    await expect(dialog.getByRole("heading", { name: "Guide", exact: true })).toBeVisible();
    await expect(alert).toContainText("You don't have access to this file.");
    expect(byteRequests).toBe(1);
  } finally {
    rmSync(repo, { recursive: true, force: true });
  }
});

previewTest("showme can reach a source line near the end of an exceptionally long file", async ({ page, request, previewDirectory: directory }) => {
  writeFileSync(path.join(directory, "README"), "\n".repeat(1_400_000) + "TARGET SOURCE LINE\n" + "\n".repeat(100));
  await createSession(request, directory, "Long source file");
  await signIn(page);
  await openSession(page, "Long source file");
  await page.setViewportSize({ width: 320, height: 520 });
  await showFile(page, "README:1400001");

  const dialog = page.getByRole("dialog");
  const last = dialog.getByText("TARGET SOURCE LINE", { exact: true });
  const navigation = dialog.getByRole("navigation", { name: "Source line sections" });
  await expect(last).toBeInViewport();
  await expect(last).toHaveAttribute("data-line", "1400001");
  const initialNav = (await navigation.boundingBox())!;
  expect.soft((await last.boundingBox())!.y).toBeGreaterThanOrEqual(initialNav.y + initialNav.height - 1);
  const marker = await last.evaluate(async (row) => {
    await document.fonts.ready;
    const style = getComputedStyle(row, "::before");
    const canvas = document.createElement("canvas").getContext("2d")!;
    canvas.font = style.font;
    const markerText = style.content.slice(1, -1);
    const rightEdge = row.getBoundingClientRect().left + Number.parseFloat(style.width);
    const clipEdge = row.closest("pre")!.getBoundingClientRect().left;
    return { text: markerText, fits: rightEdge - canvas.measureText(markerText).width >= clipEdge };
  });
  expect.soft(marker).toEqual({ text: "▸ 1400001", fits: true });
  await dialog.getByRole("button", { name: "Previous lines" }).click();
  await expect(navigation).toContainText("1398001–1400000");
  await expect(last).toHaveCount(0);

  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await page.setViewportSize({ width: 360, height: 520 });
  await showFile(page, "README#L1399999-L1400001");
  await expect(last).toBeInViewport();
  await expect(dialog.locator('[data-located="true"]')).toHaveCount(3);
  const firstMarked = dialog.locator('[data-line="1399999"]');
  const navBounds = (await navigation.boundingBox())!;
  expect((await firstMarked.boundingBox())!.y).toBeGreaterThanOrEqual(navBounds.y + navBounds.height - 1);
});

previewTest("one workspace chat opens explicit paths from workspace and repository and edits a missing target", async ({ page, context, request, account, previewDirectory }) => {
  if (!account) throw new Error("The repository fixture needs its own account");
  const repository = mkdtempSync(path.join(os.tmpdir(), "showme-repository-"));
  const filename = "中文 %20#计划.md";
  const workspaceFile = path.join(previewDirectory, filename);
  const repositoryFile = path.join(repository, filename);
  try {
    const setExtraRoots = await editExtraRoots(request, account.user_id);
    await setExtraRoots([repository]);
    writeFileSync(workspaceFile, "# Workspace plan\n\nRead the **workspace** copy.");
    writeFileSync(repositoryFile, "# Repository plan");
    const guide = path.join(repository, "docs", "guide.md");
    mkdirSync(path.dirname(guide));
    writeFileSync(guide, "[Open missing plan](missing.md)");
    writeFileSync(path.join(repository, "docs", "plan.md"), "# Relative repository plan");
    writeFileSync(path.join(previewDirectory, "plan.md"), "# Unrelated workspace plan");
    const crFile = path.join(repository, "literal\r.md");
    const backslashFile = path.join(repository, "literal\\r.md");
    const typedRepositoryFile = repositoryFile.replace(/\.md$/, "x.md");
    const editedCrFile = path.join(repository, "literal\n\r.md");
    const continuedCrFile = path.join(repository, "literal\r\nx.md");
    writeFileSync(crFile, "# Carriage return name");
    writeFileSync(backslashFile, "# Literal backslash name");
    writeFileSync(typedRepositoryFile, "# Typed repository plan");
    writeFileSync(path.join(repository, "literal\n.md"), "# Line feed name");
    writeFileSync(editedCrFile, "# Line feed before carriage return");
    writeFileSync(path.join(repository, "literal\r\n.md"), "# Carriage return before line feed");
    writeFileSync(continuedCrFile, "# Continued edit");
    writeFileSync(path.join(repository, "literal\r\n.mdx"), "# Wrong continued edit");
    const sessionId = await createSession(request, previewDirectory, "Explicit file paths");
    await signIn(page);
    await openSession(page, "Explicit file paths");
    const originalUrl = page.url();
    const dialog = page.getByRole("dialog");

    await showFile(page, JSON.stringify(filename));
    await expect(dialog.getByRole("heading", { name: "Workspace plan" })).toBeVisible();
    await expect(dialog.locator("strong")).toHaveText("workspace");
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await showFile(page, JSON.stringify(repositoryFile));
    await expect(dialog.getByRole("heading", { name: "Repository plan" })).toBeVisible();
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await showFile(page, "中文 缺失.md");
    await expect(dialog.getByRole("alert")).toHaveText("File not found.");
    await dialog.getByRole("button", { name: "Edit path" }).click();
    const field = dialog.getByRole("textbox", { name: "File path" });
    await expect(field).toHaveValue("中文 缺失.md");
    await field.fill(repositoryFile);
    await dialog.getByRole("button", { name: "Open file", exact: true }).click();
    await expect(dialog.getByRole("heading", { name: "Repository plan" })).toBeVisible();
    await dialog.getByRole("button", { name: "Close", exact: true }).click();

    await showFile(page, JSON.stringify(guide));
    await dialog.getByRole("button", { name: /^Open file: Open missing plan/ }).click();
    await expect(dialog.getByRole("alert")).toHaveText("Couldn't open missing.md. File not found.");
    await dialog.getByRole("button", { name: "Edit path for missing.md", exact: true }).click();
    await expect(field).toHaveValue("missing.md");
    await field.fill("plan.md");
    const relativeLookup = page.waitForRequest((req) => {
      const url = new URL(req.url());
      return url.pathname === `/api/sessions/${sessionId}/files/meta` && url.searchParams.get("path") === "plan.md";
    });
    await dialog.getByRole("button", { name: "Open file", exact: true }).click();
    const binding = new URL((await relativeLookup).url()).searchParams;
    expect(binding.get("source")).toBe(repository);
    expect(binding.get("source_directory")).toBe("docs");
    expect(binding.get("source_directory_id")).toBeTruthy();
    await expect(dialog.getByRole("heading", { name: "Relative repository plan" })).toBeVisible();
    await dialog.getByRole("button", { name: "Close", exact: true }).click();

    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await showFile(page, JSON.stringify(crFile));
    await expect(dialog.getByRole("heading", { name: "Carriage return name" })).toBeVisible();
    await dialog.getByRole("button", { name: "Copy path" }).click();
    await expect(dialog.getByRole("status")).toHaveText("Path copied.");
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(crFile);
    await dialog.getByRole("button", { name: "Edit path" }).click();
    await field.fill("");
    await field.press("Control+V");
    const displayedCrFile = crFile.replace("\r", "␛r");
    await expect(field).toHaveValue(displayedCrFile);
    const reloading = dialog.getByText("Reloading file…", { exact: true });
    let releaseReload!: () => void;
    const reloadMayFinish = new Promise<void>((resolve) => { releaseReload = resolve; });
    await page.route((url) => url.pathname === `/api/sessions/${sessionId}/files`, async (route) => {
      await reloadMayFinish;
      await route.continue();
    }, { times: 1 });
    try {
      await dialog.getByRole("button", { name: "Open file", exact: true }).click();
      await expect(reloading).toBeVisible();
      await expect(dialog.getByRole("heading", { name: "Carriage return name" })).toBeVisible();
      await expect(dialog.getByRole("button", { name: "Edit path" })).toHaveCount(0);
    } finally {
      releaseReload();
    }
    await expect(reloading).toHaveCount(0);
    await expect(dialog.getByRole("heading", { name: "Carriage return name" })).toBeVisible();
    const edit = () => dialog.getByRole("button", { name: "Edit path" }).click();
    const editSelection = (start: number, end = start) => field.evaluate((input, [from, to]) => {
      (input as HTMLTextAreaElement).setSelectionRange(from, to);
    }, [start, end] as const);
    const expectEditedPath = async (target: string, heading: string) => {
      const lookup = page.waitForRequest((req) => (
        new URL(req.url()).pathname === `/api/sessions/${sessionId}/files/meta`
      ));
      await dialog.getByRole("button", { name: "Open file", exact: true }).click();
      expect(new URL((await lookup).url()).searchParams.get("path")).toBe(target);
      // A same-path edit can retain a readable snapshot with the same heading.
      await expect(reloading).toHaveCount(0);
      await expect(dialog.getByRole("heading", { name: heading })).toBeVisible();
    };
    const carriageReturn = displayedCrFile.indexOf("␛r");
    await edit();
    await field.press("Control+a");
    await field.press("Control+c");
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(crFile);
    await field.press("Control+v");
    await expectEditedPath(crFile, "Carriage return name");

    await edit();
    await field.press("Control+a");
    await field.press("Control+x");
    await expect(field).toHaveValue("");
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(crFile);
    await field.press("Control+z");
    await expectEditedPath(crFile, "Carriage return name");
    await dialog.getByRole("button", { name: "Close", exact: true }).click();

    await showFile(page, JSON.stringify(repositoryFile));
    await expect(dialog.getByRole("heading", { name: "Repository plan" })).toBeVisible();
    await edit();
    await field.press("Control+a");
    await field.press("Control+v");
    await expect(field).toHaveValue(displayedCrFile);
    await field.press("Control+z");
    await expectEditedPath(repositoryFile, "Repository plan");
    await edit();
    await editSelection(repositoryFile.length - ".md".length);
    await field.press("x");
    await field.press("Control+v");
    await field.press("Control+z");
    await expectEditedPath(typedRepositoryFile, "Typed repository plan");
    await dialog.getByRole("button", { name: "Close", exact: true }).click();

    await showFile(page, JSON.stringify(crFile));
    await expect(dialog.getByRole("heading", { name: "Carriage return name" })).toBeVisible();
    await edit();
    await page.evaluate(value => navigator.clipboard.writeText(value), backslashFile);
    await field.press("Control+a");
    await field.press("Control+v");
    await expect(field).toHaveValue(backslashFile);
    await field.press("Control+z");
    await expectEditedPath(crFile, "Carriage return name");

    await edit();
    await editSelection(carriageReturn, carriageReturn + 2);
    await field.press("Backspace");
    await field.press("Control+z");
    await expectEditedPath(crFile, "Carriage return name");

    await edit();
    await editSelection(carriageReturn + 2);
    await field.press("Enter");
    await field.press("x");
    await expectEditedPath(continuedCrFile, "Continued edit");
    await dialog.getByRole("button", { name: "Close", exact: true }).click();

    await showFile(page, JSON.stringify(crFile));
    await expect(dialog.getByRole("heading", { name: "Carriage return name" })).toBeVisible();
    await edit();
    await editSelection(carriageReturn);
    await field.press("Enter");
    await expectEditedPath(editedCrFile, "Line feed before carriage return");
    await dialog.getByRole("button", { name: "Close", exact: true }).click();

    await expect(page.locator(`[data-chat-session="${sessionId}"] .msg`)).toHaveCount(0);
    const history = await request.get(`${API}/sessions/${sessionId}/messages?before_seq=10000`, { headers });
    expect(history.ok()).toBeTruthy();
    expect((await history.json()).messages).toEqual([]);
    expect(page.url()).toBe(originalUrl);

    const archived = await request.post(`${API}/sessions/${sessionId}/archive?replace=false`, { headers });
    expect(archived.status()).toBe(204);
    const openArchived = page.locator(".chat-archived-banner").getByRole("button", { name: "Open file" });
    await openArchived.click();
    const archivedPath = dialog.getByRole("textbox", { name: "File path" });
    await expect(archivedPath).toBeFocused();
    await archivedPath.fill(repositoryFile);
    await dialog.getByRole("button", { name: "Open file", exact: true }).click();
    await expect(dialog.getByRole("heading", { name: "Repository plan" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(openArchived).toBeFocused();
  } finally {
    rmSync(repository, { recursive: true, force: true });
  }
});

previewTest("file path clipboard selections and guarded drops preserve literal escape markers", async ({ page, request, previewDirectory: directory }) => {
  const name = "File path text transfers";
  const markerPath = path.join(directory, "file␛r.md");
  writeFileSync(markerPath, "# Literal marker target");
  writeFileSync(path.join(directory, "file\r.md"), "# Carriage return target");
  writeFileSync(path.join(directory, "file␛\r.md"), "# Incorrect partial edit");
  writeFileSync(path.join(directory, "plain.md"), "# Plain text drop");
  const sessionId = await createSession(request, directory, name);
  await signIn(page);
  await openSession(page, name);

  const dialog = page.getByRole("dialog");
  const field = dialog.getByRole("textbox", { name: "File path" });
  const edit = () => dialog.getByRole("button", { name: "Edit path" }).click();
  const select = (start: number, end: number) => field.evaluate((el, [from, to]) => {
    (el as HTMLTextAreaElement).setSelectionRange(from, to);
  }, [start, end] as const);
  const submitPath = async (target = markerPath, heading = "Literal marker target") => {
    const lookup = page.waitForRequest(req => new URL(req.url()).pathname === `/api/sessions/${sessionId}/files/meta`);
    await dialog.getByRole("button", { name: "Open file", exact: true }).click();
    expect(new URL((await lookup).url()).searchParams.get("path")).toBe(target);
    await expect(dialog.getByRole("heading", { name: heading })).toBeVisible();
  };

  await showFile(page, JSON.stringify(markerPath));
  await expect(dialog.getByRole("heading", { name: "Literal marker target" })).toBeVisible();
  const marker = markerPath.lastIndexOf("␛");
  await edit();
  await select(marker, marker + 1);
  await field.press("Control+c");
  await field.press("Control+v");
  await submitPath();

  await edit();
  await select(marker + 1, marker + 3);
  await field.press("Control+x");
  await expect(field).toHaveValue(path.join(directory, "file.md"));
  await field.press("Control+v");
  await submitPath();

  const dropText = async (text: string) => {
    await field.evaluate((el, value) => {
      const source = document.createElement("span");
      source.draggable = true;
      source.textContent = "Drag path text";
      source.addEventListener("dragstart", event => event.dataTransfer!.setData("text/plain", value));
      el.closest("form")!.append(source);
    }, text);
    const source = dialog.getByText("Drag path text");
    const width = await field.evaluate(el => el.clientWidth);
    await source.dragTo(field, { targetPosition: { x: width - 6, y: 12 } });
    await source.evaluate(el => el.remove());
  };
  const notice = dialog.getByText("Use copy or cut, then paste to place this text.");
  await edit();
  await field.fill("./");
  await select(0, 0);
  await dropText("plain.md");
  await expect(field).toHaveValue("./plain.md");
  await expect(notice).toHaveCount(0);
  await submitPath("./plain.md", "Plain text drop");

  await edit();
  await field.fill("./plain.md");
  await select(7, 7);
  await field.press("x");
  await field.press("Control+z");
  for (const literal of ["file␛r.md", "file\r.md"]) {
    await dropText(literal);
    await expect(notice).toBeVisible();
    await expect(field).toHaveValue("./plain.md");
    await field.press("Control+Shift+z");
    await expect(field).toHaveValue("./plainx.md");
    await field.press("Control+z");
  }
  await submitPath("./plain.md", "Plain text drop");

  const displayed = "./file␛␛r.md";
  const typed = "./file␛␛rx.md";
  const prepareHistory = async () => {
    await edit();
    await field.fill(displayed);
    await select(displayed.length - 3, displayed.length - 3);
    await field.press("x");
    await expect(field).toHaveValue(typed);
    await field.evaluate(el => {
      el.addEventListener("beforeinput", event => {
        if (event instanceof InputEvent && ["deleteByDrag", "insertFromDrop"].includes(event.inputType)) {
          el.setAttribute("data-native-drop", event.inputType);
        }
      });
    });
  };
  const moveInsideField = async () => {
    await select(2, 6);
    const { from, to, y } = await field.evaluate(el => {
      const field = el as HTMLTextAreaElement;
      const bounds = field.getBoundingClientRect();
      const style = getComputedStyle(field);
      const context = document.createElement("canvas").getContext("2d")!;
      context.font = style.font;
      const origin = bounds.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft);
      const x = (index: number) => origin + context.measureText(field.value.slice(0, index)).width;
      return { from: x(4), to: x(7), y: bounds.top + 12 };
    });
    await page.mouse.move(from, y);
    await page.mouse.down();
    await page.mouse.move(to, y, { steps: 8 });
    await page.mouse.move(to, y);
    await page.mouse.up();
  };
  for (const rejectedDrop of [moveInsideField, () => dropText("plain.md")]) {
    await prepareHistory();
    await field.press("Control+z");
    await rejectedDrop();
    await expect(notice).toBeVisible();
    await expect(field).not.toHaveAttribute("data-native-drop");
    await expect(field).toHaveValue(displayed);
    await field.press("Control+Shift+z");
    await expect(field).toHaveValue(typed);
    await field.press("Control+z");
    await submitPath("./file␛r.md");

    await prepareHistory();
    await select(displayed.length - 3, displayed.length - 2);
    await field.press("Backspace");
    await expect(field).toHaveValue(displayed);
    await rejectedDrop();
    await expect(notice).toBeVisible();
    await expect(field).not.toHaveAttribute("data-native-drop");
    await field.press("Control+z");
    await expect(field).toHaveValue(typed);
    await field.press("Control+z");
    await expect(field).toHaveValue(displayed);
    await field.press("Control+Shift+z");
    await expect(field).toHaveValue(typed);
    await field.press("Control+Shift+z");
    await expect(field).toHaveValue(displayed);
    await submitPath("./file␛r.md");
  }
});

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
    "# Appendix", spacing, "## 目标小节", "[Read source line](readme.md:1)", spacing,
  ].join("\n\n"));
  writeFileSync(path.join(other, "docs", appendix), "# Other appendix");
  writeFileSync(path.join(source, "docs", "draft:old.md"), "# Archived draft\nArchived draft text");
  writeFileSync(path.join(other, "docs", "draft:old.md"), "# Foreground draft\nForeground draft text");
  const content = [
    "# Source document",
    "[Jump to second heading](#检查结果-1)",
    "[Open appendix](%20中文%2520%23图.md?ignored=1#目标小节)",
    "[Read draft source line](draft%3Aold.md:2)",
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
  await article.getByRole("button", { name: /^Open file: Open appendix/ }).click();
  await expect(article.getByRole("heading", { name: "Other appendix" })).toBeVisible();
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await openSession(page, "Markdown source");

  const sourceFiles = `/api/sessions/${sourceId}/files`;
  await page.route((url) => url.pathname === `/api/sessions/${sourceId}/showme/resolve`,
    async (route) => {
      const meta = await route.fetch();
      expect(meta.status()).toBe(200);
      expect((await meta.json()).metadata.path).toBe("docs/readme.md");
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
  await article.getByRole("button", { name: /^Open file: Open appendix/ }).click();
  expect((await appendixRead).status()).toBe(200);
  await expect(article.getByRole("heading", { name: "目标小节" })).toBeInViewport();
  await expect(article.getByRole("heading", { name: "Appendix" })).not.toBeInViewport();
  await article.getByRole("button", { name: /^Open file: Read source line/ }).click();
  const sourceLine = dialog.locator('[data-line="1"][data-located="true"]');
  await expect(sourceLine).toContainText("# Source document");
  await expect(sourceLine).toBeInViewport();
  await dialog.getByRole("button", { name: "Reading", exact: true }).click();
  await article.getByRole("button", { name: /^Open file: Read draft source line/ }).click();
  const draftLine = dialog.locator('[data-line="2"][data-located="true"]');
  await expect(draftLine).toContainText("Archived draft text");
  await expect(draftLine).toBeInViewport();
});

previewTest("Markdown local file buttons use the source boundary and preserve web navigation", async ({
  page, context, request, account, previewDirectory: directory,
}) => {
  if (!account) throw new Error("The overlapping sources need an offline account");
  const root = path.join(directory, "repo");
  const cwd = path.join(root, "nested");
  const neighbor = path.join(directory, "repo-neighbor");
  const outside = mkdtempSync(path.join(os.tmpdir(), "showme-denied-"));
  const encodePath = (absolute: string) => absolute.split("/").map(encodeURIComponent).join("/");
  try {
    mkdirSync(cwd, { recursive: true });
    mkdirSync(path.join(cwd, "deep"));
    mkdirSync(neighbor);
    symlinkSync("nested/deep", path.join(root, "jump"));
    const guide = path.join(root, "guide.md");
    const target = path.join(cwd, "中文 %20#记录.md");
    const serverPathWithDot = `${directory}/./repo//nested/${path.basename(target)}`;
    const sibling = path.join(neighbor, "sibling.md");
    const denied = path.join(outside, "denied.md");
    writeFileSync(target, [
      "# Original", "Citation [^中文]", spacing, "## 目标章节", spacing, "[^中文]: Linked footnote text.",
    ].join("\n\n"));
    writeFileSync(sibling, "# Sibling document");
    writeFileSync(denied, "# Outside grant");
    writeFileSync(guide, [
      "# File references",
      `[Server path](${encodePath(serverPathWithDot)}#目标章节)`,
      `[Local URI](file:${encodePath(target)}#L1)`,
      `[Localhost URI](file://localhost${encodePath(target)}#L1)`,
      `[Linked footnote](nested/${encodeURIComponent(path.basename(target))}#user-content-fn-%E4%B8%AD%E6%96%87)`,
      `[Relative through symlink](jump/../${encodeURIComponent(path.basename(target))})`,
      `[Neighboring root](${encodePath(sibling)})`,
      `[Denied target](${encodePath(denied)})`,
      "[Reserved route](/health?check=1#status)",
      "[Reserved API route](/api/health?check=1#status)",
      "[Normalized reserved route](/docs/../api/sessions)",
      "[External website](https://example.test/docs?check=1#status)",
      "[Malformed URI](bad%FF.md#L4)",
      "[Web URI with numeric value](mailto:123)",
    ].join("\n\n"));
    const setExtraRoots = await editExtraRoots(request, account.user_id);
    await setExtraRoots([root]);
    const sessionId = await createSession(request, cwd, "Markdown file references");
    await signIn(page);
    await openSession(page, "Markdown file references");
    const originalUrl = page.url();
    const dialog = page.getByRole("dialog");
    const article = dialog.getByRole("article");
    const button = (label: string) => article.getByRole("button", { name: new RegExp(`^Open file: ${label}`) });
    const showGuide = async () => {
      await showFile(page, `${JSON.stringify(guide)} --source ${JSON.stringify(root)}`);
      await expect(article.getByRole("heading", { name: "File references" })).toBeVisible();
    };
    const close = () => dialog.getByRole("button", { name: "Close", exact: true }).click();
    const waitForMetadata = (target: string) => page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === `/api/sessions/${sessionId}/files/meta` && url.searchParams.get("path") === target;
    });

    await showGuide();
    for (const [label, href] of [
      ["Reserved route", "/health?check=1#status"],
      ["Reserved API route", "/api/health?check=1#status"],
      ["Normalized reserved route", "/docs/../api/sessions"],
      ["External website", "https://example.test/docs?check=1#status"],
      ["Malformed URI", "bad%FF.md#L4"],
      ["Web URI with numeric value", "mailto:123"],
    ]) {
      await expect(article.getByRole("link", { name: label, exact: true })).toHaveAttribute("href", href);
    }
    const requests: string[] = [];
    page.on("request", (request) => requests.push(new URL(request.url()).pathname));
    await expect(button("Localhost URI")).toBeVisible();
    const serverPath = button("Server path");
    await expect(serverPath).not.toHaveAttribute("href");
    await expect(serverPath).not.toHaveAttribute("aria-haspopup");
    await serverPath.click({ button: "middle" });
    await serverPath.click({ button: "right" });
    await expect(article.getByRole("heading", { name: "File references" })).toBeVisible();
    expect(context.pages()).toHaveLength(1);
    expect(page.url()).toBe(originalUrl);
    expect(requests.filter(url => url.startsWith(`/api/sessions/${sessionId}/files`))).toHaveLength(0);

    const serverMetadata = waitForMetadata(serverPathWithDot);
    await serverPath.press("Enter");
    const serverResponse = await serverMetadata;
    const serverQuery = new URL(serverResponse.url()).searchParams;
    expect(serverQuery.get("source")).toBe(root);
    expect(serverQuery.has("source_directory")).toBe(false);
    expect((await serverResponse.json()).source).toBe(root);
    await expect(article.getByRole("heading", { name: "目标章节" })).toBeInViewport();

    await close();
    await showGuide();
    const localMetadata = waitForMetadata(target);
    await button("Local URI").click();
    expect((await localMetadata).ok()).toBe(true);
    await expect(dialog.locator('[data-line="1"][data-located="true"]')).toBeInViewport();
    await expect(dialog.getByText("# Original", { exact: true })).toBeVisible();

    await close();
    await showGuide();
    await button("Linked footnote").click();
    await expect(article.getByText("Linked footnote text.")).toBeInViewport();

    await close();
    await showGuide();
    const relativeMetadata = waitForMetadata(`jump/../${path.basename(target)}`);
    await button("Relative through symlink").click();
    const relativeResponse = await relativeMetadata;
    const relativeQuery = new URL(relativeResponse.url()).searchParams;
    expect(relativeQuery.get("source")).toBe(root);
    expect(relativeQuery.get("source_directory")).toBe(".");
    expect(relativeQuery.get("source_directory_id")).toBeTruthy();
    expect((await relativeResponse.json()).absolute_path).toBe(target);
    await expect(article.getByRole("heading", { name: "Original" })).toBeVisible();

    await close();
    await showGuide();
    const neighboringMetadata = waitForMetadata(sibling);
    await button("Neighboring root").click();
    const neighboringResponse = await neighboringMetadata;
    expect(neighboringResponse.ok()).toBe(true);
    const neighboringQuery = new URL(neighboringResponse.url()).searchParams;
    expect(neighboringQuery.has("source")).toBe(false);
    expect(neighboringQuery.has("source_directory")).toBe(false);
    await expect(article.getByRole("heading", { name: "Sibling document" })).toBeVisible();

    await close();
    await showGuide();
    const deniedMetadata = waitForMetadata(denied);
    await button("Denied target").click();
    expect((await deniedMetadata).status()).toBe(403);
    await expect(dialog.getByRole("alert")).toHaveText(`Couldn't open ${denied}. You don't have access to this file.`);
    await expect(article.getByRole("heading", { name: "File references" })).toBeVisible();

    await close();
    await showGuide();
    const siteNavigation = page.waitForResponse(response => {
      return new URL(response.url()).pathname === "/health" && response.request().isNavigationRequest();
    });
    await article.getByRole("link", { name: "Reserved route", exact: true }).click();
    expect((await siteNavigation).ok()).toBe(true);
    await expect(page).toHaveURL(/\/health\?check=1#status$/);
  } finally {
    rmSync(outside, { recursive: true, force: true });
  }
});

previewTest("same-document references repeatedly position the loaded Markdown", async ({
  page, request, previewDirectory: directory,
}) => {
  const content = [
    "# Read this document", "",
    "[Jump to heading](#section)",
    "[Open range](#L2002-L2003)",
    "[Open range by relative path](.//locations.md#L2002-L2003)",
    `[Open range by absolute path](${directory.split("/").map(encodeURIComponent).join("/")}/./locations.md#L2002-L2003)`,
    "[Missing heading](#unknown)",
    "[Invalid line](#L0)",
    "[Outside lines](#L9000)",
    "", spacing, "", "## Section",
  ].join("\n").split("\n");
  const sourceMarker = Array.from({ length: 80 }, (_, index) => `Final line ${index + 1}.`);
  writeFileSync(path.join(directory, "locations.md"), [
    ...content, ...Array<string>(2001 - content.length).fill(""), ...sourceMarker,
  ].join("\n"));
  const sessionId = await createSession(request, directory, "Same-document references");
  const reads: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith(`/api/sessions/${sessionId}/files`)) reads.push(url.pathname);
  });
  await signIn(page);
  await openSession(page, "Same-document references");
  await showFile(page, "locations.md");
  const dialog = page.getByRole("dialog");
  const article = dialog.getByRole("article");
  const start = article.getByRole("heading", { name: "Read this document" });
  const heading = article.getByRole("heading", { name: "Section" });
  await expect(start).toBeVisible();
  const before = reads.length;
  unlinkSync(path.join(directory, "locations.md"));
  for (let repeat = 0; repeat < 2; repeat++) {
    await start.scrollIntoViewIfNeeded();
    await expect(heading).not.toBeInViewport();
    await article.getByRole("link", { name: "Jump to heading" }).press("Enter");
    await expect(heading).toBeInViewport();
  }
  await article.getByRole("link", { name: "Missing heading" }).click();
  await expect(dialog.getByRole("note")).toHaveText("The requested heading wasn't found.");
  await article.getByRole("link", { name: "Invalid line" }).click();
  await expect(dialog.getByRole("note")).toHaveText("The requested file position is invalid.");
  await article.getByRole("link", { name: "Outside lines" }).click();
  await expect(dialog.getByRole("note")).toHaveText("The requested lines are outside this file.");
  const read = () => dialog.getByRole("button", { name: "Reading", exact: true }).click();
  const first = dialog.locator('[data-line="2002"][data-located="true"]');
  await expect(dialog.getByRole("button", { name: "Reading", exact: true })).toHaveAttribute("aria-pressed", "true");
  await article.getByRole("link", { name: "Open range" }).click();
  await expect(first).toBeInViewport();
  await expect(first).toHaveText("Final line 1.");
  await expect(dialog.locator('[data-line="2003"]')).toHaveAttribute("data-located", "true");
  await dialog.getByRole("button", { name: "Previous lines" }).click();
  await expect(first).toHaveCount(0);
  for (const label of [/^Open file: Open range by relative path/, /^Open file: Open range by absolute path/]) {
    await read();
    await article.getByRole("button", { name: label }).press("Space");
    await expect(first).toBeInViewport();
  }
  await expect(dialog.getByRole("note")).toHaveCount(0);
  expect(reads).toHaveLength(before);
});

previewTest("heading fragments and generated footnotes reach their own targets", async ({ page, request, previewDirectory: directory }) => {
  writeFileSync(path.join(directory, "alias.md"), [
    "[Jump to note-named heading](#user-content-fn-a)",
    "[Jump to prefixed heading](#file-viewer-heading-start)",
    "Reference to the footnote [^a] and the Chinese footnote [^中文]",
    spacing, "## User Content Fn A", spacing,
    "## Start", spacing, "## File Viewer Heading Start", spacing,
    "## 重复标题", spacing, "## 重复标题", spacing,
    "[^a]: The actual footnote.", "[^中文]: 中文脚注内容。",
  ].join("\n\n"));
  await createSession(request, directory, "Anchor collisions");
  await signIn(page);
  await openSession(page, "Anchor collisions");
  await showFile(page, "alias.md#user-content-fn-a");

  const dialog = page.getByRole("dialog");
  const article = dialog.getByRole("article");
  await expect(article.getByRole("heading", { name: "User Content Fn A" })).toBeInViewport();
  await article.getByRole("link", { name: "Jump to note-named heading" }).click();
  await expect(article.getByRole("heading", { name: "User Content Fn A" })).toBeInViewport();
  await article.getByRole("link", { name: "Jump to prefixed heading" }).click();
  await expect(article.getByRole("heading", { name: "File Viewer Heading Start" })).toBeInViewport();

  const reference = article.locator("a[data-footnote-ref]").first();
  await reference.click();
  await expect(article.getByText("The actual footnote.")).toBeInViewport();
  await article.locator("a[data-footnote-backref]").first().click();
  await expect(reference).toBeInViewport();
  const chinese = article.locator("a[data-footnote-ref]").last();
  await chinese.click();
  await expect(article.getByText("中文脚注内容。")).toBeInViewport();
  await article.locator("a[data-footnote-backref]").last().click();
  await expect(chinese).toBeInViewport();

  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await showFile(page, "alias.md#重复标题-1");
  const duplicates = article.getByRole("heading", { name: "重复标题" });
  await expect(duplicates.last()).toBeInViewport();
  await expect(duplicates.first()).not.toBeInViewport();
  await article.getByRole("link", { name: "Jump to note-named heading" }).scrollIntoViewIfNeeded();
  await dialog.getByRole("button", { name: "Source", exact: true }).click();
  await expect(dialog.locator("pre.file-source")).toBeVisible();
  await dialog.getByRole("button", { name: "Reading", exact: true }).click();
  await expect(duplicates.last()).not.toBeInViewport();
});

previewTest("encoded Markdown footnote links copy commands for the exact note", async ({
  page, context, request, previewDirectory: directory,
}) => {
  const filename = "notes.md";
  const padding = spacing.split("\n").map((line) => line ? `    ${line}` : "").join("\n");
  writeFileSync(path.join(directory, filename), [
    "[Go to Chinese note](./notes.md#user-content-fn-%E4%B8%AD)",
    "[Go to Chinese note lowercase](#user-content-fn-%e4%b8%ad)",
    "[Go to Chinese note mixed](./notes.md#user-content-fn-%E4%b8%AD)",
    "[Go to escaped note](./notes.md#user-content-fn-%25e4%25b8%25ad)",
    "References [^%e4%b8%ad] and [^中]",
    `[^%e4%b8%ad]: The escaped note.\n\n${padding}`,
    `[^中]: The Chinese note.\n\n${padding}`,
  ].join("\n\n"));
  await createSession(request, directory, "Encoded footnotes");
  await openAsCurrentAccount(page);
  await page.locator(".agent-item", { hasText: "Octo" }).click();
  await openSession(page, "Encoded footnotes");
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const dialog = page.getByRole("dialog");
  for (const [role, label, note, otherNote, fragment] of [
    ["button", /Go to Chinese note at/, "The Chinese note.", "The escaped note.", "user-content-fn-%E4%B8%AD"],
    ["link", "Go to Chinese note lowercase", "The Chinese note.", "The escaped note.", "user-content-fn-%E4%B8%AD"],
    ["button", /Go to Chinese note mixed at/, "The Chinese note.", "The escaped note.", "user-content-fn-%E4%B8%AD"],
    ["button", /Go to escaped note at/, "The escaped note.", "The Chinese note.", "user-content-fn-%e4%b8%ad"],
  ] as const) {
    await showFile(page, filename);
    await dialog.getByRole(role, { name: label }).click();
    const checkPosition = async () => {
      await expect(dialog.getByText(note)).toBeInViewport();
      await expect(dialog.getByText(otherNote)).not.toBeInViewport();
    };
    await checkPosition();
    await dialog.getByRole("button", { name: "Copy /showme command" }).click();
    await expect(dialog.getByRole("status")).toHaveText("Command copied.");
    const command = await page.evaluate(() => navigator.clipboard.readText());
    expect(command).toBe(`/showme ${JSON.stringify(path.join(directory, filename))} --source ${JSON.stringify(directory)}#${fragment}`);
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await showFile(page, command.slice("/showme ".length));
    await checkPosition();
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
  }
});

previewTest("an image tab waits for decoding and respects a later reading action", async ({
  page, request, previewDirectory: directory,
}) => {
  writeFileSync(path.join(directory, "first.md"), "# Original guide\n\n[View diagram](diagram.png)\n\n[Read notes](notes.md)");
  writeFileSync(path.join(directory, "diagram.png"), png);
  writeFileSync(path.join(directory, "notes.md"), "# Current notes");
  const name = "Image readiness";
  await createSession(request, directory, name);
  await page.addInitScript(() => {
    const released = new Promise<void>((resolve) =>
      document.addEventListener("release-test-image-decode", () => resolve(), { once: true }));
    const decode = HTMLImageElement.prototype.decode;
    HTMLImageElement.prototype.decode = function () {
      const decoded = decode.call(this);
      if (!this.src.startsWith("blob:")) return decoded;
      return decoded.then(() => {
        this.dataset.awaitingDecode = "";
        return released;
      });
    };
  });
  await signIn(page);
  await openSession(page, name);
  await showFile(page, "first.md");
  const reader = page.getByRole("dialog");
  await reader.getByRole("button", { name: "Open file: View diagram" }).press("Enter");
  const image = reader.locator("img[data-awaiting-decode]");
  await expect(image).toBeAttached();
  await expect(image).toBeHidden();
  await expect(reader.getByRole("heading", { name: "Original guide" })).toBeVisible();
  await reader.getByRole("button", { name: "Open file: Read notes" }).press("Enter");
  await expect(reader.getByRole("heading", { name: "Current notes" })).toBeVisible();
  await page.evaluate(() => document.dispatchEvent(new Event("release-test-image-decode")));
  const diagram = reader.getByRole("tab", { name: "diagram.png" });
  await expect(diagram).toHaveAttribute("aria-selected", "false");
  await expect(reader.getByRole("tab", { name: "notes.md" })).toHaveAttribute("aria-selected", "true");
  await diagram.click();
  await expect(reader.getByRole("img", { name: "diagram.png" })).toBeVisible();
  await expect(image).toHaveJSProperty("naturalWidth", 1);
});

for (const viewport of [{ width: 1280, height: 800 }, { width: 360, height: 640 }]) {
  previewTest(`text file tabs retain reading state and keyboard focus at ${viewport.width}px`, async ({
    page, request, previewDirectory: directory,
  }) => {
    writeFileSync(path.join(directory, "first.md"), [
      "# Shared heading", "[Read the second file](second.md)", "First citation [^shared]", spacing,
      Array.from({ length: 2100 }, (_, line) => `Source line ${line + 1}`).join("\n"),
      "[^shared]: First document note.",
    ].join("\n\n"));
    writeFileSync(path.join(directory, "second.md"), [
      "# Shared heading", "Second citation [^shared]", spacing, "[^shared]: Second document note.",
    ].join("\n\n"));
    const name = `Reader keyboard ${viewport.width}`;
    const session = await request.post(`${API}/sessions/import`, { headers, data: {
      name, working_dir: directory,
      messages: [{ role: "assistant", type: "text", content: "[Open the first file](first.md)" }],
    } });
    expect(session.status()).toBe(201);
    const { id } = await session.json();
    await openAsCurrentAccount(page);
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await openSession(page, name);
    await page.setViewportSize(viewport);
    const trigger = page.getByRole("button", { name: "Open file: Open the first file" });
    const reads: (string | null)[] = [];
    page.on("request", (resource) => {
      const url = new URL(resource.url());
      if (url.pathname === `/api/sessions/${id}/files`) reads.push(url.searchParams.get("path"));
    });
    await trigger.press("Enter");

    const reader = page.getByRole("dialog");
    const a = reader.getByRole("tab", { name: "first.md" });
    const b = reader.getByRole("tab", { name: "second.md" });
    const visible = reader.getByRole("tabpanel");
    const link = visible.getByRole("button", { name: "Open file: Read the second file" });
    await link.press("Enter");
    await expect(b).toBeFocused();
    await expect(b).toHaveAttribute("aria-selected", "true");
    await expect(link).toHaveCount(0);
    const inactive = reader.locator('[role="tabpanel"][inert]');
    await expect(inactive).toHaveAttribute("aria-hidden", "true");
    await inactive.locator(".markdown-file-link").evaluate((node) => (node as HTMLElement).focus());
    await expect(b).toBeFocused();

    const noteLinks = reader.locator("a[data-footnote-ref]");
    const describedBy = await noteLinks.evaluateAll((refs) => refs.map((ref) => {
      const id = ref.getAttribute("aria-describedby");
      const target = id && ref.ownerDocument.getElementById(id);
      const destination = ref.ownerDocument.getElementById(ref.getAttribute("href")!.slice(1));
      return { id, inside: !!target && !!destination && ref.closest("article") === target.closest("article") &&
        ref.closest("article") === destination.closest("article") };
    }));
    expect(describedBy).toHaveLength(2);
    expect(describedBy.every((ref) => ref.inside)).toBe(true);
    expect(new Set(describedBy.map((ref) => ref.id)).size).toBe(2);
    const headingIds = await reader.locator("article h1").evaluateAll((headings) => headings.map((heading) => heading.id));
    expect(new Set(headingIds).size).toBe(2);
    await visible.locator("a[data-footnote-ref]").click();
    await expect(visible.getByText("Second document note.")).toBeInViewport();
    const body = visible.locator(".file-viewer-body");
    const secondScroll = await body.evaluate((node) => node.scrollTop);
    expect(secondScroll).toBeGreaterThan(0);

    await b.press("ArrowLeft");
    await expect(a).toBeFocused();
    await visible.locator("a[data-footnote-ref]").click();
    await expect(visible.getByText("First document note.")).toBeInViewport();
    if (viewport.width >= 800) {
      await visible.getByRole("button", { name: "Source", exact: true }).click();
      const lines = visible.getByRole("navigation", { name: "Source line sections" });
      await lines.getByRole("button", { name: "Next lines" }).click();
      await expect(lines).toContainText("Lines 2001–");
      await body.evaluate((node) => { node.scrollTop = 450; });
    }
    await a.press("ArrowRight");
    await expect(b).toBeFocused();
    await expect.poll(() => body.evaluate((node) => node.scrollTop)).toBe(secondScroll);
    await b.press("Home");
    await expect(a).toBeFocused();
    if (viewport.width >= 800) {
      await expect(visible.getByRole("button", { name: "Source", exact: true })).toHaveAttribute("aria-pressed", "true");
      await expect(visible.getByRole("navigation", { name: "Source line sections" })).toContainText("Lines 2001–");
      await expect.poll(() => body.evaluate((node) => node.scrollTop)).toBe(450);
    }
    expect(reads).toEqual(["first.md", "second.md"]);

    await a.press("Delete");
    await expect(b).toBeFocused();
    await b.press("Delete");
    const chooser = reader.getByRole("button", { name: "Choose a file" });
    await expect(chooser).toBeFocused();
    await chooser.press("Enter");
    await expect(reader.getByRole("heading", { name: "Choose a file" })).toBeFocused();
    await reader.getByRole("button", { name: "Cancel file picker" }).press("Enter");
    await expect(chooser).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(reader).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });
}

previewTest("a footnote backreference reveals its citation inside a wide table", async ({ page, request, previewDirectory: directory }) => {
  writeFileSync(path.join(directory, "table-notes.md"), [
    "Opening citation [^shared]", "",
    "| Details | Citation |", "| --- | --- |",
    `| \`${"wide".repeat(40)}\` | Later citation [^shared] |`, "",
    spacing, "", "[^shared]: The shared note.",
  ].join("\n"));
  await createSession(request, directory, "Table footnotes");
  await signIn(page);
  await openSession(page, "Table footnotes");
  await page.setViewportSize({ width: 320, height: 520 });
  await showFile(page, "table-notes.md");

  const article = page.getByRole("dialog").getByRole("article");
  const tableReference = article.getByRole("table").locator("a[data-footnote-ref]");
  await article.locator("a[data-footnote-ref]").first().click();
  await expect(article.getByText("The shared note.")).toBeInViewport();
  await article.locator("a[data-footnote-backref]").last().click();
  await expect(tableReference).toBeInViewport({ ratio: 0.95 });
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

cacheTest("a legacy cached image cannot satisfy a new viewer read after revocation", async ({ page, request, account, baseURL }) => {
  if (!account || !baseURL) throw new Error("The cache story requires the offline account and runner");
  const setExtraRoots = await editExtraRoots(request, account.user_id);

  // The loopback proxy supplies the cache policy of a pre-upgrade response.
  // It forwards the file request to the real API and cannot satisfy a cache miss.
  let legacyPath: string | undefined;
  let legacyNetworkReads = 0;
  const upstream = new URL(baseURL);
  const proxy = createServer((incoming, outgoing) => {
    const forwarded = forwardRequest(upstream, {
      path: incoming.url, method: incoming.method, headers: { ...incoming.headers, host: upstream.host },
    }, (response) => {
      const headers = { ...response.headers };
      if (incoming.url === legacyPath) {
        legacyNetworkReads++;
        if (response.statusCode === 200) headers["cache-control"] = "private, max-age=600";
      }
      outgoing.writeHead(response.statusCode ?? 502, headers);
      response.pipe(outgoing);
    });
    forwarded.on("error", (error) => outgoing.destroy(error));
    incoming.pipe(forwarded);
  });
  const directory = mkdtempSync(path.join(os.tmpdir(), "viewer-legacy-cache-"));
  try {
    await new Promise<void>((resolve, reject) => {
      proxy.once("error", reject);
      proxy.listen(0, "127.0.0.1", resolve);
    });
    const address = proxy.address();
    if (!address || typeof address === "string") throw new Error("Cache proxy has no loopback port");
    writeFileSync(path.join(directory, "logo.png"), png);
    await setExtraRoots([directory]);
    const sessionId = await createSession(request, directory, "Legacy file cache");
    // Keep WebSocket traffic on the runner's existing loopback connection.
    await page.addInitScript((host) => {
      window.WebSocket = class extends WebSocket {
        constructor(input: string | URL, protocols?: string | string[]) {
          const url = new URL(input);
          url.host = host;
          super(url, protocols);
        }
      };
    }, upstream.host);
    await signIn(page, `http://127.0.0.1:${address.port}`);
    await openSession(page, "Legacy file cache");

    const filePath = `/api/sessions/${sessionId}/files`;
    legacyPath = await page.evaluate((endpoint) => {
      const token = localStorage.getItem("octopus_token");
      if (!token) throw new Error("Browser has no file viewer token");
      return endpoint + "?path=logo.png&token=" + encodeURIComponent(token);
    }, filePath);
    const loadLegacyImage = (url: string) => page.evaluate((src) => new Promise<number>((resolve) => {
      const image = new Image();
      image.onload = () => resolve(image.naturalWidth);
      image.onerror = () => resolve(0);
      image.src = src;
    }), url);
    expect(await loadLegacyImage(legacyPath)).toBe(1);
    expect(legacyNetworkReads).toBe(1);

    await page.exposeFunction("revokeViewerFileGrant", () => setExtraRoots([]));
    await page.evaluate((endpoint) => {
      performance.clearResourceTimings();
      performance.setResourceTimingBufferSize(0);
      const originalFetch = window.fetch.bind(window);
      let revocation: Promise<void> | undefined;
      window.fetch = async (input, init) => {
        const response = await originalFetch(input, init);
        if (response.ok && new URL(String(input), location.href).pathname === endpoint.replace(/\/files$/, "/showme/resolve")) {
          revocation ??= (window as unknown as { revokeViewerFileGrant(): Promise<void> }).revokeViewerFileGrant();
          await revocation;
        }
        return response;
      };
    }, filePath);
    const freshRead = page.waitForResponse((response) => new URL(response.url()).pathname === filePath);
    await page.locator(".chat-input-bar textarea").fill("/showme logo.png");
    await page.locator("button.btn-send").click();
    const response = await freshRead;
    expect(response.status()).toBe(403);
    expect(new URL(response.url()).searchParams.get("viewer")).toBe("1");
    const viewer = page.getByRole("dialog");
    await expect(viewer.getByRole("alert")).toHaveText("You don't have access to this file.");
    await expect(viewer.getByRole("button", { name: "Edit path" })).toBeVisible();
    expect(await loadLegacyImage(legacyPath)).toBe(1);
    expect(legacyNetworkReads).toBe(1);
  } finally {
    proxy.closeAllConnections();
    await new Promise<void>((resolve) => proxy.close(() => resolve()));
    rmSync(directory, { recursive: true, force: true });
  }
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
  const conflict = await attemptedRead;
  expect(conflict.status()).toBe(409);
  expect((await conflict.json()).code).toBe("target_changed");
  await expect(dialog.getByText(/target file changed/i)).toBeVisible();
  await dialog.getByRole("button", { name: "Retry", exact: true }).click();
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
  symlinkSync("original/alias.md", path.join(directory, "alias.md"));
  await createSession(request, directory, "Relative resources");
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
  await article.getByRole("button", { name: /^Open file: Open appendix/ }).click();
  await expect(dialog.getByText(/document directory changed/i)).toBeVisible();
  expect(await page.evaluate(async (url) => (await fetch(url)).status, originalImage)).toBe(409);

  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await showAlias(page);
  await expect(article.getByRole("heading", { name: "Replacement document" })).toBeVisible();
  await expect.poll(() => diagram.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(24);
  await article.getByRole("button", { name: /^Open file: Open appendix/ }).click();
  await expect(article.getByRole("heading", { name: "Replacement appendix" })).toBeVisible();
});

previewTest("the reader picker opens the current document directory and preserves the reading position on cancel and failure", async ({
  page, request, previewDirectory: directory,
}) => {
  const docs = path.join(directory, "docs");
  mkdirSync(docs);
  writeFileSync(path.join(docs, "guide.md"), "# Guide\n\n" + "A paragraph in the guide.\n\n".repeat(150));
  writeFileSync(path.join(docs, "note.md"), "# A neighboring note");
  const sessionId = await createSession(request, directory, "Reader directory picker");
  await signIn(page);
  await openSession(page, "Reader directory picker");
  await showFile(page, "docs/guide.md");
  const reader = page.getByRole("dialog");
  const plus = reader.getByRole("button", { name: "Open file picker" });
  const cancelPicker = reader.getByRole("button", { name: "Cancel file picker" });
  const body = reader.getByRole("tabpanel").locator(".file-viewer-body");
  const browse = () => page.waitForResponse((response) =>
    new URL(response.url()).pathname === `/api/sessions/${sessionId}/files/browse`);
  await expect(reader.getByRole("heading", { name: "Guide", exact: true })).toBeVisible();
  await body.evaluate((node) => { node.scrollTop = 600; });
  await expect.poll(() => body.evaluate((node) => node.scrollTop)).toBe(600);

  const firstBrowse = browse();
  await plus.press("Enter");
  const listing = await firstBrowse;
  const params = new URL(listing.url()).searchParams;
  expect(params.get("source")).toBe(directory);
  expect(params.get("source_directory")).toBe("docs");
  expect(params.get("source_directory_id")).toBeTruthy();
  await expect(reader.getByRole("navigation", { name: "Directory" }).getByText("docs")).toHaveAttribute("aria-current", "location");
  await expect(reader.getByRole("button", { name: "note.md" })).toBeVisible();
  await cancelPicker.press("Enter");
  await expect(plus).toBeFocused();
  await expect.poll(() => body.evaluate((node) => node.scrollTop)).toBe(600);

  await plus.click();
  await reader.getByRole("button", { name: "note.md" }).click();
  await expect(reader.getByRole("heading", { name: "A neighboring note" })).toBeVisible();
  await reader.getByRole("tab", { name: "guide.md" }).click();
  await reader.getByRole("button", { name: "Edit path" }).click();
  await reader.getByRole("textbox", { name: "File path" }).fill("note.md");
  const edited = page.waitForRequest((resource) =>
    new URL(resource.url()).pathname === `/api/sessions/${sessionId}/files/meta`);
  await reader.getByRole("button", { name: "Open file", exact: true }).click();
  const editedPath = new URL((await edited).url()).searchParams;
  expect(editedPath.get("path")).toBe("note.md");
  expect(editedPath.get("source")).toBe(directory);
  expect(editedPath.get("source_directory")).toBe("docs");
  expect(editedPath.get("source_directory_id")).toBe(params.get("source_directory_id"));
  await expect(reader.getByRole("heading", { name: "A neighboring note" })).toBeVisible();
  await reader.getByRole("tab", { name: "guide.md" }).click();
  await plus.click();
  const note = reader.getByRole("button", { name: "note.md" });
  await expect(note).toBeVisible();
  renameSync(docs, `${docs}-retired`);
  mkdirSync(docs);
  writeFileSync(path.join(docs, "note.md"), "# A replacement directory");

  await note.click();
  await expect(reader.getByRole("alert")).toContainText(/document directory changed/i);
  await expect(reader.getByRole("tab", { name: "guide.md" })).toHaveAttribute("aria-selected", "true");
  await expect(reader.getByRole("alert")).toContainText("Reopen the source document.");
  await expect(reader.getByRole("button", { name: "Retry opening note.md" })).toHaveCount(0);
  await expect(reader.getByRole("button", { name: "Edit path for note.md" })).toHaveCount(0);
  await expect.poll(() => body.evaluate((node) => node.scrollTop)).toBe(600);

  const staleBrowse = browse();
  await plus.click();
  expect((await staleBrowse).status()).toBe(409);
  await expect(reader.getByRole("alert")).toContainText(/document directory changed/i);
  await cancelPicker.press("Enter");
  await expect(plus).toBeFocused();
  await expect.poll(() => body.evaluate((node) => node.scrollTop)).toBe(600);
  await plus.click();
  await reader.getByRole("searchbox", { name: "Search filenames" }).fill("note.md");
  const found = reader.getByRole("button", { name: /^note\.md docs$/ });
  await expect(found).toBeVisible();
  const searched = page.waitForResponse((response) =>
    new URL(response.url()).pathname === `/api/sessions/${sessionId}/files/meta`);
  await found.click();
  const searchResponse = await searched;
  const searchSelection = new URL(searchResponse.url()).searchParams;
  expect(searchSelection.get("path")).toBe("note.md");
  expect(searchSelection.get("source_directory_id")).toBe(params.get("source_directory_id"));
  expect(searchResponse.status()).toBe(409);
  await expect(reader.getByRole("alert")).toHaveText(/document directory changed/i);
  await expect.poll(() => body.evaluate((node) => node.scrollTop)).toBe(600);
  await reader.getByRole("tab", { name: "note.md" }).click();
  await expect(reader.getByRole("heading", { name: "A neighboring note" })).toBeVisible();
});

previewTest("picker selection, reload and download recheck revoked access while the open snapshot remains readable", async ({
  page, request, account, previewDirectory: directory,
}) => {
  if (!account) throw new Error("Expected an isolated account");
  const overlapping = mkdtempSync(path.join(os.tmpdir(), "octopus-reader-grant-"));
  const grant = path.join(overlapping, "narrow");
  mkdirSync(grant);
  const target = path.join(grant, "report.md");
  try {
    const setExtraRoots = await editExtraRoots(request, account.user_id);
    await setExtraRoots([overlapping, grant]);
    writeFileSync(target, "# Previously authorized report");
    await createSession(request, directory, "Reader grant changes");
    await openAsCurrentAccount(page);
    await page.locator(".agent-item", { hasText: "Octo" }).click();
    await openSession(page, "Reader grant changes");
    await showFile(page, JSON.stringify(target));
    const reader = page.getByRole("dialog");
    const snapshot = reader.getByRole("heading", { name: "Previously authorized report" });
    await expect(snapshot).toBeVisible();
    const download = await reader.getByRole("link", { name: "Download file" }).getAttribute("href");
    expect(download).toBeTruthy();
    await reader.getByRole("button", { name: "Open file picker" }).click();
    const report = reader.getByRole("button", { name: "report.md" });
    await expect(report).toBeVisible();
    await setExtraRoots([overlapping]);
    await report.click();
    await expect(reader.getByRole("alert")).toContainText(/don't have access/i);
    await expect(snapshot).toBeVisible();
    expect((await request.get(new URL(download!, page.url()).href)).status()).toBe(403);

    await reader.getByRole("button", { name: "Open file picker" }).click();
    await expect(reader.getByRole("alert")).toContainText(/source is no longer available/i);
    await expect(reader.getByRole("combobox", { name: "Source" })).toHaveValue(grant);
    await reader.getByRole("button", { name: "Cancel file picker" }).click();
    await reader.getByRole("button", { name: "Reload file", exact: true }).click();
    await expect(reader.locator(".file-viewer-body").getByRole("alert")).toContainText(/don't have access/i);
    await expect(snapshot).toHaveCount(0);
    await expect(reader.getByRole("link", { name: "Download file" })).toHaveCount(0);
    await expect(reader.getByRole("tab", { name: "report.md" })).toHaveAttribute("aria-selected", "true");

    await setExtraRoots([overlapping, grant]);
    writeFileSync(target, "# Report after access is restored");
    await expect(reader.getByRole("button", { name: "Retry", exact: true })).toHaveCount(0);
    await reader.getByRole("button", { name: "Edit path", exact: true }).click();
    await expect(reader.getByRole("textbox", { name: "File path" })).toHaveValue(target);
    await reader.getByRole("button", { name: "Open file", exact: true }).click();
    await expect(reader.getByRole("heading", { name: "Report after access is restored" })).toBeVisible();
    await expect(reader.getByRole("tab")).toHaveCount(1);
  } finally {
    rmSync(overlapping, { recursive: true, force: true });
  }
});

previewTest("bad links can be dismissed or expire while the original document stays at its reading position", async ({
  page, request, previewDirectory: directory,
}) => {
  writeFileSync(path.join(directory, "a.md"), [
    "# Original document", "[Broken link](missing.md)", "[Good link](b.md)", "[Bad position in B](b.md#L9999)",
    ...Array.from({ length: 80 }, (_, index) => `Paragraph ${index}`),
  ].join("\n\n"));
  writeFileSync(path.join(directory, "b.md"), [
    "# Next document", ...Array.from({ length: 80 }, (_, index) => `Paragraph ${index} in B`),
  ].join("\n\n"));
  await createSession(request, directory, "Reading through broken links");
  await signIn(page);
  await openSession(page, "Reading through broken links");
  await showFile(page, "a.md");
  const reader = page.getByRole("dialog");
  const badLink = reader.getByRole("button", { name: "Open file: Broken link" });
  const goodLink = reader.getByRole("button", { name: "Open file: Good link" });
  const original = reader.getByRole("heading", { name: "Original document" });
  const body = reader.locator(".file-viewer-body:visible");
  await expect(original).toBeVisible();
  await page.clock.install();
  for (const method of ["dismiss", "expire"]) {
    await badLink.focus();
    await body.evaluate((node) => { node.scrollTop = 600; });
    await badLink.press("Enter");
    await expect(reader.getByRole("alert")).toContainText("Couldn't open missing.md.");
    expect(await body.evaluate((node) => node.scrollTop)).toBe(600);
    if (method === "dismiss") {
      await reader.getByRole("button", { name: "Dismiss file opening notice" }).press("Enter");
    } else {
      await page.clock.fastForward(8010);
    }
    await expect(reader.getByRole("alert")).toHaveCount(0);
    await expect(reader.getByRole("tab", { name: "a.md" })).toHaveAttribute("aria-selected", "true");
    expect(await body.evaluate((node) => node.scrollTop)).toBe(600);
  }
  await goodLink.press("Enter");
  await expect(reader.getByRole("heading", { name: "Next document" })).toBeVisible();
  await body.evaluate((node) => { node.scrollTop = 440; });
  await reader.getByRole("tab", { name: "a.md" }).click();
  await reader.getByRole("button", { name: /^Open file: Bad position in B/ }).press("Enter");
  await expect(reader.getByRole("note")).toContainText("The requested lines are outside this file.");
  await expect(reader.getByRole("button", { name: "Reading", exact: true })).toHaveAttribute("aria-pressed", "true");
  expect(await body.evaluate((node) => node.scrollTop)).toBe(440);
});

previewTest("reloading a relative file keeps its document directory binding after a local jump", async ({
  page, request, previewDirectory: directory,
}) => {
  const sessionId = await createSession(request, directory, "Relative reload binding");
  await openAsCurrentAccount(page);
  await page.locator(".agent-item", { hasText: "Octo" }).click();
  await openSession(page, "Relative reload binding");
  const dialog = page.getByRole("dialog");
  const article = dialog.getByRole("article");
  for (const folder of ["before-jump", "after-jump"]) {
    const parent = path.join(directory, folder);
    mkdirSync(parent);
    writeFileSync(path.join(parent, "guide.md"), "[Open report](report.md)");
    writeFileSync(path.join(parent, "report.md"), "# Original report\n\n[Jump locally](#L1)");
    await showFile(page, `${folder}/guide.md`);
    await article.getByRole("button", { name: "Open file: Open report", exact: true }).click();
    await expect(article.getByRole("heading", { name: "Original report" })).toBeVisible();
    if (folder === "after-jump") {
      await article.getByRole("link", { name: "Jump locally", exact: true }).click();
      const located = dialog.locator('[data-line="1"][data-located="true"]');
      await expect(located).toHaveText("# Original report");
      writeFileSync(path.join(parent, "report.md"), "# Reloaded report");
      await dialog.getByRole("button", { name: "Reload file", exact: true }).click();
      await expect(located).toHaveText("# Reloaded report");
    }

    renameSync(parent, `${parent}-retired`);
    mkdirSync(parent);
    writeFileSync(path.join(parent, "report.md"), "# Replacement report");
    const reload = page.waitForResponse((response) =>
      new URL(response.url()).pathname === `/api/sessions/${sessionId}/files/meta`);
    await dialog.getByRole("button", { name: "Reload file", exact: true }).click();
    const response = await reload;
    const query = new URL(response.url()).searchParams;
    expect(query.get("path")).toBe("report.md");
    expect(query.get("source_directory")).toBe(folder);
    expect(query.get("source_directory_id")).toBeTruthy();
    expect(response.status()).toBe(409);
    expect((await response.json()).code).toBe("source_directory_changed");
    await expect(dialog.getByRole("alert")).toHaveText(/document directory changed/i);
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
  }
});

previewTest("reloading a showme alias re-resolves the command after a slow retry, with or without a local jump", async ({
  page, request, previewDirectory: directory,
}) => {
  writeFileSync(path.join(directory, "original.md"), "# Original first line\n\n[Jump locally](#L3)");
  writeFileSync(path.join(directory, "replacement.md"), [
    "# Replacement first line", "Replacement third line", "[Back to top](./replacement.md)",
  ].join("\n\n"));
  const sessionId = await createSession(request, directory, "Alias reload binding");
  await signIn(page);
  await openSession(page, "Alias reload binding");
  const dialog = page.getByRole("dialog");
  const fileEndpoint = `/api/sessions/${sessionId}/files`;
  const resolveEndpoint = `/api/sessions/${sessionId}/showme/resolve`;
  const originalBody = (url: URL) => url.pathname === fileEndpoint && url.searchParams.get("path") === "original.md";
  await page.clock.install();
  for (const jump of [false, true]) {
    const name = jump ? "after-jump.md" : "before-jump.md";
    const alias = path.join(directory, name);
    const command = `${name}#L1`;
    symlinkSync("original.md", alias);
    let releaseBody!: () => void;
    const firstBodyMayFinish = new Promise<void>((resolve) => { releaseBody = resolve; });
    await page.route(originalBody, async (route) => {
      await firstBodyMayFinish;
      await route.abort("aborted").catch(() => {});
    }, { times: 1 });
    const firstBody = page.waitForRequest((read) => originalBody(new URL(read.url())));
    const located = dialog.locator('[data-located="true"]');
    try {
      await showFile(page, command);
      await firstBody;
      await page.clock.fastForward(5_001);
      const retryBody = page.waitForRequest((read) => originalBody(new URL(read.url())));
      await dialog.getByRole("button", { name: "Retry", exact: true }).click();
      const retryQuery = new URL((await retryBody).url()).searchParams;
      expect(retryQuery.get("path")).toBe("original.md");
      expect(retryQuery.get("source")).toBe(directory);
      expect(retryQuery.get("canonical_path_id")).toBe(new URL((await firstBody).url()).searchParams.get("canonical_path_id"));
      await expect(located).toHaveText("# Original first line");
    } finally {
      releaseBody();
    }
    if (jump) {
      await dialog.getByRole("button", { name: "Reading", exact: true }).click();
      await dialog.getByRole("link", { name: "Jump locally", exact: true }).click();
      await expect(located).toHaveAttribute("data-line", "3");
    }
    symlinkSync("replacement.md", alias + ".new");
    renameSync(alias + ".new", alias);
    const reload = page.waitForRequest((read) => {
      const endpoint = new URL(read.url()).pathname;
      return endpoint === resolveEndpoint || endpoint === `${fileEndpoint}/meta`;
    });
    await dialog.getByRole("button", { name: "Reload file", exact: true }).click();
    const reloadRequest = await reload;
    expect(new URL(reloadRequest.url()).pathname).toBe(resolveEndpoint);
    expect(new URL(reloadRequest.url()).searchParams.get("source")).toBeNull();
    expect(reloadRequest.postDataJSON()).toEqual({ text: command });
    await expect(located).toHaveText(jump ? "Replacement third line" : "# Replacement first line");
    if (jump) {
      await dialog.getByRole("button", { name: "Reading", exact: true }).click();
      await dialog.getByRole("button", { name: "Open file: Back to top", exact: true }).click();
      await dialog.getByRole("button", { name: "Reload file", exact: true }).click();
      await expect(dialog.getByRole("heading", { name: "Replacement first line" })).toBeInViewport();
      await expect(dialog.getByRole("button", { name: "Reading", pressed: true })).toBeVisible();
    }
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
  }
});

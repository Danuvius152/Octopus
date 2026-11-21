import { fileURLToPath } from "node:url";
import { deferred } from "../src/test-support/deferred";
import { accountToken, enterAccountCredentials } from "./current-account";
import { expect, test } from "./test";

const original = "/markdown-preview-test.png";
const inlineImage = "/markdown-inline-test.svg";
const fixture = fileURLToPath(new URL("../public/icon-192.png", import.meta.url));
const passage = "This is the paragraph the reader is following.";

const cases = [
  { width: 1120, fails: false, syntax: "plain paragraph", markdown: (image: string) => image },
  { width: 360, fails: true, syntax: "padded author link", markdown: (image: string) => `**[ *${image}* ](/diagram-source)**` },
  { width: 1120, fails: false, syntax: "trailing space entity", markdown: (image: string) => `${image} &#32;` },
  { width: 1120, fails: false, syntax: "tight list with sublist", markdown: (image: string) => `- ${image}\n  - Nested bullet` },
];

for (const { width, fails, syntax, markdown } of cases) {
  test(`a Markdown image keeps the paragraph readable at ${width}px in a ${syntax}`, async ({ browser, page, request }, info) => {
    await page.setViewportSize({ width, height: 640 });
    const imageMarkdown = `![Portrait diagram](${original})`;
    const imported = await request.post("http://localhost:8765/api/sessions/import", {
      headers: { Authorization: `Bearer ${accountToken()}` },
      data: {
        name: "Markdown image preview",
        messages: [{
          role: "assistant", type: "text",
          content: [
            ...Array.from({ length: 16 }, (_, i) => `Earlier paragraph ${i + 1}`),
            markdown(imageMarkdown),
            passage,
            `Use ![Status mark](${inlineImage}) here.`,
            ...Array.from({ length: 32 }, (_, i) => `Later paragraph ${i + 1}`),
          ].join("\n\n"),
        }],
      },
    });
    expect(imported.ok()).toBe(true);

    const firstRequest = deferred<void>();
    const secondRequest = deferred<void>();
    const releaseFirst = deferred<void>();
    const releaseSecond = deferred<void>();
    let attempts = 0;
    await page.route(`**${original}`, async (route) => {
      if (++attempts === 1) {
        firstRequest.resolve();
        await releaseFirst.promise;
        if (fails) {
          await route.fulfill({ status: 503, headers: { "cache-control": "no-store" } });
          return;
        }
      } else {
        secondRequest.resolve();
        await releaseSecond.promise;
      }
      await route.fulfill({ path: fixture });
    });
    await page.route(`**${inlineImage}`, (route) => route.fulfill({
      contentType: "image/svg+xml",
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12"><circle cx="6" cy="6" r="5"/></svg>',
    }));

    const scroller = page.locator(".chat-messages");
    const paragraph = scroller.getByText(passage, { exact: true });
    const image = scroller.getByRole("img", { name: "Portrait diagram" });
    const originalLink = scroller.getByRole("link", { name: "Open original image: Portrait diagram" });
    const retry = scroller.getByRole("button", { name: "Retry image: Portrait diagram" });
    const jump = page.getByRole("button", { name: "Jump to latest" });
    const measure = () => paragraph.evaluate(async (element) => {
      const viewport = element.closest(".chat-messages")!;
      await new Promise<void>((done) => requestAnimationFrame(() => requestAnimationFrame(() => done())));
      const rect = element.getBoundingClientRect();
      const port = viewport.getBoundingClientRect();
      return { offset: rect.top - port.top, visible: rect.top >= port.top && rect.bottom <= port.bottom };
    });
    const loaded = () => image.evaluate((element: HTMLImageElement) => element.complete ? element.naturalWidth : null);

    try {
      await page.goto("/");
      await enterAccountCredentials(page);
      if (width < 769) await page.getByRole("button", { name: "Toggle sidebar", exact: true }).click();
      await page.locator(".agent-item", { hasText: "Octo" }).click();
      await page.locator(".session-item", { hasText: "Markdown image preview" }).click();
      await firstRequest.promise;
      await page.evaluate(() => document.fonts.ready);
      await expect(scroller.getByText("Later paragraph 32", { exact: true })).toBeInViewport();
      const start = await measure();
      await scroller.hover();
      await page.mouse.wheel(0, Math.round(start.offset - 245));
      await expect(jump).toBeVisible();
      await expect(paragraph).toBeInViewport({ ratio: 1 });
      const before = await measure();
      expect(before.visible).toBe(true);
      expect(await loaded()).toBeNull();
      const frame = (await image.boundingBox())!;

      releaseFirst.resolve();
      await expect.poll(loaded).toBe(fails ? 0 : 192);
      const settled = [await measure()];
      expect(Math.abs(settled[0].offset - before.offset)).toBeLessThanOrEqual(2);
      if (fails) {
        await expect(scroller.locator('a[href="/diagram-source"]').getByRole("img", { name: "Portrait diagram" })).toBeVisible();
        await expect(image).toHaveJSProperty("alt", "Portrait diagram");
        await retry.focus();
        await page.keyboard.press("Enter");
        await secondRequest.promise;
        await expect(originalLink).toBeFocused();
        expect(await loaded()).toBeNull();
        settled.push(await measure());
        releaseSecond.resolve();
        await expect.poll(loaded).toBe(192);
        settled.push(await measure());
      }

      for (const sample of settled) {
        expect(sample.visible).toBe(true);
        expect(Math.abs(sample.offset - before.offset)).toBeLessThanOrEqual(2);
      }
      expect(Math.abs(frame.height - frame.width * 5 / 8)).toBeLessThanOrEqual(1);
      expect(frame.width).toBeGreaterThan(width === 360 ? 200 : 400);
      expect(frame.width).toBeLessThanOrEqual(width === 360 ? 260 : 512);
      expect(await image.evaluate((element) => getComputedStyle(element).objectFit)).toBe("contain");
      const after = (await image.boundingBox())!;
      expect(after.height).toBeCloseTo(frame.height, 0);
      expect(after.width).toBeCloseTo(frame.width, 0);
      await expect(originalLink).toHaveAttribute("href", original);
      await expect(originalLink).toHaveAttribute("target", "_blank");
      const inline = scroller.getByRole("img", { name: "Status mark" });
      await expect.poll(() => inline.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBe(12);
      const sameLine = await inline.evaluate((element) => {
        const range = document.createRange();
        range.selectNode(element.previousSibling!);
        const start = range.getBoundingClientRect();
        range.selectNode(element.nextSibling!);
        const end = range.getBoundingClientRect();
        const icon = element.getBoundingClientRect();
        return start.top === end.top && start.right <= icon.left + 1 && icon.right <= end.left + 1;
      });
      expect(sameLine).toBe(true);
      await expect(jump).toBeVisible();
      await info.attach("markdown-image-geometry", {
        body: JSON.stringify({ chromium: browser.version(), width, syntax, fails, before, settled, frame, after, attempts }),
        contentType: "application/json",
      });
    } finally {
      releaseFirst.resolve();
      releaseSecond.resolve();
    }
  });
}

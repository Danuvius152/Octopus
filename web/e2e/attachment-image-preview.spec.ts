import { readFileSync } from "node:fs";
import type { Locator } from "@playwright/test";
import { deferred } from "../src/test-support/deferred";
import { accountToken, enterAccountCredentials } from "./current-account";
import { systemAgentId } from "./system-agent";
import { expect, test } from "./test";

const API = "http://localhost:8765/api";
const imageFile = {
  name: "chat-image.png",
  mimeType: "image/png",
  buffer: readFileSync(new URL("../public/icon-192.png", import.meta.url)),
};
const replyText = "Offline UI execution complete.";

function heldImage(fails: boolean) {
  return { fails, requested: deferred<string>(), release: deferred<void>() };
}

async function measurePreview(scroller: Locator, preview: Locator, paragraph: Locator) {
  const [viewport, frame, text] = await Promise.all([
    scroller.boundingBox(), preview.boundingBox(), paragraph.boundingBox(),
  ]);
  if (!viewport || !frame || !text) throw new Error("The preview and its reply must be mounted");
  const offset = text.y - viewport.y;
  expect(offset).toBeGreaterThanOrEqual(0);
  expect(offset + text.height).toBeLessThanOrEqual(viewport.height + 1);
  expect(frame.x).toBeGreaterThanOrEqual(viewport.x);
  expect(frame.x + frame.width).toBeLessThanOrEqual(viewport.x + viewport.width);
  const distanceBelowPreview = text.y - (frame.y + frame.height);
  expect(distanceBelowPreview).toBeGreaterThanOrEqual(0);
  expect(distanceBelowPreview).toBeLessThan(80);
  return { width: frame.width, height: frame.height, offset };
}

for (const { width, newImageFails } of [{ width: 1120, newImageFails: false }, { width: 360, newImageFails: true }]) {
  test(`attachment previews preserve the following and reading positions at ${width}px`, async ({ page, request }, info) => {
    await page.setViewportSize({ width, height: 640 });
    const headers = { Authorization: `Bearer ${accountToken()}` };
    const response = await request.post(`${API}/sessions`, { headers, data: {
      agent_id: await systemAgentId(request, `${API}/sessions`, headers), name: "Attachment image preview",
    } });
    expect(response.ok()).toBe(true);
    const session = await response.json() as { id: string; name: string };

    const authored = heldImage(newImageFails);
    const saved = heldImage(!newImageFails);
    let imageRequest = authored;
    await page.route(`**/api/sessions/${session.id}/attachments/*?*`, async (route) => {
      if (route.request().resourceType() !== "image") { await route.fallback(); return; }
      const { fails, requested, release } = imageRequest;
      requested.resolve(route.request().url());
      await release.promise;
      if (fails) await route.fulfill({ status: 404 });
      else await route.continue();
    });
    const scroller = page.locator(".chat-messages");
    const preview = scroller.getByRole("link", { name: imageFile.name });
    const image = preview.getByRole("img", { name: imageFile.name });
    const jump = page.getByRole("button", { name: "Jump to latest" });
    const gap = () => scroller.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop);
    const scrollTop = () => scroller.evaluate((element) => element.scrollTop);
    const send = async (content: string) => {
      await page.locator(".chat-input-bar textarea").fill(content);
      await page.getByRole("button", { name: "Send message" }).click();
    };
    const waitForReplies = async (count: number) => {
      let replySequences: number[] = [];
      await expect.poll(async () => {
        const response = await request.get(`${API}/sessions/${session.id}`, { headers });
        expect(response.ok()).toBe(true);
        const snapshot = await response.json() as { messages: { role: string; content: string; seq: number }[] };
        replySequences = snapshot.messages
          .filter((message) => message.role === "assistant" && message.content === replyText)
          .map((message) => message.seq);
        return replySequences.length;
      }).toBe(count);
      return replySequences;
    };
    const openSession = async () => {
      if (width < 769) await page.getByRole("button", { name: "Toggle sidebar", exact: true }).click();
      await page.locator(".agent-item", { hasText: "Octo" }).click();
      await page.locator(".session-item", { hasText: session.name }).click();
      await expect(page.locator(".conn-status.on")).toBeVisible();
    };
    const checkPreview = async ({ fails, requested, release }: ReturnType<typeof heldImage>, paragraph: Locator) => {
      const original = await requested.promise;
      const before = await measurePreview(scroller, preview, paragraph);
      expect(before.width).toBeGreaterThan(100);
      expect(Math.abs(before.height - before.width * 5 / 8)).toBeLessThanOrEqual(1);
      if (width === 360) expect(before.width).toBeLessThan(256);
      expect(await image.evaluate((node: HTMLImageElement) => node.complete)).toBe(false);
      release.resolve();
      await expect.poll(() => image.evaluate((node: HTMLImageElement) =>
        node.complete ? [node.naturalWidth, node.naturalHeight] : null)).toEqual(fails ? [0, 0] : [192, 192]);
      await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      const after = await measurePreview(scroller, preview, paragraph);
      expect(after.width).toBeCloseTo(before.width, 0);
      expect(after.height).toBeCloseTo(before.height, 0);
      expect(Math.abs(after.offset - before.offset)).toBeLessThanOrEqual(2);
      if (!fails) {
        expect(await image.evaluate((node) => getComputedStyle(node).objectFit)).toBe("contain");
      }
      await expect(preview).toHaveAttribute("href", original);
      await expect(preview).toHaveAttribute("target", "_blank");
      return { before, after, fails, original };
    };
    try {
      await page.goto("/");
      await enterAccountCredentials(page);
      await openSession();
      await send(Array.from({ length: 38 }, (_, line) => `Earlier history (${line + 1})`).join("\n"));
      await waitForReplies(1);
      await page.evaluate(() => document.fonts.ready);
      await expect.poll(scrollTop).toBeGreaterThan(100);
      await expect.poll(gap).toBeLessThanOrEqual(2);
      const beforeAttachment = await scrollTop();

      await page.getByTestId("attachment-file-input").setInputFiles(imageFile);
      await expect(page.locator(".attachment-pending")).toContainText(imageFile.name);
      await expect(page.locator(".attachment-pending")).not.toContainText("uploading");
      await send("Photo");

      const [, photoReplySequence] = await waitForReplies(2);
      const paragraph = scroller.locator(`[data-message-key="message:${photoReplySequence}"]`)
        .getByText(replyText, { exact: true });
      await expect(paragraph).toBeInViewport({ ratio: 1 });
      await expect.poll(scrollTop).toBeGreaterThan(beforeAttachment + 50);
      await expect.poll(gap).toBeLessThanOrEqual(2);
      const { original, ...following } = await checkPreview(authored, paragraph);
      await expect.poll(gap).toBeLessThanOrEqual(2);
      await expect(jump).toHaveCount(0);
      const followingTop = await scrollTop();
      const originalFile = await request.get(original);
      expect(originalFile.ok()).toBe(true);
      expect(await originalFile.body()).toEqual(imageFile.buffer);

      await send(
        Array.from({ length: 30 }, (_, line) => `More history below the image (${line + 1})`).join("\n"),
      );
      await waitForReplies(3);

      imageRequest = saved;
      await page.reload({ waitUntil: "domcontentloaded" });
      await openSession();
      await expect(scroller.getByText("More history below the image (30)")).toBeVisible();
      await page.evaluate(() => document.fonts.ready);
      await expect.poll(gap).toBeLessThanOrEqual(2);
      await scroller.hover();
      await page.mouse.wheel(0, Math.round(followingTop - await scrollTop()));
      await expect(jump).toBeVisible();
      await expect.poll(gap).toBeGreaterThan(150);
      await expect(paragraph).toBeInViewport({ ratio: 1 });
      const { original: savedOriginal, ...reading } = await checkPreview(saved, paragraph);
      await expect(jump).toBeVisible();
      expect(new URL(savedOriginal).pathname).toBe(new URL(original).pathname);
      await info.attach("preview-geometry", {
        body: JSON.stringify({ width, beforeAttachment, followingTop, following, reading }),
        contentType: "application/json",
      });
    } finally {
      authored.release.resolve();
      saved.release.resolve();
    }
  });
}

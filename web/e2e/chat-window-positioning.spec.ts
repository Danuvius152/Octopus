import { expect, test } from "./test";
import { capturePaint } from "./paint-capture";
import { writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import type { CDPSession } from "@playwright/test";
import { deferred } from "../src/test-support/deferred";

const scriptSeconds = async (session: CDPSession) => {
  const { metrics } = await session.send("Performance.getMetrics");
  const duration = metrics.find((metric) => metric.name === "ScriptDuration");
  if (!duration) throw new Error("Chromium did not report ScriptDuration");
  return duration.value;
};

for (const count of [200, 1000]) for (const footerHeight of [0, 900, 1800]) {
  test(`${count} saved rows replace a same-tail window with ${footerHeight}px Footer`, async ({ page }, info) => {
    await page.goto("/");
    await page.evaluate(() => document.fonts.ready);
    await capturePaint(page, info, async (paintFrames, onCleanup, session) => {
      await session.send("Performance.enable");
      let scriptsStart = await scriptSeconds(session);
      const probe = await page.evaluateHandle(async ({ count, footerHeight }) => {
        const path = "/e2e/chat-window-probe.ts";
        const module = await import(path) as typeof import("./chat-window-probe");
        return module.mountWindowProbe(count, footerHeight);
      }, { count, footerHeight });
      onCleanup(() => probe.evaluate((probe) => probe.dispose()));
      onCleanup(() => probe.dispose());
      await expect.poll(() => probe.evaluate((probe) => probe.geometry().visible)).toBe(true);
      await expect.poll(() => probe.evaluate((probe) => probe.geometry().gap)).toBeLessThanOrEqual(1);
      if (footerHeight) expect((await probe.evaluate((probe) => probe.geometry())).footerHeight).toBeGreaterThanOrEqual(footerHeight);
      const cold = { ...await probe.evaluate((probe) => probe.report()), scriptSeconds: await scriptSeconds(session) - scriptsStart };
      expect(cold.normalReady).toBe(true);
      expect(cold.samples.some((sample) => sample.incomplete)).toBe(false);
      const reports = [];
      for (const first of [5, 9]) {
        scriptsStart = await scriptSeconds(session);
        const at = await probe.evaluate((probe, first) => probe.replace(first), first);
        await expect.poll(() => probe.evaluate((probe) => probe.geometry().visible)).toBe(true);
        await page.evaluate(async () => {
          for (let i = 0; i < 8; i++) await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
        });
        const report = await probe.evaluate((probe) => probe.report());
        const firstVisible = report.samples.find((sample) => sample.visible);
        expect(firstVisible).toBeDefined();
        expect(firstVisible!.gap, "first visible frame already includes the real Footer").toBeLessThanOrEqual(1);
        expect(report.samples.filter((sample) => sample.visible && sample.gap! > 1)).toEqual([]);
        expect(report.samples.some((sample) => sample.incomplete)).toBe(false);
        expect(report.writes.filter((write) => write.application && write.visible), "no visible application probe").toEqual([]);
        if (footerHeight === 1800) {
          expect(firstVisible!.tailVisible, "tail and bottom need not remain visible together").toBe(false);
          expect(firstVisible!.tailMounted, "the acquired proof survives the real tail's unmount").toBe(false);
        }
        reports.push({ at, firstReadable: firstVisible!.at - at, ...report, scriptSeconds: await scriptSeconds(session) - scriptsStart });
      }
      const reportPath = info.outputPath("window-presentation.json");
      await writeFile(reportPath, JSON.stringify({ count, footerHeight, cold, reports, paintFrames }));
      await info.attach("window-presentation.json", { contentType: "application/json", path: reportPath });
    });
  });
}

for (const [count, footerHeight] of [[200, 120], [1000, 900]]) {
  test(`${count} saved rows reveal through resumed visibility, zoom and late Markdown assets`, async ({ page }, info) => {
    const releaseAssets = deferred<void>();
    const fontRequested = deferred<void>();
    const imageRequested = deferred<void>();
    await page.route(/KaTeX_.*\.woff2(?:\?|$)/, async (route) => {
      fontRequested.resolve();
      await releaseAssets.promise;
      await route.continue();
    });
    await page.route("**/positioning-late-image.png", async (route) => {
      imageRequested.resolve();
      await releaseAssets.promise;
      await route.fulfill({ path: fileURLToPath(new URL("../public/icon-192.png", import.meta.url)) });
    });
    await page.goto("/");
    await capturePaint(page, info, async (paintFrames, onCleanup, session) => {
      onCleanup(() => releaseAssets.resolve());
      await session.send("Performance.enable");
      const scriptsStart = await scriptSeconds(session);
      const probe = await page.evaluateHandle(async ({ count, footerHeight }) => {
        const path = "/e2e/chat-window-probe.ts";
        const { mountWindowProbe } = await import(path) as typeof import("./chat-window-probe");
        // Deliver a controlled visibilitychange without depending on headless tab focus.
        Object.defineProperty(document, "hidden", { configurable: true, value: true });
        return mountWindowProbe(count, footerHeight, "\n\n$$\n\\frac{1}{2}\n$$\n\n![Late image](/positioning-late-image.png)");
      }, { count, footerHeight });
      const viewport = page.locator(".positioning-probe .chat-messages");
      const marks: { event: string; at: number; gap: number | null; visible: boolean }[] = [];
      const mark = async (event: string) => {
        const { at, gap, visible } = await probe.evaluate((probe) => probe.geometry());
        marks.push({ event, at, gap, visible });
      };
      onCleanup(async () => {
        const result = await probe.evaluate((probe) => probe.report());
        const scale = await page.evaluate(() => visualViewport?.scale);
        const reportPath = info.outputPath("window-presentation.json");
        await writeFile(reportPath, JSON.stringify({ count, footerHeight, scale, ...result, paintFrames, marks,
          scriptSeconds: await scriptSeconds(session) - scriptsStart }));
        await info.attach("window-presentation.json", { contentType: "application/json", path: reportPath });
      });
      onCleanup(() => probe.evaluate((probe) => probe.dispose()));
      onCleanup(() => probe.dispose());
      await expect(viewport).toBeAttached();
      await mark("hidden");
      expect(marks.at(-1)?.visible).toBe(false);
      await session.send("Emulation.setPageScaleFactor", { pageScaleFactor: 1.15 });
      const scale = await page.evaluate(() => visualViewport?.scale);
      expect(scale).toBeGreaterThan(1.1);
      await page.evaluate(() => {
        Reflect.deleteProperty(document, "hidden");
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await mark("resumed");
      await Promise.all([fontRequested.promise, imageRequested.promise]);
      await probe.evaluate((probe) => probe.append("\nAccepted text while the font and image are pending."));
      await mark("assets requested");
      const imageLoaded = page.waitForResponse((response) => response.url().endsWith("/positioning-late-image.png") && response.ok());
      releaseAssets.resolve();
      await imageLoaded;
      await expect.poll(() => page.evaluate(() => Array.from(document.fonts).some((face) =>
        face.family.startsWith("KaTeX_") && face.status === "loaded"))).toBe(true);
      await mark("assets delivered");
      for (let i = 0; i < 3; i++) {
        await probe.evaluate((probe) => probe.append("\nContinued output after an asset settles."));
        await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
      }
      await expect(viewport.getByText("Continued output after an asset settles.", { exact: false })).toBeVisible();
      await expect.poll(() => probe.evaluate((probe) => probe.geometry().gap)).toBeLessThanOrEqual(1);
      const result = await probe.evaluate((probe) => probe.report());
      expect(result.normalReady).toBe(true);
      expect(result.timeouts).toBe(0);
    });
  });
}

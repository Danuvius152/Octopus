import { expect, test } from "@playwright/test";

for (const fontNetwork of ["delayed", "offline"] as const) {
  test(`sign-in becomes ready when remote font styles are ${fontNetwork}`, async ({ page }, testInfo) => {
    let releaseFonts!: () => void;
    let finishFonts!: () => void;
    let fontRequested = false;
    const pendingFonts = new Promise<void>((resolve) => { releaseFonts = resolve; });
    const fontsFinished = new Promise<void>((resolve) => { finishFonts = resolve; });
    const events: string[] = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      events.push(`${request.resourceType()} ${url.host}${url.pathname}`);
    });
    page.on("console", (message) => { if (message.type() === "error") events.push(`console: ${message.text()}`); });
    await page.route("https://fonts.googleapis.com/**", async (route) => {
      fontRequested = true;
      if (fontNetwork === "delayed") await pendingFonts;
      await route.abort("failed");
      finishFonts();
    });
    try {
      await page.goto("/", { waitUntil: "load", timeout: 5_000 });
      await expect(page.locator('input[type="password"]')).toBeVisible();
      await expect.poll(() => fontRequested).toBe(true);
      await page.locator('input[type="password"]').fill("changeme");
      await page.locator("button.btn-login").click();
      await expect(page.locator(".conn-status.on")).toBeVisible();
      releaseFonts();
      await fontsFinished;
      await expect(page.locator(".conn-status.on")).toBeVisible();
    } finally {
      releaseFonts();
      await testInfo.attach("page-readiness-network", { body: events.join("\n"), contentType: "text/plain" });
    }
  });
}

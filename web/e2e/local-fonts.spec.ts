import { expect, test } from "./test";

test("English and Chinese fonts load locally without Google Fonts", async ({ page }) => {
  const remoteFonts: string[] = [];
  const fontRequests: string[] = [];
  page.on("request", (request) => {
    if (/^https:\/\/fonts\.(googleapis|gstatic)\.com\//.test(request.url())) {
      remoteFonts.push(request.url());
    }
    if (request.resourceType() === "font") fontRequests.push(request.url());
  });
  // The regression must never download the external fonts it detects.
  await page.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, (route) => route.abort());
  await page.goto("/");
  const faces = await page.evaluate(async () => {
    const samples = [
      ['400 16px "Hanken Grotesk Variable"', "Octopus"],
      ['700 16px "Hanken Grotesk Variable"', "Octopus"],
      ['400 16px "IBM Plex Mono"', "const value = 42"],
      ['500 16px "IBM Plex Mono"', "const value = 42"],
      ['600 16px "IBM Plex Mono"', "const value = 42"],
      ['400 16px "Noto Sans SC Variable"', "中文字体加载"],
      ['500 16px "Noto Sans SC Variable"', "中文字体加载"],
      ['700 16px "Noto Sans SC Variable"', "中文字体加载"],
    ];
    return Promise.all(samples.map(async ([font, text]) => {
      const loaded = await document.fonts.load(font, text);
      return { font, count: loaded.length, loaded: loaded.every((face) => face.status === "loaded") };
    }));
  });
  expect(remoteFonts).toEqual([]);
  for (const face of faces) {
    expect(face.count, face.font).toBeGreaterThan(0);
    expect(face.loaded, face.font).toBe(true);
  }
  expect(fontRequests.length).toBeGreaterThan(0);
  const origin = new URL(page.url()).origin;
  expect(fontRequests.every((url) => new URL(url).origin === origin)).toBe(true);
});

test("offline contexts block remote fonts and other external assets", async ({ page }) => {
  const failures: { url: string; error: string | undefined }[] = [];
  page.on("requestfailed", (request) => {
    failures.push({ url: request.url(), error: request.failure()?.errorText });
  });
  await page.goto("/");
  const urls = [
    "https://fonts.googleapis.com/offline-test-must-not-download.css",
    "https://fonts.gstatic.com/offline-test-must-not-download.woff2",
    "https://example.invalid/offline-test-must-not-download.png",
    "https://example.invalid/offline-test-must-not-download.js",
  ];
  await page.evaluate(async (targets) => {
    await Promise.all(targets.map((url) => fetch(url).catch(() => undefined)));
  }, urls);
  for (const url of urls) {
    await expect.poll(() => failures.find((failure) => failure.url === url)?.error)
      .toMatch(/^net::ERR_BLOCKED_BY_CLIENT(?:\.|$)/);
  }
});

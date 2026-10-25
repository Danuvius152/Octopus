import { test as base } from "@playwright/test";

export { expect } from "@playwright/test";
export type { APIRequestContext, Page, WebSocketRoute } from "@playwright/test";

// Offline pages and popups use local servers or explicit mocked responses.
// A new external asset must not silently turn the UI suite into a download job.
export const test = base.extend<{ _offlineNetwork: void }>({
  _offlineNetwork: [async ({ context }, use) => {
    const offline = process.env.OCTOPUS_E2E_MODE === "offline";
    const pattern = /^https?:\/\//;
    if (offline) await context.route(pattern, (route) => {
      const host = new URL(route.request().url()).hostname;
      return host === "localhost" || host === "127.0.0.1" || host === "[::1]"
        ? route.fallback()
        : route.abort("blockedbyclient");
    });
    await use();
    if (offline) await context.unroute(pattern);
  }, { auto: true }],
});

import { test as base } from "@playwright/test";
import { cleanupAccount, createTestAccount } from "./account-resources";
import { PROVISIONING_ACCOUNT, setCurrentAccount, type TestAccount } from "./current-account";

export { expect } from "@playwright/test";
export type { APIRequestContext, Page, WebSocketRoute } from "@playwright/test";

// Offline pages and popups use local servers or explicit mocked responses.
// A new external asset must not silently turn the UI suite into a download job.
export const test = base.extend<{ _offlineNetwork: void; account: TestAccount | undefined }>({
  account: [async ({ request }, runTest, testInfo) => {
    // These stories provision their own first account on the empty accounts
    // backend. No main server or provisioning administrator is needed.
    if (/\/accounts[^/]*\.spec\.ts$/.test(testInfo.file)) {
      if (testInfo.project.name !== "installation" || process.env.OCTOPUS_E2E_SCOPE !== "accounts") {
        throw new Error("First-account stories require the empty installation scope");
      }
      await runTest(undefined);
      return;
    }
    const admin = process.env.OCTOPUS_E2E_ACCOUNT_TOKEN;
    if (!admin) throw new Error("Missing runner provisioning account");
    // Only connector writes require the seeded administrator. Monitor reads
    // global metrics as an administrator but never mutates installation state.
    const installationAdmin = /\/connectors\.spec\.ts$/.test(testInfo.file);
    const adminReader = /\/monitor\.spec\.ts$/.test(testInfo.file);
    if (installationAdmin && testInfo.project.name !== "installation") throw new Error("Installation writes require the exclusive project");
    const account = installationAdmin || adminReader
      ? { ...PROVISIONING_ACCOUNT, token: admin, user_id: process.env.OCTOPUS_E2E_ACCOUNT_ID! }
      : await createTestAccount(request, admin);
    setCurrentAccount(account);
    await testInfo.attach("account-owner", { body: JSON.stringify({ user_id: account.user_id, username: account.username }), contentType: "application/json" });
    await runTest(account);
    setCurrentAccount(undefined);
    if (!installationAdmin && !adminReader) {
      try { await cleanupAccount(request, account); }
      catch (error) {
        await testInfo.attach("cleanup-error", { body: String(error), contentType: "text/plain" });
        throw error;
      }
    }
  }, { auto: true, timeout: 30_000 }],
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

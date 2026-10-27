import { enterAccountCredentials } from "./current-account";
import { test, expect } from "./test";

for (const status of ["cancelled", "success"] as const) {
  test(`OAuth close consumes the authoritative ${status} result`, async ({ page, context }) => {
    let installations: Record<string, unknown>[] = [];
    let confirm!: () => void;
    const confirmation = new Promise<void>((resolve) => { confirm = resolve; });
    await context.route("**/oauth-test-authorize", (route) => route.fulfill({ body: "Local OAuth test" }));
    await page.route("**/api/connectors/catalog", (route) => route.fulfill({ json: [{
      kind: "test-provider", display_name: "Local provider", category: "test", allows_multiple: true,
      available: true, scopes: [], custom: false, setup_steps: [],
    }] }));
    await page.route(/\/api\/connectors$/, (route) => route.fulfill({ json: installations }));
    await page.route("**/api/connectors/oauth/start", (route) => route.fulfill({ json: {
      login_id: "local-login", authorize_url: "http://localhost:8765/oauth-test-authorize",
    } }));
    await page.route("**/api/connectors/oauth/status/local-login", (route) => route.fulfill({ json: { status: "processing" } }));
    await page.route("**/api/connectors/oauth/cancel", async (route) => {
      await confirmation;
      if (status === "success") installations = [{ id: "saved", kind: "test-provider", label: "Saved local account",
        auth_type: "oauth", scopes: [], needs_reconnect: false, created_at: "2026-10-27T00:00:00Z" }];
      await route.fulfill({ json: { status, installation_id: status === "success" ? "saved" : null, message: null } });
    });
    await page.goto("/");
    await enterAccountCredentials(page);
    await page.locator(".btn-manage-connectors").click();
    await page.locator(".btn-connector-add").click();
    await page.getByRole("button", { name: "Connect", exact: true }).click();
    await expect(page.getByText("Waiting for sign-in in the opened tab…")).toBeVisible();
    await page.getByRole("dialog").getByRole("button", { name: "Close", exact: true }).click();
    await expect(page.getByText("Confirming connection cancellation…")).toBeVisible();
    confirm();
    await expect(page.getByRole("dialog")).toBeHidden();
    if (status === "success") await expect(page.getByText("Saved local account")).toBeVisible();
    else await expect(page.locator(".connector-item")).toHaveCount(0);
  });
}

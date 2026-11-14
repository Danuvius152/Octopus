import { accountToken, enterAccountCredentials } from "./current-account";
import { test, expect, type Page } from "./test";
import { deferred } from "../src/test-support/deferred";

// Connectors UI against the real backend. No real third-party OAuth is needed:
// configuring an OAuth client (or creating a custom connector) just flips a
// connector to "available" — which is the browser-only setup flow we verify.

// The runner gives this installation phase one worker and reuses its backend
// across repetitions. Each case restores the empty configuration it received.

const API = "http://localhost:8765/api";

async function login(page: Page) {
  await page.goto("/");
  await enterAccountCredentials(page);
  await expect(page.locator(".agent-list-header h2")).toHaveText("Agents");
}

// This exclusive installation starts empty; return it to empty between cases.
test.afterEach(async ({ request }) => {
  const headers = { Authorization: `Bearer ${accountToken()}` };
  for (const kind of ["github", "gmail"]) {
    const response = await request.delete(`${API}/connectors/${kind}/oauth-client`, { headers });
    expect(response.ok(), `${kind} OAuth cleanup: HTTP ${response.status()} ${await response.text()}`).toBe(true);
  }
  const cat = await request.get(`${API}/connectors/catalog`, { headers });
  expect(cat.ok(), `Connector inventory: HTTP ${cat.status()}`).toBe(true);
  {
    for (const c of (await cat.json()) as { kind: string; custom: boolean }[]) {
      if (c.custom) {
        const response = await request.delete(`${API}/connectors/custom/${c.kind}`, { headers });
        expect(response.ok(), `${c.kind} cleanup: HTTP ${response.status()} ${await response.text()}`).toBe(true);
      }
    }
  }
});

test.describe("Connectors", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    // Connectors live on their own main-area page now (the console design
    // moved every manage surface out of dialogs), so open it first.
    await page.locator(".btn-manage-connectors").click();
    await expect(page.locator(".connectors-page")).toBeVisible();
  });

  test("the sidebar has a Connectors section", async ({ page }) => {
    await expect(
      page.locator(".btn-manage-connectors", { hasText: "Connectors" })
    ).toBeVisible();
  });

  test("the catalog lists GitHub + Gmail with a Set up action", async ({
    page,
  }) => {
    await page.locator(".btn-connector-add").click();
    await expect(page.locator(".connector-catalog")).toBeVisible();
    const github = page.locator(".connector-catalog-item", { hasText: "GitHub" });
    await expect(github).toBeVisible();
    await expect(
      page.locator(".connector-catalog-item", { hasText: "Gmail" })
    ).toBeVisible();
    // Unconfigured → a Set up button (not a Connect button).
    await expect(github.locator(".btn-connector-setup")).toBeVisible();
  });

  test("setting up a built-in connector in-browser makes it connectable", async ({
    page,
  }) => {
    await page.locator(".btn-connector-add").click();
    const github = page.locator(".connector-catalog-item", { hasText: "GitHub" });
    await github.locator(".btn-connector-setup").click();

    // Setup form: shows the redirect URI to register + client id/secret fields.
    await expect(page.locator("#setup-client-id")).toBeVisible();
    await expect(
      page.locator("code", { hasText: "/api/connectors/oauth/callback" })
    ).toBeVisible();
    await page.locator("#setup-client-id").fill("test-client-id");
    await page.locator("#setup-client-secret").fill("test-secret");
    await page.locator(".btn-connector-save-client").click();

    // Back at the catalog, GitHub is now available → Connect is shown.
    await expect(
      page
        .locator(".connector-catalog-item", { hasText: "GitHub" })
        .locator(".btn-connector-connect")
    ).toBeVisible();
  });

  test("adding and removing a custom connector in-browser", async ({ page }) => {
    await page.locator(".btn-connector-add").click();
    await page.locator(".btn-connector-add-custom").click();

    await page.locator("#cc-kind").fill("linear");
    await page.locator("#cc-name").fill("Linear");
    await page.locator("#cc-auth").fill("https://linear.app/oauth/authorize");
    await page.locator("#cc-token").fill("https://api.linear.app/oauth/token");
    await page.locator("#cc-api").fill("https://api.linear.app");
    await page.locator("#cc-cid").fill("cid");
    await page.locator("#cc-csec").fill("csec");
    await page.locator(".btn-connector-save-custom").click();

    // Appears in the catalog as a custom (available, connectable) connector.
    const linear = page.locator(".connector-catalog-item", { hasText: "Linear" });
    await expect(linear).toBeVisible();
    await expect(linear).toContainText("custom");
    await expect(linear.locator(".btn-connector-connect")).toBeVisible();

    // Remove it.
    await linear.locator(".btn-connector-remove").click();
    await expect(
      page.locator(".connector-catalog-item", { hasText: "Linear" })
    ).toHaveCount(0);
  });

  test("Agent settings shows the per-agent connectors section", async ({
    page,
  }) => {
    const octo = page.locator(".agent-item", { hasText: "Octo" });
    await octo.click();

    await page.locator(".btn-account").click();
    await page.locator(".menu-agent-settings").click();
    await expect(page.locator(".agent-settings")).toBeVisible();
    await expect(page.locator("#agent-name")).toHaveValue("Octo");
    await expect(page.locator(".agent-connectors")).toContainText(
      "No connectors installed"
    );
  });
});

const setupAPI = "http://localhost:8765/api/connectors";
const setupHeaders = { get Authorization() { return `Bearer ${accountToken()}`; } };
const catalog = ["gmail", "github"].map((kind) => ({ kind, display_name: kind, available: false }));

async function start(page: Page) {
  await page.route("**/api/connectors/catalog", (route) => route.fulfill({ json: catalog }));
  await page.goto("/");
  await enterAccountCredentials(page);
  await page.locator(".btn-manage-connectors").click();
  await page.locator(".btn-connector-add").click();
}
async function setup(page: Page, kind: string) {
  await page.locator(".connector-catalog-item", { hasText: kind }).getByRole("button", { name: "Set up" }).click();
}

for (const kind of ["gmail", "github"]) {
  test(`late Gmail and current ${kind} reads preserve the current PUT's credentials`, async ({ page, request }) => {
    const old = deferred<void>();
    const current = deferred<void>();
    let reads = 0;
    await page.route("**/api/connectors/*/oauth-client", async (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      const first = ++reads === 1;
      await (first ? old.promise : current.promise);
      await route.fulfill({ json: {
        client_id: first ? "old-gmail" : "saved-current",
        redirect_uri: `https://${first ? "old" : "current"}.test/callback`,
      } });
    });
    try {
      await start(page);
      await setup(page, "gmail");
      await expect.poll(() => reads).toBe(1);
      await page.getByRole("button", { name: "Back" }).click();
      await setup(page, kind);
      await expect.poll(() => reads).toBe(2);
      await page.getByLabel("Client ID", { exact: true }).fill("typed-current");
      await page.getByLabel("Client secret", { exact: true }).fill("current-secret");
      current.resolve();
      await expect(page.locator("code")).toHaveText("https://current.test/callback");
      const received = page.waitForResponse((response) => response.url().endsWith("/gmail/oauth-client"));
      old.resolve();
      await received;
      await expect(page.getByLabel("Client ID", { exact: true })).toHaveValue("typed-current");
      await expect(page.getByLabel("Client secret", { exact: true })).toHaveValue("current-secret");
      await expect(page.locator("code")).toHaveText("https://current.test/callback");
      const submitted = page.waitForRequest((sent) => sent.method() === "PUT" && sent.url().endsWith(`/${kind}/oauth-client`));
      await page.getByRole("button", { name: "Save", exact: true }).click();
      expect((await submitted).postDataJSON()).toEqual({ client_id: "typed-current", client_secret: "current-secret" });
      await expect(page.getByLabel("Client ID", { exact: true })).toBeHidden();
      const stored = await request.get(`${setupAPI}/${kind}/oauth-client`, { headers: setupHeaders });
      expect(stored.ok()).toBe(true);
      expect(await stored.json()).toMatchObject({ client_id: "typed-current", configured: true });
    } finally {
      old.resolve(); current.resolve();
    }
  });
}

test("a committed Gmail setup cannot close or unlock a newer GitHub submission", async ({ page, request }) => {
  const old = deferred<void>();
  const current = deferred<void>();
  const committed = deferred<void>();
  await page.route("**/api/connectors/*/oauth-client", async (route) => {
    if (route.request().method() !== "PUT") return route.fallback();
    const response = await route.fetch();
    expect(response.ok()).toBe(true);
    if (route.request().url().includes("/gmail/")) {
      committed.resolve();
      await old.promise;
    } else await current.promise;
    await route.fulfill({ response });
  });
  try {
    await start(page);
    await setup(page, "gmail");
    await page.getByLabel("Client ID", { exact: true }).fill("gmail-original");
    await page.getByLabel("Client secret", { exact: true }).fill("gmail-secret");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await committed.promise;
    const stored = await request.get(`${setupAPI}/gmail/oauth-client`, { headers: setupHeaders });
    expect(await stored.json()).toMatchObject({ client_id: "gmail-original", configured: true });
    await page.getByRole("button", { name: "Back" }).click();
    await setup(page, "github");
    await page.getByLabel("Client ID", { exact: true }).fill("github-current");
    await page.getByLabel("Client secret", { exact: true }).fill("github-secret");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    const received = page.waitForResponse((response) => response.request().method() === "PUT"
      && response.url().endsWith("/gmail/oauth-client"));
    old.resolve();
    await received;
    await expect(page.getByLabel("Client ID", { exact: true })).toHaveValue("github-current");
    await expect(page.getByRole("button", { name: "Saving…", exact: true })).toBeDisabled();
    current.resolve();
    await expect(page.getByLabel("Client ID", { exact: true })).toBeHidden();
    const github = await request.get(`${setupAPI}/github/oauth-client`, { headers: setupHeaders });
    expect(await github.json()).toMatchObject({ client_id: "github-current", configured: true });
  } finally {
    old.resolve(); current.resolve(); committed.resolve();
  }
});

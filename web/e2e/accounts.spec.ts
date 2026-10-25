import { test, expect, type Page } from "./test";

import {
  E2E_ACCOUNTS_PORT,
  E2E_ACCOUNTS_WEB_PORT,
} from "../playwright.config";

/**
 * Accounts, end to end (multi-tenancy.md §3, §5, §9).
 *
 * This spec drives a backend of its own, left empty until the first-account
 * setup test. The other specs use an already-created account on the shared host.
 *
 * The tests run in order, because they are one story: a single-user install
 * gets its first account, that account invites somebody, and the two of them
 * cannot see each other's work.
 */

test.describe.configure({ mode: "serial" });
test.use({ baseURL: `http://localhost:${E2E_ACCOUNTS_WEB_PORT}` });

const API = `http://localhost:${E2E_ACCOUNTS_PORT}/api`;
const INSTALL_TOKEN = "changeme";
const OWNER = { username: "archer", password: "owner-password-1" };
const GUEST = { username: "vera", password: "guest-password-2" };

/** Sign in as an account — the way in once one exists. */
async function signIn(page: Page, who: { username: string; password: string }) {
  await page.goto("/");
  await expect(page.locator("#username")).toBeVisible();
  await page.locator("#username").fill(who.username);
  await page.locator("#password").fill(who.password);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header")).toBeVisible();
}

async function openAccountPage(page: Page) {
  await page.locator("button.btn-account").click();
  await page.locator(".menu-account").click();
  await expect(page.locator(".account-page")).toBeVisible();
}

test.describe("Accounts", () => {
  test("a fresh install requires setup before business is available", async ({ page, request }) => {
    await page.goto("/");
    await expect(page.locator("#setup-proof")).toBeVisible();
    await expect(page.locator("#username")).toBeVisible();
    await expect(page.locator("#password")).toBeVisible();
    const response = await request.get(`${API}/sessions`, { headers: { Authorization: `Bearer ${INSTALL_TOKEN}` } });
    expect(response.status()).toBe(401);
  });

  test("setup creates the first owner before its first conversation", async ({ page }) => {
    await page.goto("/");
    await page.locator("#setup-proof").fill(INSTALL_TOKEN);
    await page.locator("#username").fill(OWNER.username);
    await page.locator("#password").fill(OWNER.password);
    await page.locator("button.btn-setup").click();
    await expect(page.locator(".account-handle")).toHaveText(OWNER.username);
    expect(await page.evaluate(() => localStorage.getItem("octopus_token"))).not.toBe(INSTALL_TOKEN);

    await page
      .locator(".agent-item", { hasText: "Octo" })
      .locator(".btn-session-add")
      .click();
    await page
      .locator('.session-create input[placeholder="Session name"]')
      .fill("Owned first conversation");
    await page.locator("button.btn-create").click();
    await expect(
      page.locator(".session-item .session-name", { hasText: "Owned first conversation" })
    ).toBeVisible();

    await page.reload();
    const rail = page.locator(".agent-item", { hasText: "Octo" });
    await expect(rail).toBeVisible();
    if (await rail.locator(".agent-fold.rotate-90").count() === 0) await rail.click();
    await expect(
      page.locator(".session-item .session-name", { hasText: "Owned first conversation" })
    ).toBeVisible();
  });

  test("the install token no longer signs anybody in", async ({ request }) => {
    const res = await request.get(`${API}/sessions`, {
      headers: { Authorization: `Bearer ${INSTALL_TOKEN}` },
    });
    expect(res.status()).toBe(401);
  });

  test("the sign-in screen now asks for a username", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator("#username")).toBeVisible();
    await expect(page.locator("#token")).toHaveCount(0);
  });

  test("the owner signs in and the sidebar says who they are", async ({ page }) => {
    await signIn(page, OWNER);
    await expect(page.locator(".account-handle")).toHaveText(OWNER.username);
  });

  test("an account's rewind keeps its conversation after reload", async ({ page, request }) => {
    await signIn(page, OWNER);
    const bearer = await page.evaluate(() => localStorage.getItem("octopus_token") ?? "");
    const headers = { Authorization: `Bearer ${bearer}` };
    const name = "Owned rewind history";
    const imported = await request.post(`${API}/sessions/import`, {
      headers,
      data: {
        name,
        backend: "codex",
        messages: [
          { role: "user", type: "text", content: "keep the first question" },
          { role: "assistant", type: "text", content: "keep the first answer" },
          { role: "user", type: "text", content: "redo the next question" },
          { role: "assistant", type: "text", content: "old answer" },
        ],
      },
    });
    expect(imported.ok()).toBeTruthy();
    const parent = await imported.json();
    let forkId: string | null = null;
    try {
      await page.reload();
      const agent = page.locator(".agent-item", { hasText: "Octo" });
      await agent.click();
      await page.locator(".session-item .session-name", { hasText: name }).click();
      const message = page.locator(".msg-user").nth(1);
      await message.hover();
      await message.locator('[data-testid="fork-from-here"]').click();
      const created = page.waitForResponse(
        (response) => response.url().endsWith(`/sessions/${parent.id}/fork`) &&
          response.request().method() === "POST",
      );
      await page.locator(".btn-create-fork").click();
      const fork = await (await created).json();
      forkId = fork.id;
      await expect(page.locator('[data-testid="fork-banner"]')).toBeVisible();
      await expect(page.locator(".msg-assistant")).toContainText("keep the first answer");

      await page.reload();
      await page.locator(".agent-item", { hasText: "Octo" }).click();
      const session = page.locator(".session-item .session-name", { hasText: name });
      await expect(session).toHaveCount(1);
      await session.click();
      await expect(page.locator(".msg-user")).toContainText("keep the first question");
      await expect(page.locator(".msg-assistant")).toContainText("keep the first answer");
      await expect(page.locator(".chat-input-bar textarea")).toHaveValue("redo the next question");
      const history = await request.get(`${API}/sessions/${parent.id}`, { headers });
      expect(history.ok()).toBeTruthy();
      expect((await history.json()).messages).toHaveLength(4);
    } finally {
      for (const id of [forkId, parent.id]) {
        if (id) await request.delete(`${API}/sessions/${id}`, { headers });
      }
    }
  });

  test("a wrong password is refused, and says so", async ({ page }) => {
    await page.goto("/");
    await page.locator("#username").fill(OWNER.username);
    await page.locator("#password").fill("not the password");
    await page.locator("button.btn-login").click();
    await expect(page.locator(".signin-error")).toBeVisible();
    // Still outside.
    await expect(page.locator(".agent-list-header")).toHaveCount(0);
  });

  test("the owner invites somebody, who joins with the code", async ({ page }) => {
    await signIn(page, OWNER);
    await openAccountPage(page);

    await page.locator("button.btn-new-invite").click();
    const code = page.locator(".invite-code").first();
    await expect(code).toBeVisible();
    const inviteCode = ((await code.textContent()) ?? "").trim();
    expect(inviteCode.length).toBeGreaterThan(0);

    // Sign out, then register as the invited person.
    await page.locator("button.btn-account").click();
    await page.locator(".menu-sign-out").click();

    await expect(page.locator("#username")).toBeVisible();
    await page.locator("button.btn-have-invite").click();
    await page.locator("#invite").fill(inviteCode);
    await page.locator("#username").fill(GUEST.username);
    await page.locator("#password").fill(GUEST.password);
    await page.locator("button.btn-register").click();

    await expect(page.locator(".agent-list-header")).toBeVisible();
    await expect(page.locator(".account-handle")).toHaveText(GUEST.username);
  });

  test("the invited account sees none of the owner's work", async ({ page }) => {
    await signIn(page, GUEST);
    await expect(
      page.locator(".session-item .session-name", { hasText: "Owned first conversation" })
    ).toHaveCount(0);
  });

  test("an invited account is not an admin", async ({ page }) => {
    await signIn(page, GUEST);
    await openAccountPage(page);
    // Their own password, yes. Other people's accounts, no.
    await expect(page.locator(".account-password")).toBeVisible();
    await expect(page.locator(".account-invites")).toHaveCount(0);
    await expect(page.locator(".account-people")).toHaveCount(0);
  });

  test("an admin cannot disable their own account", async ({ page }) => {
    await signIn(page, OWNER);
    await openAccountPage(page);
    await expect(page.locator(".account-people")).toBeVisible();
    await expect(page.locator(".person-row")).toHaveCount(2);
    // One toggle, on the other person — never on the row that is you.
    await expect(page.locator("button.btn-toggle-disabled")).toHaveCount(1);
    await expect(
      page.locator(".person-row", { hasText: OWNER.username })
    ).not.toContainText("disabled");
  });

  for (const entry of ["sidebar", "settings"] as const) {
    test(`signing out from ${entry} gives the bearer back after logout completes`, async ({ page, request }) => {
      await signIn(page, OWNER);
      const bearer = await page.evaluate(
        () => localStorage.getItem("octopus_token") ?? ""
      );
      expect(bearer.length).toBeGreaterThan(0);
      expect(
        (await request.get(`${API}/auth/identity`, {
          headers: { Authorization: `Bearer ${bearer}` },
        })).status()
      ).toBe(200);

      const otherBearer = (await (await request.post(`${API}/auth/login`, { data: OWNER })).json()).token as string;
      let release!: () => void;
      const logoutGate = new Promise<void>((resolve) => { release = resolve; });
      let received = false;
      await page.route("**/api/auth/logout", async (route) => {
        expect(route.request().headers().authorization).toBe(`Bearer ${bearer}`);
        received = true;
        await logoutGate;
        await route.continue();
      });

      await page.locator("button.btn-account").click();
      if (entry === "settings") {
        await page.locator(".menu-settings").click();
        await page.getByRole("tab", { name: "Account", exact: true }).click();
        await page.locator(".settings-dialog .btn-signout").click();
        await expect(page.locator(".settings-dialog")).toHaveCount(0);
      } else {
        await page.locator(".menu-sign-out").click();
      }
      await expect.poll(() => received).toBe(true);
      expect(await page.evaluate(() => localStorage.getItem("octopus_token"))).toBe(bearer);
      await expect(page.locator(".agent-list-header")).toBeVisible();
      release();
      await expect(page.locator("#username")).toBeVisible();

      // The token the browser was holding is no longer one the server honours.
      await expect
        .poll(async () =>
          (await request.get(`${API}/auth/identity`, {
            headers: { Authorization: `Bearer ${bearer}` },
          })).status()
        )
        .toBe(401);
      expect((await request.get(`${API}/auth/identity`, {
        headers: { Authorization: `Bearer ${otherBearer}` },
      })).status()).toBe(200);
      await request.post(`${API}/auth/logout`, { headers: { Authorization: `Bearer ${otherBearer}` } });
    });
  }

  test("settings still signs out locally when logout cannot reach the server", async ({ page, request }) => {
    await signIn(page, OWNER);
    const bearer = await page.evaluate(() => localStorage.getItem("octopus_token") ?? "");
    await page.route("**/api/auth/logout", (route) => route.abort("failed"));
    await page.locator("button.btn-account").click();
    await page.locator(".menu-settings").click();
    await page.getByRole("tab", { name: "Account", exact: true }).click();
    await page.locator(".settings-dialog .btn-signout").click();
    await expect(page.locator("#username")).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem("octopus_token"))).toBe("");
    // The synthetic network failure prevented revocation; clean up this
    // test-owned bearer through the independent request client.
    expect((await request.get(`${API}/auth/identity`, {
      headers: { Authorization: `Bearer ${bearer}` },
    })).status()).toBe(200);
    await request.post(`${API}/auth/logout`, { headers: { Authorization: `Bearer ${bearer}` } });
  });

  test("same-page reauthentication clears the previous account's transcript and draft", async ({ page, request }) => {
    await signIn(page, OWNER);
    const bearer = await page.evaluate(() => localStorage.getItem("octopus_token") ?? "");
    const imported = await request.post(`${API}/sessions/import`, {
      headers: { Authorization: `Bearer ${bearer}` },
      data: { name: "Private owner transcript", backend: "codex",
        messages: [{ role: "user", type: "text", content: "OWNER-PRIVATE-MESSAGE" }] },
    });
    expect(imported.status()).toBe(201);
    await page.reload();
    await page.locator(".agent-item").first().click();
    await page.locator(".session-item", { hasText: "Private owner transcript" }).click();
    await expect(page.getByText("OWNER-PRIVATE-MESSAGE", { exact: true })).toBeVisible();
    await page.locator("textarea").fill("OWNER-PRIVATE-DRAFT");
    await page.getByTestId("attachment-file-input").setInputFiles({ name: "owner-private.txt", mimeType: "text/plain", buffer: Buffer.from("Private attachment") });
    await expect(page.getByRole("button", { name: "Remove owner-private.txt" })).toBeVisible();
    expect((await request.post(`${API}/auth/logout`, { headers: { Authorization: `Bearer ${bearer}` } })).ok()).toBeTruthy();
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect(page.locator("#username")).toBeVisible();
    await page.locator("#username").fill(GUEST.username);
    await page.locator("#password").fill(GUEST.password);
    await page.locator("button.btn-login").click();
    await expect(page.locator(".account-handle")).toHaveText(GUEST.username);
    await expect(page.getByText("OWNER-PRIVATE-MESSAGE", { exact: true })).toHaveCount(0);
    await expect(page.locator(".session-item", { hasText: "Private owner transcript" })).toHaveCount(0);
    // This invited account has no agents yet. Create its own without a
    // reload, preserving the same-page identity transition under test.
    await page.locator(".btn-agent-add").click();
    await page.locator("#agent-name").fill("Guest reauthentication agent");
    await page.locator(".btn-agent-save").click();
    await page.locator(".agent-item", { hasText: "Guest reauthentication agent" }).locator(".btn-session-add").click();
    await page.locator('.session-create input[placeholder="Session name"]').fill("Guest after reauthentication");
    await page.locator("button.btn-create").click();
    await expect(page.locator("textarea")).toHaveValue("");
    await expect(page.getByRole("button", { name: "Remove owner-private.txt" })).toHaveCount(0);
  });

  test("changing the password ends the other sessions", async ({ page, request }) => {
    await signIn(page, GUEST);
    const stale = (
      await (
        await request.post(`${API}/auth/login`, { data: GUEST })
      ).json()
    ).token as string;
    expect(
      (await request.get(`${API}/auth/identity`, {
        headers: { Authorization: `Bearer ${stale}` },
      })).status()
    ).toBe(200);

    await openAccountPage(page);
    await page.locator("#current-password").fill(GUEST.password);
    await page.locator("#new-password").fill("guest-password-3");
    await page.locator("button.btn-change-password").click();
    await expect(page.locator(".password-done")).toBeVisible();

    // The other device is out; this one is still in.
    expect(
      (await request.get(`${API}/auth/identity`, {
        headers: { Authorization: `Bearer ${stale}` },
      })).status()
    ).toBe(401);
    await expect(page.locator(".account-page")).toBeVisible();
  });
});

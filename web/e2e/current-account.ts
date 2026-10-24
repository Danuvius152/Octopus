import type { Page } from "@playwright/test";

// global-setup exchanges the setup proof through the real API before workers start.
export const TEST_BEARER = process.env.OCTOPUS_E2E_ACCOUNT_TOKEN ?? "";
export const TEST_ACCOUNT = { username: "ui-owner", password: "ui-owner-password" };

export async function enterAccountCredentials(page: Page): Promise<void> {
  await page.locator("#username").fill(TEST_ACCOUNT.username);
  await page.locator("#password").fill(TEST_ACCOUNT.password);
  await page.locator("button.btn-login").click();
}

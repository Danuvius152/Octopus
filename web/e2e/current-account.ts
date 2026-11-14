import type { Page } from "@playwright/test";

export interface TestAccount {
  username: string;
  password: string;
  token: string;
  user_id: string;
}

export const PROVISIONING_ACCOUNT = { username: "ui-owner", password: "ui-owner-password" };

// Playwright runs one test at a time in each worker process. Only the test
// fixture sets this binding; helpers read it at call time, never at import.
let current: TestAccount | undefined;

export function setCurrentAccount(account: TestAccount | undefined): void {
  current = account;
}

export function accountToken(): string {
  if (!current) throw new Error("Account access requires the test-scoped account fixture");
  return current.token;
}

export async function enterAccountCredentials(page: Page,
  credentials: Pick<TestAccount, "username" | "password"> | undefined = current): Promise<void> {
  if (!credentials) throw new Error("Login requires the test-scoped account fixture");
  await page.locator("#username").fill(credentials.username);
  await page.locator("#password").fill(credentials.password);
  await page.locator("button.btn-login").click();
}

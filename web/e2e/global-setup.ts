import { TEST_ACCOUNT } from "./current-account";

/** Seed only the shared test host; the separate first-setup host stays empty. */
export default async function setup(): Promise<void> {
  const response = await fetch("http://localhost:8765/api/auth/setup", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer changeme" },
    body: JSON.stringify(TEST_ACCOUNT),
  });
  if (!response.ok) throw new Error(`Test account setup failed: HTTP ${response.status}`);
  const account = await response.json() as { token?: string };
  if (!account.token) throw new Error("Test account setup did not issue a bearer");
  process.env.OCTOPUS_E2E_ACCOUNT_TOKEN = account.token;
}

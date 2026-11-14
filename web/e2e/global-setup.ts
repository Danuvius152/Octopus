/** Fixture provisioning authority; business tests receive separate accounts. */
import { PROVISIONING_ACCOUNT } from "./current-account";

export default async function setup(): Promise<void> {
  if (process.env.OCTOPUS_E2E_SCOPE === "accounts") return;
  const response = await fetch("http://localhost:8765/api/auth/setup", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer changeme" },
    body: JSON.stringify(PROVISIONING_ACCOUNT),
  });
  if (!response.ok) throw new Error(`Test account setup failed: HTTP ${response.status}`);
  const account = await response.json() as { token?: string; user_id: string };
  if (!account.token) throw new Error("Test account setup did not issue a bearer");
  process.env.OCTOPUS_E2E_ACCOUNT_TOKEN = account.token;
  process.env.OCTOPUS_E2E_ACCOUNT_ID = account.user_id;
}

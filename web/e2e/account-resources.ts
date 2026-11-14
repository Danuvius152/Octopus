import { randomUUID } from "node:crypto";
import type { APIRequestContext, APIResponse } from "@playwright/test";
import type { TestAccount } from "./current-account";

export const ACCOUNT_API = "http://localhost:8765/api";

async function checked(response: APIResponse): Promise<APIResponse> {
  if (!response.ok()) {
    throw new Error(`${response.url()}: HTTP ${response.status()} ${await response.text()}`);
  }
  return response;
}

export async function createTestAccount(request: APIRequestContext, admin: string): Promise<TestAccount> {
  // Every attempt, retry/repeat and rebuilt worker creates a new namespace.
  const username = `e2e-${randomUUID().replaceAll("-", "").slice(0, 24)}`;
  const password = randomUUID();
  const invite = await checked(await request.post(`${ACCOUNT_API}/auth/invites`, {
    headers: { Authorization: `Bearer ${admin}` }, data: { max_uses: 1, ttl_days: 1 },
  }));
  const { code } = await invite.json();
  try {
    const response = await checked(await request.post(`${ACCOUNT_API}/auth/register`, {
      data: { username, password, invite_code: code },
    }));
    return { ...await response.json(), password };
  } finally {
    await checked(await request.delete(`${ACCOUNT_API}/auth/invites/${code}`, {
      headers: { Authorization: `Bearer ${admin}` },
    }));
  }
}

/** The real account creation receipt owns the whole namespace, including
 * server-created successors/children whose response never reached the browser.
 * Archived sessions are already retired and cannot be DELETEd through the
 * product API; the runner destroys those rows/files after stopping the backend.
 * Names never grant ownership. */
export async function cleanupAccount(request: APIRequestContext, account: TestAccount): Promise<void> {
  const headers = { Authorization: `Bearer ${account.token}` };
  const errors: Error[] = [];
  for (const [collection, query, action] of [
    ["schedules", "", ""],
    ["notifiers", "", ""],
    ["applications", "", ""],
    ["applications", "?archived=true", ""],
    ["sessions", "", ""],
    ["credentials", "", ""],
    ["agents", "?include_archived=true", "/archive"],
  ]) {
    try {
      const response = await checked(await request.get(`${ACCOUNT_API}/${collection}${query}`, { headers, timeout: 3_000 }));
      const rows = await response.json() as { id: string; is_system?: boolean; archived?: boolean }[];
      for (const row of rows) {
        if (collection === "agents" && (row.is_system || row.archived)) continue;
        const url = `${ACCOUNT_API}/${collection}/${row.id}${action}`;
        try {
          await checked(await (action ? request.post(url, { headers, timeout: 3_000 }) : request.delete(url, { headers, timeout: 3_000 })));
        } catch (error) { errors.push(new Error(`${collection}/${row.id}: ${String(error)}`)); }
      }
    } catch (error) { errors.push(new Error(`${collection} inventory: ${String(error)}`)); }
  }
  try { await checked(await request.post(`${ACCOUNT_API}/auth/logout`, { headers, timeout: 3_000 })); }
  catch (error) { errors.push(new Error(`account ${account.user_id} logout: ${String(error)}`)); }
  if (errors.length) throw new AggregateError(errors, `Cleanup failed for account ${account.user_id}:\n${errors.map(String).join("\n")}`);
}

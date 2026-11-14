import { test, expect, type APIRequestContext } from "./test";
import { ACCOUNT_API as API, cleanupAccount, createTestAccount } from "./account-resources";
import type { TestAccount } from "./current-account";

const headers = (account: TestAccount) => ({ Authorization: `Bearer ${account.token}` });

async function imported(request: APIRequestContext, account: TestAccount) {
  const response = await request.post(`${API}/sessions/import`, {
    headers: headers(account),
    data: { name: "Windowed Transcript", messages: [{ role: "user", type: "text", content: "owned" }] },
  });
  expect(response.ok()).toBe(true);
  return response.json() as Promise<{ id: string }>;
}

test("ending A cannot delete B's same-name session, including an archived successor", async ({ request, account }) => {
  if (!account) throw new Error("This isolation test requires an owned account");
  const admin = process.env.OCTOPUS_E2E_ACCOUNT_TOKEN!;
  const [a, b] = await Promise.all([createTestAccount(request, admin), createTestAccount(request, admin)]);
  const [first, second] = await Promise.all([imported(request, a), imported(request, b)]);
  const archived = await request.post(`${API}/sessions/${first.id}/archive`, { headers: headers(a) });
  expect(archived.ok()).toBe(true);
  const successor = await archived.json();
  const deleted: string[] = [];
  const observed = new Proxy(request, { get(target, property) {
    if (property === "delete") return async (...args: Parameters<APIRequestContext["delete"]>) => {
      deleted.push(String(args[0]));
      return target.delete(...args);
    };
    const value = Reflect.get(target, property);
    return typeof value === "function" ? value.bind(target) : value;
  } });
  try {
    await cleanupAccount(observed, a);
    expect(deleted).not.toContain(`${API}/sessions/${first.id}`);
    expect(deleted).toContain(`${API}/sessions/${successor.id}`);
    expect(deleted).not.toContain(`${API}/sessions/${second.id}`);
    expect((await request.get(`${API}/sessions/${second.id}`, { headers: headers(b) })).ok()).toBe(true);
    expect((await request.patch(`${API}/sessions/${second.id}`, { headers: headers(b), data: { name: "B remains editable" } })).ok()).toBe(true);
    expect((await request.get(`${API}/sessions/${second.id}`, { headers: headers(account) })).status()).toBe(404);
  } finally { await cleanupAccount(request, b); }
});

test("concurrent whole pin-order writes are confined to their creation accounts", async ({ request, account }) => {
  if (!account) throw new Error("This isolation test requires an owned account");
  const b = await createTestAccount(request, process.env.OCTOPUS_E2E_ACCOUNT_TOKEN!);
  async function seed(owner: TestAccount, reverse: boolean) {
    const initial = await request.get(`${API}/agents`, { headers: headers(owner) });
    expect(initial.ok()).toBe(true);
    const system = (await initial.json() as { id: string; is_system: boolean }[]).find((row) => row.is_system)!;
    const ids = [system.id];
    for (const name of ["Pin Alpha", "Pin Beta"]) {
      const response = await request.post(`${API}/agents`, { headers: headers(owner), data: { name } });
      expect(response.ok(), await response.text()).toBe(true);
      ids.push((await response.json()).id);
    }
    const order = reverse ? ids.toReversed() : ids;
    return order;
  }
  try {
    // Creating Agents takes an installation-wide memory guard. The whole-set
    // pin-order writes under test remain genuinely simultaneous.
    const orders = [await seed(account, false), await seed(b, true)];
    const writes = await Promise.all([account, b].map((owner, index) =>
      request.put(`${API}/agents/pin-order`, { headers: headers(owner), data: { ids: orders[index] } })));
    expect(writes.every((response) => response.ok())).toBe(true);
    for (const [index, owner] of [account, b].entries()) {
      const response = await request.get(`${API}/agents`, { headers: headers(owner) });
      const rows = await response.json() as { id: string; pin_order: number }[];
      expect(rows.sort((a,b) => a.pin_order - b.pin_order).map((row) => row.id)).toEqual(orders[index]);
    }
  } finally { await cleanupAccount(request, b); }
});

test("cleanup reports the exact failed resource and HTTP status while B remains usable", async ({ request }) => {
  const admin = process.env.OCTOPUS_E2E_ACCOUNT_TOKEN!;
  const [a, b] = await Promise.all([createTestAccount(request, admin), createTestAccount(request, admin)]);
  const [first, second] = await Promise.all([imported(request, a), imported(request, b)]);
  const failing = new Proxy(request, { get(target, property) {
    if (property === "delete") return async (...args: Parameters<APIRequestContext["delete"]>) =>
      String(args[0]).endsWith(`/sessions/${first.id}`)
        ? target.get(`${API}/sessions/injected-missing-resource`, { headers: headers(a) })
        : target.delete(...args);
    const value = Reflect.get(target, property);
    return typeof value === "function" ? value.bind(target) : value;
  } });
  try {
    await expect(cleanupAccount(failing, a)).rejects.toThrow(new RegExp(`sessions/${first.id}:.*HTTP 404`));
    expect((await request.get(`${API}/sessions/${second.id}`, { headers: headers(b) })).ok()).toBe(true);
  } finally { await cleanupAccount(request, b); }
});

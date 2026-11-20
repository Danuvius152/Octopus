import type { Page, Request as BrowserRequest } from "@playwright/test";
import { deferred } from "../src/test-support/deferred";
import { accountToken, enterAccountCredentials } from "./current-account";
import { test, expect } from "./test";

const agentsUrl = "http://localhost:8765/api/agents";
const headers = { get Authorization() { return `Bearer ${accountToken()}`; } };

async function stubConnectorCatalog(page: Page, ids: string[]) {
  await page.route(/\/api\/connectors$/, (route) => route.fulfill({ json: ids.map((id) => ({
    id, kind: "github", label: `${id} account`, auth_type: "oauth", scopes: [], needs_reconnect: false,
    external_account_id: id, enable_by_default: false, created_at: "2026-11-10T00:00:00Z",
  })) }));
}

test("a saved Agent remains saved while its delayed receipt leaves the reopened draft alone", async ({ page, request }) => {
  const created = await request.post(agentsUrl, { headers, data: { name: "E2E Form lifetime" } });
  expect(created.ok()).toBe(true);
  const agent = await created.json();
  let release!: () => void;
  const receipt = new Promise<void>((resolve) => { release = resolve; });
  let committed!: () => void;
  const saved = new Promise<void>((resolve) => { committed = resolve; });
  await page.route(`**/api/agents/${agent.id}`, async (route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    const response = await route.fetch();
    expect(response.ok()).toBe(true);
    committed();
    await receipt;
    await route.fulfill({ response });
  });
  try {
    await page.goto("/");
    await enterAccountCredentials(page);
    const row = page.locator(".agent-item", { hasText: agent.name });
    await row.dblclick();
    await page.getByLabel("One-line role").fill("Saved original edit");
    await page.getByRole("button", { name: "Save Agent", exact: true }).click();
    await saved;
    await page.locator(".btn-agent-cancel").click();
    await row.dblclick();
    await expect(page.getByRole("button", { name: "Save Agent", exact: true })).toBeEnabled();
    await page.getByLabel("One-line role").fill("Current unsent edit");
    const received = page.waitForResponse((response) => response.url().endsWith(`/api/agents/${agent.id}`)
      && response.request().method() === "PATCH");
    release();
    await received;
    await expect(page.getByLabel("One-line role")).toHaveValue("Current unsent edit");
    await expect(page.getByRole("button", { name: "Save Agent", exact: true })).toBeEnabled();
    const stored = await request.get(`${agentsUrl}/${agent.id}`, { headers });
    expect(stored.ok()).toBe(true);
    expect((await stored.json()).description).toBe("Saved original edit");
  } finally {
    release();
    await request.post(`${agentsUrl}/${agent.id}/archive`, { headers });
  }
});

test("reopening an Agent cancels its first read and lets a delayed Connector write settle before reversal", async ({ page, request }) => {
  const created = await request.post(agentsUrl, { headers, data: { name: "E2E Connector draft lifetime" } });
  expect(created.ok()).toBe(true);
  const agent = await created.json();
  let releaseRead!: () => void;
  const firstRead = new Promise<void>((resolve) => { releaseRead = resolve; });
  let releaseWrite!: () => void;
  const firstWrite = new Promise<void>((resolve) => { releaseWrite = resolve; });
  let holdOpeningRead = true;
  const openingReads: BrowserRequest[] = [];
  const selected = new Set<string>();
  const pending = new Map<string, { operation_id: string; intent_id: string; enabled: boolean }>();
  let nextId = 0;
  const bindingsPath = `**/api/agents/${agent.id}/connectors`;
  await stubConnectorCatalog(page, ["first", "second"]);
  await page.route(bindingsPath, async (route) => {
    if (holdOpeningRead) {
      const read = route.request();
      openingReads.push(read);
      await firstRead;
      if (!read.failure()) await route.fulfill({ json: { installation_ids: ["first"] } });
      return;
    }
    await route.fulfill({ json: {
      installation_ids: [...selected],
      pending_operations: Object.fromEntries([...pending].map(([id, op]) => [id, op.operation_id])),
    } });
  });
  await page.route(`${bindingsPath}/*/operations`, async (route) => {
    const id = new URL(route.request().url()).pathname.split("/").at(-2)!;
    const { enabled, intent_id } = route.request().postDataJSON();
    const op = { operation_id: `write-${++nextId}`, intent_id, enabled };
    pending.set(id, op);
    await route.fulfill({ json: { operation_id: op.operation_id, reserved: true } });
  });
  await page.route(`${bindingsPath}/first/operations/write-1/settle`, async (route) => {
    expect(pending.get("first")?.operation_id).toBe("write-1");
    pending.delete("first");
    await route.fulfill({ json: { disposition: "retired", enabled: selected.has("first") } });
  });
  await page.route(`${bindingsPath}/*`, async (route) => {
    const id = new URL(route.request().url()).pathname.split("/").at(-1)!;
    const { operation_id: operationId, intent_id: intentId } = route.request().postDataJSON();
    if (operationId === "write-1") await firstWrite;
    const op = pending.get(id);
    if (!op || op.operation_id !== operationId || !op.intent_id || op.intent_id !== intentId) {
      return route.fulfill({ status: 409 });
    }
    if (op.enabled) selected.add(id);
    else selected.delete(id);
    pending.delete(id);
    await route.fulfill({ status: 204 });
  });
  try {
    await page.goto("/");
    await enterAccountCredentials(page);
    const row = page.locator(".agent-item", { hasText: agent.name });
    await row.dblclick();
    const first = page.getByRole("checkbox", { name: /first account/ });
    const second = page.getByRole("checkbox", { name: /second account/ });
    await expect(first).toBeDisabled();
    await expect.poll(() => openingReads.some((read) => !read.failure())).toBe(true);
    // StrictMode can already have cancelled an earlier request for this opening.
    const originalRead = openingReads.findLast((read) => !read.failure());
    expect(originalRead).toBeDefined();
    const [cancelledRead] = await Promise.all([
      page.waitForEvent("requestfailed", { predicate: (read) => read === originalRead, timeout: 5_000 }),
      page.locator(".btn-agent-cancel").click(),
    ]);
    expect(cancelledRead.failure()?.errorText).toContain("ERR_ABORTED");
    holdOpeningRead = false;
    await row.dblclick();
    await expect(first).toBeEnabled();
    await expect(first).not.toBeChecked();
    releaseRead();
    const delayed = page.waitForRequest((item) => item.method() === "PATCH" && item.url().endsWith("/connectors/first"));
    await first.click();
    const originalWrite = await delayed;
    await expect(first).toBeDisabled();
    await expect(second).toBeEnabled();
    await second.click();
    await expect(second).toBeChecked();
    await expect(second).toBeEnabled();
    await page.locator(".btn-agent-cancel").click();
    await row.dblclick();
    await expect(first).toBeDisabled();
    await expect(second).toBeEnabled();
    await page.getByRole("button", { name: "Check update for first account" }).click();
    await expect(first).toBeEnabled();
    await first.click();
    await expect(first).toBeChecked();
    await expect(first).toBeEnabled();
    await first.click();
    await expect(first).not.toBeChecked();
    await expect(first).toBeEnabled();
    const received = page.waitForResponse((response) => response.request() === originalWrite);
    releaseWrite();
    expect((await received).status()).toBe(409);
    await expect(first).not.toBeChecked();
    await expect(first).toBeEnabled();
    await expect(second).toBeChecked();
    expect([...selected]).toEqual(["second"]);
  } finally {
    releaseRead();
    releaseWrite();
  }
});

for (const delayedStage of ["admission", "delivery"] as const) {
  test(`a retired form cannot send a Connector write after late reservation ${delayedStage}`, async ({ page, request }) => {
    const created = await request.post(agentsUrl, { headers, data: { name: "E2E Connector preparation" } });
    expect(created.ok()).toBe(true);
    const agent = await created.json();
    const bindingsPath = `**/api/agents/${agent.id}/connectors`;
    const holdOld = deferred<void>();
    const holdNewWrite = deferred<void>();
    const oldRequested = deferred<void>();
    const newPatchRequested = deferred<void>();
    const oldResolved = deferred<void>();
    const oldCleanup = deferred<void>();
    let preparationCount = 0;
    let pendingId: string | undefined;
    let enabled = false;
    const writes: string[] = [];

    await stubConnectorCatalog(page, ["source"]);
    await page.route(bindingsPath, (route) => route.fulfill({ json: {
      installation_ids: enabled ? ["source"] : [],
      pending_operations: pendingId ? { source: pendingId } : {},
    } }));
    await page.route(`${bindingsPath}/source/operations`, async (route) => {
      const id = `reservation-${++preparationCount}`;
      const original = id === "reservation-1";
      if (original) oldRequested.resolve();
      if (original && delayedStage === "admission") await holdOld.promise;
      const reservation = { operation_id: pendingId ?? id, reserved: pendingId === undefined };
      pendingId ??= id;
      if (original && delayedStage === "delivery") await holdOld.promise;
      await route.fulfill({ json: reservation });
      if (original) oldResolved.resolve();
    });
    await page.route(`${bindingsPath}/source/operations/reservation-1/settle`, async (route) => {
      if (pendingId === "reservation-1") pendingId = undefined;
      else oldCleanup.resolve();
      await route.fulfill({ json: { disposition: "retired", enabled } });
    });
    await page.route(`${bindingsPath}/source`, async (route) => {
      const operationId = route.request().postDataJSON().operation_id;
      writes.push(operationId);
      if (operationId === "reservation-2") {
        newPatchRequested.resolve();
        await holdNewWrite.promise;
      }
      if (pendingId !== operationId) return route.fulfill({ status: 409 });
      enabled = true;
      pendingId = undefined;
      await route.fulfill({ status: 204 });
    });
    try {
      await page.goto("/");
      await enterAccountCredentials(page);
      const row = page.locator(".agent-item", { hasText: agent.name });
      const checkbox = page.getByRole("checkbox", { name: /source account/ });
      await row.dblclick();
      await checkbox.click();
      await oldRequested.promise;
      await page.locator(".btn-agent-cancel").click();
      await row.dblclick();
      if (delayedStage === "delivery") {
        await page.getByRole("button", { name: "Check update for source account" }).click();
      }
      await checkbox.click();
      await newPatchRequested.promise;
      holdOld.resolve();
      await oldResolved.promise;
      if (delayedStage === "delivery") await oldCleanup.promise;
      expect(pendingId).toBe("reservation-2");
      holdNewWrite.resolve();
      await expect(checkbox).toBeEnabled();
      await expect(checkbox).toBeChecked();
      expect(writes).toEqual(["reservation-2"]);
    } finally {
      holdOld.resolve();
      holdNewWrite.resolve();
    }
  });
}

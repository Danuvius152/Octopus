import { enterAccountCredentials } from "./current-account";
import { expect, test } from "./test";

// Exercise navigation and actual backend reports together. Detailed formatting
// and empty-data variations live in MonitorPage.test.tsx.
test("opens Monitor, renders recorded metrics and refetches another window", async ({ page }) => {
  await page.goto("/");
  await enterAccountCredentials(page);
  await expect(page.locator(".agent-list-header h2")).toHaveText("Agents");
  await page.locator("button.btn-manage-monitor").click();
  await expect(page.getByText(/last 24h/)).toBeVisible();
  for (const title of ["activity", "turns", "top errors", "resources"]) {
    await expect(page.getByText(title, { exact: true })).toBeVisible();
  }
  // The HTTP hook timed the requests this page made to load itself.
  await expect(page.locator("tr", { hasText: "http" }).first()).toBeVisible();
  await page.getByText("Recent Codex turns", { exact: true }).click();
  await expect(page.getByText("Recent logical requests (up to 20, within window)", { exact: true })).toBeVisible();

  // Every section owes the reader either data or an explicit empty notice.
  // Other browser cases share the server, so the exact rows can vary.
  const sections = page.locator(".monitor-page section");
  await expect(sections.first()).toBeVisible();
  const count = await sections.count();
  expect(count).toBeGreaterThan(0);
  for (let i = 0; i < count; i++) {
    const section = sections.nth(i);
    const rows = await section.locator("table").count();
    const notice = await section.getByText(/Nothing recorded in this window/).count();
    const heading = await section.locator("h3").textContent();
    expect(rows + notice, `section "${heading?.trim()}" said nothing`).toBeGreaterThan(0);
  }

  // The periodic sampler may not have produced its first reading yet.
  const sidecars = page.locator("tr", { hasText: "mcp_sidecar_count" });
  if (await sidecars.isVisible()) {
    const latest = await sidecars.locator("td").last().textContent();
    expect(Number(latest?.trim())).toBe(0);
  }

  const refetched = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/monitor/overview" && url.searchParams.get("window") === "7d";
  });
  await page.getByRole("button", { name: "7d", exact: true }).click();
  expect((await refetched).ok()).toBe(true);
  await expect(page.getByText(/last 7d/)).toBeVisible();
  await expect(sections.first()).toBeVisible();
});

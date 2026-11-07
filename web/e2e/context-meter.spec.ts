import { expect, test, type WebSocketRoute } from "./test";
import { enterAccountCredentials } from "./current-account";
import { contextState } from "../src/lib/sessionContext.fixture";
import type { ContextState } from "../src/lib/sessionContext";

for (const [width, colorScheme] of [[1280, "light"], [390, "dark"]] as const) {
  test.describe(`Context meter ${width}px ${colorScheme}`, () => {
    test.use({ viewport: { width, height: 844 }, colorScheme, isMobile: width < 768, hasTouch: width < 768 });

    test("shows remaining beside Attach file, readable details and persistent stale state", async ({ page }) => {
      test.setTimeout(15_000);
      const session = { id: "context-browser", agent_id: "context-agent", name: "Context observation",
        backend: "codex", status: "idle", archived: false, working_dir: "/tmp", created_at: "2026-11-07T00:00:00Z" };
      const dispatch = { generation: "context-service", revision: 1, paused: false,
        active_request_id: null, active_operation: null, items: [] };
      let state: ContextState = contextState();
      state.sample!.used_tokens = 700;
      let socket: WebSocketRoute;
      const writes: string[] = [];
      await page.route("**/api/**", async (route) => {
        const path = new URL(route.request().url()).pathname;
        if (!path.startsWith("/api/")) { await route.continue(); return; }
        if (!["GET", "HEAD"].includes(route.request().method()) && path !== "/api/auth/login") {
          writes.push(path);
          await route.fulfill({ status: 409, json: { detail: "Read-only context observation" } });
          return;
        }
        let data: unknown = [];
        if (path === "/api/auth/state") data = { accounts_exist: true };
        else if (path === "/api/auth/login") data = { token: "browser-token" };
        else if (path === "/api/auth/identity") data = { user_id: "context-user", label: "Operator", is_admin: false };
        else if (path === "/api/agents") data = [{ id: session.agent_id, name: "Octo", backend: "codex", pinned: true, is_system: true }];
        else if (path === "/api/backends") data = { available: ["codex"], default: "codex", models: { codex: [] } };
        else if (path === "/api/sessions") data = [session];
        else if (path === `/api/sessions/${session.id}`) data = { ...session, messages: [],
          active_attempt: null, pending_questions: [], subagents: [], next_message_seq: 0,
          oldest_loaded_seq: null, has_more_messages: false, dispatch_state: dispatch,
          context_generation: dispatch.generation, context_state: state };
        await route.fulfill({ json: data });
      });
      await page.routeWebSocket("**/ws?*", (ws) => { socket = ws; });
      const open = async () => {
        if (width < 768) await page.getByRole("button", { name: "Toggle sidebar", exact: true }).click();
        await page.locator(".agent-item", { hasText: "Octo" }).click();
        await page.locator(".session-item", { hasText: session.name }).click();
      };
      await page.goto("/");
      await expect(page.locator("#username")).toBeVisible({ timeout: 5_000 });
      await enterAccountCredentials(page);
      await open();
      const meter = page.getByRole("button", { name: "Context remaining: 30%", exact: true });
      await expect(meter).toBeVisible();
      await expect(meter).toHaveAttribute("data-tone", "amber");
      const attach = await page.getByRole("button", { name: "Attach file", exact: true }).boundingBox();
      const ring = await meter.boundingBox();
      expect(ring!.x - (attach!.x + attach!.width)).toBeGreaterThanOrEqual(0);
      expect(ring!.x - (attach!.x + attach!.width)).toBeLessThan(16);
      const contrast = await meter.evaluate((element) => {
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 1;
        const context = canvas.getContext("2d")!;
        const luminance = (color: string) => {
          context.fillStyle = color;
          context.fillRect(0, 0, 1, 1);
          const values = [...context.getImageData(0, 0, 1, 1).data].slice(0, 3).map((value) => {
            const channel = value / 255;
            return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4;
          });
          return .2126 * values[0] + .7152 * values[1] + .0722 * values[2];
        };
        const foreground = luminance(getComputedStyle(element.querySelector("span")!).color);
        const background = luminance(getComputedStyle(document.querySelector(".chat-input-bar")!).backgroundColor);
        return (Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05);
      });
      expect(contrast).toBeGreaterThanOrEqual(4.5);
      if (width < 768) await meter.tap();
      else { await meter.focus(); await meter.press("Enter"); }
      const details = page.getByRole("dialog", { name: "Context details" });
      await expect(details).toBeVisible();
      await expect(details).toContainText("700 / 1,000 tokens");
      await expect(details.getByRole("button", { name: /compact/i })).toHaveCount(0);
      const box = await details.boundingBox();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(width);
      await page.keyboard.press("Escape");
      await expect(details).not.toBeVisible();
      if (width >= 768) await expect(meter).toBeFocused();
      state = { ...state, revision: 2, validity: "stale",
        invalidation: { reason: "compacted", observed_at: "2026-11-07T08:00:00Z" } };
      socket!.send(JSON.stringify({ type: "context_state", session_id: session.id,
        generation: dispatch.generation, context_state: state }));
      await expect(page.getByRole("button", { name: "Context remaining: —", exact: true })).toContainText("Awaiting update");
      await page.reload();
      await open();
      await expect(page.getByRole("button", { name: "Context remaining: —", exact: true })).toContainText("Awaiting update");
      expect(writes).toEqual([]);
    });
  });
}

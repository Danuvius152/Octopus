import type { Route } from "@playwright/test";
import { browserTest, expect, type Page, type WebSocketRoute } from "./test";
import { OFFLINE_VITE_HMR_PATH } from "./vite-hmr";

export const mockChatTest = browserTest.extend<{ _mockTraffic: void }>({
  _mockTraffic: [async ({ context, baseURL, _offlineNetwork }, use) => {
    void _offlineNetwork;
    if (process.env.OCTOPUS_E2E_MODE !== "offline" || !baseURL) throw new Error("Mock chat requires offline E2E");
    const devServer = new URL(baseURL);
    await context.route((url) => url.pathname === "/api" || url.pathname.startsWith("/api/"), async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      expect.soft(false, `Unexpected mock HTTP request: ${request.method()} ${path}`).toBe(true);
      await route.abort("blockedbyclient");
    });
    await context.routeWebSocket((url) => url.origin !== socketOrigin(devServer) || url.pathname !== OFFLINE_VITE_HMR_PATH, async (socket) => {
      expect.soft(false, `Unexpected mock WebSocket: ${new URL(socket.url()).pathname}`).toBe(true);
      await socket.close({ code: 1008 });
    });
    await use();
  }, { auto: true }],
});

function mockOrigin() {
  const { baseURL } = mockChatTest.info().project.use;
  if (!baseURL) throw new Error("Mock chat requires a project baseURL");
  return new URL(baseURL);
}

function socketOrigin(url: URL) {
  return url.origin.replace(/^http/, "ws");
}

export function mockChatHttp(page: Page, matches: (url: URL) => boolean, handler: (route: Route) => Promise<void>) {
  const { origin } = mockOrigin();
  return page.route((url) => url.origin === origin && matches(url), handler);
}

export function mockChatGet(page: Page, path: string, handler: (route: Route) => Promise<void>) {
  return mockChatHttp(page, (url) => url.pathname + url.search === path, (route) =>
    route.request().method() === "GET" ? handler(route) : route.fallback());
}

export function isEmptyChatCollection(path: string, sessionIds: readonly string[]): boolean {
  return ["/api/applications", "/api/connectors", "/api/credentials", "/api/schedules"].includes(path) ||
    sessionIds.some((id) => ["bg-task-summaries", "delegations", "research", "wiki-jobs"]
      .some((collection) => path === `/api/sessions/${id}/${collection}`));
}

export interface MockSubmission { content: string; client_request_id: string }

export async function mockChatSocket(
  page: Page, token: string, connected: (socket: WebSocketRoute) => void,
  submit?: (message: MockSubmission) => void,
) {
  const origin = socketOrigin(mockOrigin());
  await page.routeWebSocket((url) => url.origin === origin && url.pathname === "/ws" &&
    url.search === `?token=${encodeURIComponent(token)}`, (socket) => {
    connected(socket);
    socket.onMessage(async (payload) => {
      let message: Record<string, unknown> | null = null;
      try { message = JSON.parse(String(payload)); } catch { /* Invalid payloads fail below. */ }
      if (submit && message?.type === "send_message" &&
          typeof message.content === "string" && typeof message.client_request_id === "string") {
        submit({ content: message.content, client_request_id: message.client_request_id });
      } else {
        expect.soft(false, `Unexpected mock WebSocket message: ${String(message?.type ?? "<invalid>")}`).toBe(true);
        await socket.close({ code: 1008 });
      }
    });
  });
}

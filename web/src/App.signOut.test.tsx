import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type {} from "vitest/jsdom";
import App from "./App";
import { mockChatViewport } from "./components/chatViewport.test-support";
import { useSessionStore } from "./stores/sessionStore";
import { deferred } from "./test-support/deferred";

vi.mock("./hooks/useWebSocket", () => ({ useWebSocket: () => ({}) }));

const navigate = vi.fn();
function observeNavigation(error: Error) {
  // JSDOM reports full-document navigation instead of replacing the page.
  if (error.message === "Not implemented: navigation to another Document") navigate();
}

beforeEach(() => {
  mockChatViewport();
  navigate.mockClear();
  jsdom.virtualConsole.on("jsdomError", observeNavigation);
  useSessionStore.getState().setToken("old-token");
});

afterEach(() => {
  cleanup();
  useSessionStore.getState().setToken("");
  jsdom.virtualConsole.off("jsdomError", observeNavigation);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function clickSignOut() {
  fireEvent.keyDown(screen.getByRole("button", { name: "Account menu" }), { key: "Enter" });
  fireEvent.click(await screen.findByRole("menuitem", { name: "Sign out" }));
}

type LogoutResult = "success" | "network error";
async function completeLogout(logout: ReturnType<typeof deferred<Response>>, result: LogoutResult) {
  await act(async () => {
    if (result === "success") logout.resolve(new Response(null, { status: 204 }));
    else logout.reject(new TypeError("Network failure"));
  });
}

it.each([
  ["new-token", "success"],
  ["new-token", "network error"],
  ["old-token", "success"],
  ["old-token", "network error"],
] as const)("preserves the new login and draft for %s after the old logout returns %s", async (nextToken, result) => {
  const oldLogout = deferred<Response>();
  let revoked = false;
  let signedIn = false;
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const { pathname } = new URL(String(input), window.location.origin);
    if (pathname === "/api/auth/logout") {
      revoked = true;
      return oldLogout.promise;
    }
    if (pathname === "/api/auth/identity") {
      const bearer = new Headers(init?.headers).get("Authorization");
      if (bearer === "Bearer old-token" && revoked && !signedIn) return new Response(null, { status: 401 });
      return Response.json({
        user_id: signedIn ? "new-user" : "old-user",
        label: signedIn ? "New account" : "Original account",
        is_admin: false,
      });
    }
    if (pathname === "/api/auth/state") return Response.json({ accounts_exist: true });
    if (pathname === "/api/auth/login") {
      signedIn = true;
      return Response.json({ token: nextToken });
    }
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<App />);
  await screen.findByText("Original account");
  await clickSignOut();
  expect(fetcher).toHaveBeenCalledWith(`${window.location.origin}/api/auth/logout`, {
    method: "POST",
    headers: { Authorization: "Bearer old-token" },
    keepalive: true,
  });

  fireEvent.focus(window);
  fireEvent.change(await screen.findByLabelText("Username"), { target: { value: "reader" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await screen.findByText("New account");

  act(() => useSessionStore.getState().setActiveSessionId("new-chat"));
  fireEvent.change(screen.getByPlaceholderText("Send a message…"), { target: { value: "New draft" } });
  expect(screen.getByPlaceholderText("Send a message…")).toHaveValue("New draft");

  await completeLogout(oldLogout, result);

  expect(screen.getByText("New account")).toBeVisible();
  expect(screen.getByPlaceholderText("Send a message…")).toHaveValue("New draft");
  expect(navigate).not.toHaveBeenCalled();
});

it.each(["success", "network error"] as const)("signs out the current login after logout returns %s", async (result) => {
  const logout = deferred<Response>();
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    const { pathname } = new URL(String(input), window.location.origin);
    if (pathname === "/api/auth/logout") return logout.promise;
    if (pathname === "/api/auth/state") return Response.json({ accounts_exist: true });
    if (pathname === "/api/auth/identity") {
      return Response.json({ user_id: "old-user", label: "Original account", is_admin: false });
    }
    return Response.json([]);
  });
  render(<App />);
  await screen.findByText("Original account");
  await clickSignOut();
  expect(screen.getByText("Original account")).toBeVisible();
  expect(navigate).not.toHaveBeenCalled();

  await completeLogout(logout, result);

  expect(await screen.findByLabelText("Username")).toBeVisible();
  expect(localStorage.getItem("octopus_token")).toBe("");
  expect(navigate).toHaveBeenCalledTimes(1);
});

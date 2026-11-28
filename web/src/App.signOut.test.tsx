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
  vi.useRealTimers();
  useSessionStore.getState().setToken("");
  jsdom.virtualConsole.off("jsdomError", observeNavigation);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function beginSignOut() {
  fireEvent.keyDown(screen.getByRole("button", { name: "Account menu" }), { key: "Enter" });
  const signOut = await screen.findByRole("menuitem", { name: "Sign out" });
  vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
  fireEvent.click(signOut);
}

type LogoutResult = "success" | "network error" | "server error";
async function completeLogout(logout: ReturnType<typeof deferred<Response>>, result: LogoutResult) {
  await act(async () => {
    if (result === "network error") logout.reject(new TypeError("Network failure"));
    else logout.resolve(new Response(null, { status: result === "success" ? 204 : 503 }));
  });
}

it.each([
  ["new-token", "success"],
  ["new-token", "network error"],
  ["new-token", "timeout"],
  ["old-token", "success"],
  ["old-token", "network error"],
  ["old-token", "timeout"],
] as const)("preserves the new login and draft for %s when the old logout ends with %s", async (nextToken, result) => {
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
  await beginSignOut();

  await act(async () => { fireEvent.focus(window); });
  fireEvent.change(screen.getByLabelText("Username"), { target: { value: "reader" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret" } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Sign in" })); });
  expect(screen.getByText("New account")).toBeVisible();

  act(() => useSessionStore.getState().setActiveSessionId("new-chat"));
  fireEvent.change(screen.getByPlaceholderText("Send a message…"), { target: { value: "New draft" } });
  expect(screen.getByPlaceholderText("Send a message…")).toHaveValue("New draft");

  if (result === "timeout") {
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(screen.getByText("New account")).toBeVisible();
    expect(screen.getByPlaceholderText("Send a message…")).toHaveValue("New draft");
    await completeLogout(oldLogout, "network error");
  } else {
    await completeLogout(oldLogout, result);
  }

  expect(screen.getByText("New account")).toBeVisible();
  expect(screen.getByPlaceholderText("Send a message…")).toHaveValue("New draft");
  expect(localStorage.getItem("octopus_token")).toBe(nextToken);
  expect(navigate).not.toHaveBeenCalled();
});

it.each(["success", "network error", "server error", "timeout"] as const)("signs out the current login on %s", async (result) => {
  const logout = deferred<Response>();
  const fetcher = vi.fn<typeof globalThis.fetch>(async (input) => {
    const { pathname } = new URL(String(input), window.location.origin);
    if (pathname === "/api/auth/logout") return logout.promise;
    if (pathname === "/api/auth/state") return Response.json({ accounts_exist: true });
    if (pathname === "/api/auth/identity") {
      return Response.json({ user_id: "old-user", label: "Original account", is_admin: false });
    }
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<App />);
  await screen.findByText("Original account");
  await beginSignOut();
  expect(screen.getByText("Original account")).toBeVisible();
  expect(navigate).not.toHaveBeenCalled();

  if (result === "timeout") {
    await act(() => vi.advanceTimersByTimeAsync(29_999));
    expect(screen.getByText("Original account")).toBeVisible();
    await act(() => vi.advanceTimersByTimeAsync(1));
  } else {
    await completeLogout(logout, result);
  }

  expect(screen.getByLabelText("Username")).toBeVisible();
  expect(localStorage.getItem("octopus_token")).toBe("");
  if (result === "timeout") await completeLogout(logout, "success");
  expect(navigate).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith("/api/auth/logout"))).toEqual([[
    `${window.location.origin}/api/auth/logout`,
    { method: "POST", headers: { Authorization: "Bearer old-token" }, keepalive: true },
  ]]);
});

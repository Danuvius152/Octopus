import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { HarnessPage } from "./HarnessPage";
import { useSessionStore, type CredentialInfo } from "../stores/sessionStore";

beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.setState({ token: "account-token" });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it("finishes Codex sign-in when a retry returns the credential after the first success response was lost", async () => {
  vi.useFakeTimers();
  const credential: CredentialInfo = {
    id: "saved-credential", backend: "codex", label: "Recovered account", auth_type: "oauth",
    created_at: "2026-10-25T00:00:00Z", status: "active", needs_reconnect: false,
  };
  let statusReads = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/codex/start")) return Response.json({ login_id: "login" });
    if (path.endsWith("/codex/login/status")) {
      statusReads++;
      if (statusReads === 1) throw new TypeError("Success response lost after credential was saved");
      return Response.json({ state: "success", credential });
    }
    return Response.json([]);
  }));
  await act(async () => { render(<HarnessPage onToggleSidebar={() => {}} />); });
  fireEvent.click(screen.getByText("Sign in to Claude Code or Codex"));
  fireEvent.click(screen.getByRole("button", { name: /Codex.*Sign in with ChatGPT/ }));
  fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Recovered account" } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Continue" })); });
  expect(screen.getByRole("dialog")).toBeTruthy();
  await act(async () => vi.advanceTimersByTimeAsync(600));
  expect(statusReads).toBe(1);
  expect(screen.getByRole("dialog")).toBeTruthy();
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  expect(statusReads).toBe(2);
  expect(useSessionStore.getState().credentials).toEqual([credential]);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByText("Recovered account")).toBeTruthy();
  await act(async () => vi.advanceTimersByTimeAsync(10000));
  expect(statusReads).toBe(2);
});

async function enterClaudeCode() {
  await act(async () => { render(<HarnessPage onToggleSidebar={() => {}} />); });
  fireEvent.click(screen.getByText("Sign in to Claude Code or Codex"));
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: /Claude Code.*Sign in with Anthropic/ })); });
  fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Personal" } });
  fireEvent.change(screen.getByLabelText("Code from browser"), { target: { value: "original-code" } });
}

it("waits again with the original Claude request and confirms current credentials instead of restoring a deleted result", async () => {
  const requests: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/oauth/start")) return Response.json({ login_id: "original-login", device_url: "https://example.test/login" });
    if (path.endsWith("/oauth/complete")) {
      requests.push(JSON.parse(String(init?.body)));
      if (requests.length === 1) throw new TypeError("Response lost after commit");
      return Response.json({ id: "deleted-credential", backend: "claude-code", label: "Personal", auth_type: "oauth", created_at: "now" });
    }
    return Response.json([]);
  }));
  await enterClaudeCode();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Finish sign-in" })); });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Wait again" })); });
  expect(requests).toEqual([
    { login_id: "original-login", code: "original-code", label: "Personal", credential_id: null },
    { login_id: "original-login", code: "original-code", label: "Personal", credential_id: null },
  ]);
  expect(useSessionStore.getState().credentials).toEqual([]);
  expect(screen.getByText(/no longer in your credential list/)).toBeTruthy();
});

it.each(["cancelled", "already_completed", "unconfirmed"])("consumes the explicit Claude cancellation result: %s", async (status) => {
  const credential: CredentialInfo = { id: "committed", backend: "claude-code", label: "Personal", auth_type: "oauth", created_at: "now", status: "active", needs_reconnect: false };
  let committed = false;
  const cancelled: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/oauth/start")) return Response.json({ login_id: "original-login", device_url: "https://example.test/login" });
    if (path.endsWith("/oauth/cancel")) {
      cancelled.push(JSON.parse(String(init?.body)));
      committed = status === "already_completed";
      return Response.json({ status });
    }
    return Response.json(committed ? [credential] : []);
  }));
  await enterClaudeCode();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Cancel sign-in" })); });
  expect(cancelled).toEqual([{ login_id: "original-login" }]);
  if (status === "unconfirmed") {
    expect(screen.getByText(/cancellation was not confirmed/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Cancel sign-in" })).toBeTruthy();
  } else {
    expect(screen.getByRole("status").textContent).toContain(status === "cancelled" ? "Sign-in cancelled." : "Sign-in already completed.");
  }
  expect(useSessionStore.getState().credentials).toEqual(committed ? [credential] : []);
});

it.each(["complete", "cancel"])("closing only ends Claude observation and a late %s cannot replace the new dialog", async (operation) => {
  let finish!: (response: Response) => void;
  let cancellations = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/oauth/start")) return Response.json({ login_id: "original-login", device_url: "https://example.test/login" });
    if (path.endsWith("/oauth/cancel")) cancellations++;
    if (path.endsWith(`/oauth/${operation}`)) return new Promise<Response>((resolve) => { finish = resolve; });
    return Response.json([]);
  }));
  await enterClaudeCode();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: operation === "complete" ? "Finish sign-in" : "Cancel sign-in" })); });
  fireEvent.click(screen.getAllByRole("button", { name: "Close" })[0]);
  fireEvent.click(screen.getByText("Sign in to Claude Code or Codex"));
  await act(async () => { finish(Response.json(operation === "cancel" ? { status: "cancelled" } : { id: "historical" })); });
  expect(screen.getByRole("heading", { name: "Add a credential" })).toBeTruthy();
  expect(useSessionStore.getState().credentials).toEqual([]);
  expect(cancellations).toBe(operation === "cancel" ? 1 : 0);
});

it("does not show a completion error from a retired account", async () => {
  let fail!: (reason: Error) => void;
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    if (String(url).endsWith("/oauth/start")) return Response.json({ login_id: "original-login", device_url: "https://example.test/login" });
    if (String(url).endsWith("/oauth/complete")) return new Promise<Response>((_, reject) => { fail = reject; });
    return Response.json([]);
  }));
  await enterClaudeCode();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Finish sign-in" })); });
  await act(async () => { useSessionStore.getState().setToken("different-account"); fail(new TypeError("Old account failure")); });
  expect(screen.queryByText(/Old account failure/)).toBeNull();
  expect(useSessionStore.getState().credentials).toEqual([]);
});

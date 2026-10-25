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

import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { HarnessPage } from "./HarnessPage";
import { useSessionStore, type CredentialInfo } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";
import { modelCredentials } from "../lib/modelCredentials";
import { handleWsMessage } from "../hooks/useWebSocket";

beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.setState({ token: "account-token" });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it.each([false, true])("keeps both independent deletions and confirms unrelated new credentials (reverse=%s)", async (reverse) => {
  const rows: CredentialInfo[] = ["First", "Second", "New"].map((label) => ({
    id: label.toLowerCase(), label, backend: "codex", auth_type: "oauth", created_at: "now", status: "active", needs_reconnect: false,
  }));
  const replies = [deferred<Response>(), deferred<Response>()];
  const deleted = new Set<string>();
  let initial = true;
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (init?.method === "DELETE") return replies[path.endsWith("/first") ? 0 : 1].promise;
    if (initial) { initial = false; return Response.json(rows.slice(0, 2)); }
    return Response.json(rows.filter((row) => !deleted.has(row.id)));
  }));
  await act(async () => { render(<HarnessPage onToggleSidebar={() => {}} />); });
  fireEvent.click(screen.getByRole("button", { name: "Delete First" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete Second" }));
  for (const index of reverse ? [1, 0] : [0, 1]) {
    await act(async () => { deleted.add(rows[index].id); replies[index].resolve(Response.json({ deleted: true })); });
  }
  expect(screen.queryByText("First", { exact: true })).not.toBeInTheDocument();
  expect(screen.queryByText("Second", { exact: true })).not.toBeInTheDocument();
  expect(screen.getByText("New", { exact: true })).toBeVisible();
  expect(useSessionStore.getState().credentials.map((row) => row.id)).toEqual(["new"]);
});

it.each(["pending", "success", "error", "cancelled"].flatMap((state) =>
  [false, true].map((accountSwitch) => ({ state, accountSwitch }))))(
  "a retired Codex $state body cannot affect the new view after headers arrived (account switch=$accountSwitch)", async ({ state, accountSwitch }) => {
  vi.useFakeTimers();
  let finish!: (body: unknown) => void;
  let statusReads = 0, listReads = 0;
  const response = new Response(new ReadableStream({ start(controller) {
    finish = (body) => { controller.enqueue(new TextEncoder().encode(JSON.stringify(body))); controller.close(); };
  } }), { headers: { "Content-Type": "application/json" } });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/codex/start")) return Response.json({ login_id: "old-login" });
    if (path.endsWith("/codex/old-login/status")) { statusReads++; return response; }
    if (path.endsWith("/credentials")) listReads++;
    return Response.json([]);
  }));
  await act(async () => { render(<HarnessPage onToggleSidebar={() => {}} />); });
  fireEvent.click(screen.getByText("Sign in to Claude Code or Codex"));
  fireEvent.click(screen.getByRole("button", { name: /Codex.*Sign in with ChatGPT/ }));
  fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Old login" } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Continue" })); });
  await act(async () => vi.advanceTimersByTimeAsync(600));
  expect(statusReads).toBe(1);
  fireEvent.click(screen.getAllByRole("button", { name: "Close" })[0]);
  if (accountSwitch) await act(async () => {
    useSessionStore.getState().setToken("another-account");
    useSessionStore.getState().setToken("account-token");
  });
  fireEvent.click(screen.getByText("Sign in to Claude Code or Codex"));
  const before = listReads;
  await act(async () => { finish({ state, verification_url: "https://old.example.test", user_code: "OLD-CODE", message: "Old failure", credential: null }); });
  expect(screen.getByRole("heading", { name: "Add a credential" })).toBeVisible();
  expect(screen.getByRole("button", { name: /Codex.*Sign in with ChatGPT/ })).toBeVisible();
  expect(screen.queryByText(/OLD-CODE|Old failure/)).not.toBeInTheDocument();
  expect(listReads).toBe(before);
  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  expect(statusReads).toBe(1);
});

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
    if (path.endsWith("/credentials") && statusReads >= 2) return Response.json([credential]);
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

it.each([null, { id: "deleted", backend: "codex", label: "Old result", auth_type: "oauth", created_at: "now", status: "active", needs_reconnect: false }])(
  "ends Codex observation on historical success and uses the current list: %s", async (credential) => {
    vi.useFakeTimers();
    let statusReads = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
      const path = String(url);
      if (path.endsWith("/codex/start")) return Response.json({ login_id: "login" });
      if (path.endsWith("/codex/login/status")) {
        statusReads++;
        return Response.json({ state: "success", credential });
      }
      return Response.json([]);
    }));
    await act(async () => { render(<HarnessPage onToggleSidebar={() => {}} />); });
    fireEvent.click(screen.getByText("Sign in to Claude Code or Codex"));
    fireEvent.click(screen.getByRole("button", { name: /Codex.*Sign in with ChatGPT/ }));
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Login" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Continue" })); });
    await act(async () => vi.advanceTimersByTimeAsync(600));
    expect(screen.getByRole("status")).toHaveTextContent("no longer in your credential list");
    expect(useSessionStore.getState().credentials).toEqual([]);
    await act(async () => vi.advanceTimersByTimeAsync(10000));
    expect(statusReads).toBe(1);
  },
);

it("does not publish an old account's credential reread after Codex historical success", async () => {
  vi.useFakeTimers();
  let succeeded = false;
  let finishList!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/codex/start")) return Response.json({ login_id: "login" });
    if (path.endsWith("/codex/login/status")) {
      succeeded = true;
      return Response.json({ state: "success", credential: null });
    }
    const authorization = (init?.headers as Record<string, string> | undefined)?.Authorization;
    if (path.endsWith("/credentials") && succeeded && authorization === "Bearer account-token") {
      return new Promise<Response>((resolve) => { finishList = resolve; });
    }
    return Response.json([]);
  }));
  await act(async () => { render(<HarnessPage onToggleSidebar={() => {}} />); });
  fireEvent.click(screen.getByText("Sign in to Claude Code or Codex"));
  fireEvent.click(screen.getByRole("button", { name: /Codex.*Sign in with ChatGPT/ }));
  fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Old account" } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Continue" })); });
  await act(async () => vi.advanceTimersByTimeAsync(600));
  expect(finishList).toBeTypeOf("function");
  await act(async () => { useSessionStore.getState().setToken("new-account-token"); });
  await act(async () => { finishList(Response.json([{ id: "old", backend: "codex", label: "Old account credential", auth_type: "oauth", created_at: "now", status: "active", needs_reconnect: false }])); });
  expect(useSessionStore.getState().credentials).toEqual([]);
  expect(screen.queryByText("Old account credential")).toBeNull();
});

async function enterClaudeCode() {
  await act(async () => { render(<HarnessPage onToggleSidebar={() => {}} />); });
  fireEvent.click(screen.getByText("Sign in to Claude Code or Codex"));
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: /Claude Code.*Sign in with Anthropic/ })); });
  fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Personal" } });
  fireEvent.change(screen.getByLabelText("Code from browser"), { target: { value: "original-code" } });
}

it.each(["before", "during"])("a GET begun %s DELETE cannot restore a removed credential", async (when) => {
  const old: CredentialInfo = { id: "old", label: "Old", backend: "codex", auth_type: "oauth", created_at: "now", status: "active", needs_reconnect: false };
  const added = { ...old, id: "new", label: "New" };
  const deletion = deferred<Response>();
  let release!: () => void;
  const stale = new Response(new ReadableStream({ start(controller) {
    release = () => { controller.enqueue(new TextEncoder().encode(JSON.stringify([old]))); controller.close(); };
  } }));
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "DELETE") return deletion.promise;
    reads++;
    return reads === 1 ? Response.json([old]) : reads === 2 ? stale : Response.json([added]);
  }));
  await act(async () => { render(<HarnessPage onToggleSidebar={() => {}} />); });
  if (when === "before") await act(async () => { void modelCredentials.refresh(); });
  fireEvent.click(screen.getByRole("button", { name: "Delete Old" }));
  if (when === "during") await act(async () => { void modelCredentials.refresh(); });
  await act(async () => { deletion.resolve(Response.json({ deleted: true })); });
  expect(screen.queryByText("Old", { exact: true })).not.toBeInTheDocument();
  expect(screen.getByText("New", { exact: true })).toBeVisible();
  await act(async () => { release(); });
  expect(useSessionStore.getState().credentials).toEqual([added]);
});

it.each(["result", "error"])("%s auth_expired replaces a pre-event GET even after its headers arrived", async (type) => {
  const row: CredentialInfo = { id: "credential", label: "Saved", backend: "codex", auth_type: "oauth", created_at: "now", status: "active", needs_reconnect: false };
  let release!: () => void;
  const stale = new Response(new ReadableStream({ start(controller) {
    release = () => { controller.enqueue(new TextEncoder().encode(JSON.stringify([row]))); controller.close(); };
  } }));
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async () => ++reads === 1 ? stale : Response.json([{ ...row, needs_reconnect: true }])));
  await act(async () => { render(<HarnessPage onToggleSidebar={() => {}} />); });
  await act(async () => { handleWsMessage({ type, session_id: "session", code: "auth_expired", message: "Expired" }); });
  expect(screen.getByText("token expired")).toBeVisible();
  await act(async () => { release(); });
  expect(screen.getByRole("button", { name: "Re-authorize →" })).toBeVisible();
});

it("a confirmed service generation retires an older credential body and confirms the current list", async () => {
  const row: CredentialInfo = { id: "saved", label: "Saved", backend: "codex", auth_type: "oauth", created_at: "now", status: "active", needs_reconnect: false };
  let release!: () => void;
  const stale = new Response(new ReadableStream({ start(controller) {
    release = () => { controller.enqueue(new TextEncoder().encode(JSON.stringify([row]))); controller.close(); };
  } }));
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async () => ++reads === 1 ? stale : Response.json([{ ...row, needs_reconnect: true }])));
  await act(async () => { render(<HarnessPage onToggleSidebar={() => {}} />); });
  await act(async () => { handleWsMessage({ type: "dispatch_state", session_id: "session", dispatch_state: {
    generation: "new-service", revision: 1, paused: false, active_request_id: null, active_operation: null, items: [],
  } }); });
  await act(async () => { release(); });
  expect(screen.getByText("token expired")).toBeVisible();
  expect(useSessionStore.getState().credentialRead).toMatchObject({ loading: false, stale: false });
});

it("shared reads survive page unmount and old-account cleanup cannot retire the new A after A→B→A", async () => {
  const old = deferred<Response>(), latest = deferred<Response>();
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(() => ++reads === 1 ? old.promise : latest.promise));
  const page = render(<HarnessPage onToggleSidebar={() => {}} />);
  await act(async () => { useSessionStore.getState().setToken("B"); useSessionStore.getState().setToken("account-token"); });
  page.unmount();
  await act(async () => { old.resolve(Response.json([{ id: "retired" }])); });
  expect(useSessionStore.getState().credentialRead.loading).toBe(true);
  expect(useSessionStore.getState().credentials).toEqual([]);
  const row = { id: "current", label: "Current", backend: "codex", auth_type: "oauth", created_at: "now", status: "active", needs_reconnect: false };
  await act(async () => { latest.resolve(Response.json([row])); });
  expect(useSessionStore.getState().credentials).toEqual([row]);
  expect(useSessionStore.getState().credentialRead).toMatchObject({ loading: false, stale: false });
});

it("a list body deadline preserves known credentials and exposes a read-only retry", async () => {
  vi.useFakeTimers();
  const row: CredentialInfo = { id: "saved", label: "Saved", backend: "codex", auth_type: "oauth", created_at: "now", status: "active", needs_reconnect: false };
  useSessionStore.setState({ credentials: [row] });
  let release!: () => void;
  const delayed = new Response(new ReadableStream({ start(controller) {
    release = () => { controller.enqueue(new TextEncoder().encode("[]")); controller.close(); };
  } }));
  let reads = 0;
  const fetcher = vi.fn(async () => ++reads === 1 ? delayed : Response.json([row]));
  vi.stubGlobal("fetch", fetcher);
  await act(async () => { render(<HarnessPage onToggleSidebar={() => {}} />); });
  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  expect(screen.getByRole("alert")).toHaveTextContent("timed out");
  expect(screen.getByText("Saved", { exact: true })).toBeVisible();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Refresh credential list" })); });
  await act(async () => { release(); });
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(useSessionStore.getState().credentials).toEqual([row]);
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it.each(["claude-code", "codex", "already_completed"])("%s keeps completed sign-in on list failure and retries only the list", async (backend) => {
  vi.useFakeTimers();
  const row: CredentialInfo = { id: "saved", label: "Personal", backend: backend === "codex" ? "codex" : "claude-code", auth_type: "oauth", created_at: "now", status: "active", needs_reconnect: false };
  let completed = false, fail = true, writes = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (init?.method === "POST") writes++;
    if (path.endsWith("/oauth/start")) return Response.json({ login_id: "login", device_url: "https://example.test" });
    if (path.endsWith("/codex/start")) return Response.json({ login_id: "login" });
    if (path.endsWith("/oauth/complete") || path.endsWith("/codex/login/status")) {
      completed = true;
      return Response.json(backend === "codex" ? { state: "success", credential: row } : row);
    }
    if (path.endsWith("/oauth/cancel")) { completed = true; return Response.json({ status: "already_completed" }); }
    if (completed && fail) return Response.json({ detail: "Unavailable" }, { status: 503 });
    return Response.json(completed ? [row] : []);
  }));
  if (backend === "codex") {
    await act(async () => { render(<HarnessPage onToggleSidebar={() => {}} />); });
    fireEvent.click(screen.getByRole("button", { name: "+ Connect engine" }));
    fireEvent.click(screen.getByRole("button", { name: /Codex.*Sign in with ChatGPT/ }));
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Personal" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Continue" })); });
    await act(async () => vi.advanceTimersByTimeAsync(600));
  } else {
    await enterClaudeCode();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: backend === "already_completed" ? "Cancel sign-in" : "Finish sign-in" })); });
  }
  expect(screen.getByRole("status")).toHaveTextContent("Sign-in completed; credential list is not synchronized.");
  const originalWrites = writes;
  fail = false;
  await act(async () => { fireEvent.click(screen.getAllByRole("button", { name: "Refresh credential list" }).at(-1)!); });
  expect(useSessionStore.getState().credentials).toEqual([row]);
  expect(writes).toBe(originalWrites);
  if (backend === "already_completed") expect(screen.getByRole("status")).toHaveTextContent("Sign-in already completed.");
  else expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("a delayed sign-in list confirmation cannot close a later dialog or restore its deleted result", async () => {
  const historical = { id: "historical", label: "Historical", backend: "claude-code", auth_type: "oauth", created_at: "now", status: "active", needs_reconnect: false };
  const confirmation = deferred<Response>();
  let completed = false;
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/oauth/start")) return Response.json({ login_id: "login", device_url: "https://example.test" });
    if (path.endsWith("/oauth/complete")) { completed = true; return Response.json(historical); }
    return completed ? confirmation.promise : Response.json([]);
  }));
  await enterClaudeCode();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Finish sign-in" })); });
  expect(screen.getByRole("status")).toHaveTextContent("Confirming the credential list");
  fireEvent.click(screen.getAllByRole("button", { name: "Close" })[0]);
  fireEvent.click(screen.getByRole("button", { name: "+ Connect engine" }));
  await act(async () => { confirmation.resolve(Response.json([])); });
  expect(screen.getByRole("heading", { name: "Add a credential" })).toBeVisible();
  expect(screen.queryByText("Historical", { exact: true })).not.toBeInTheDocument();
  expect(useSessionStore.getState().credentials).toEqual([]);
});

it("login confirmation follows a deletion's newer read without replaying sign-in or reviving the deleted result", async () => {
  const row = { id: "saved", label: "Saved", backend: "claude-code", auth_type: "oauth", created_at: "now", status: "active", needs_reconnect: false };
  const other = { ...row, id: "new", label: "New" };
  const stale = deferred<Response>();
  let completed = false, reads = 0, logins = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/oauth/start")) return Response.json({ login_id: "login", device_url: "https://example.test" });
    if (path.endsWith("/oauth/complete")) { logins++; completed = true; return Response.json(row); }
    if (init?.method === "DELETE") return Response.json({ deleted: true });
    if (completed && ++reads === 1) return stale.promise;
    return Response.json(completed ? [other] : [row]);
  }));
  await act(async () => { render(<HarnessPage onToggleSidebar={() => {}} />); });
  fireEvent.click(screen.getByRole("button", { name: "+ Connect engine" }));
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: /Claude Code.*Sign in with Anthropic/ })); });
  fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Saved" } });
  fireEvent.change(screen.getByLabelText("Code from browser"), { target: { value: "code" } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Finish sign-in" })); });
  await act(async () => { await modelCredentials.remove("saved"); });
  expect(screen.getByRole("status")).toHaveTextContent("no longer in your credential list");
  expect(screen.getByText("New", { exact: true })).toBeVisible();
  await act(async () => { stale.resolve(Response.json([row])); });
  expect(useSessionStore.getState().credentials).toEqual([other]);
  expect(logins).toBe(1);
});

it("sign-in confirmation has one total deadline even when auth-expired starts a later shared read", async () => {
  vi.useFakeTimers();
  const waits: ReturnType<typeof deferred<Response>>[] = [];
  let completed = false;
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/oauth/start")) return Response.json({ login_id: "login", device_url: "https://example.test" });
    if (path.endsWith("/oauth/complete")) { completed = true; return Response.json({ id: "saved" }); }
    if (!completed) return Response.json([]);
    const wait = deferred<Response>(); waits.push(wait); return wait.promise;
  }));
  await enterClaudeCode();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Finish sign-in" })); });
  await act(async () => vi.advanceTimersByTimeAsync(20_000));
  await act(async () => { handleWsMessage({ type: "error", session_id: "s", code: "auth_expired", message: "expired" }); });
  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  expect(screen.getByRole("status")).toHaveTextContent("Sign-in completed; credential list is not synchronized.");
  expect(useSessionStore.getState().credentialRead.loading).toBe(true);
  await act(async () => { waits.at(-1)!.resolve(Response.json([])); });
  expect(useSessionStore.getState().credentialRead).toMatchObject({ loading: false, stale: false });
});

it("waits again with the original Claude request and confirms current credentials instead of restoring a deleted result", async () => {
  const requests: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/oauth/start")) return Response.json({ login_id: "original-login", device_url: "https://example.test/login" });
    if (path.endsWith("/oauth/complete")) {
      requests.push(JSON.parse(String(init?.body)));
      if (requests.length === 1) throw new TypeError("Response lost after commit");
      return Response.json({ id: "deleted-credential", backend: "claude-code", label: "Personal", auth_type: "oauth", created_at: "now", status: "active", needs_reconnect: false });
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

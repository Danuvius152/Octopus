import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { HarnessPage } from "./HarnessPage";
import { useSessionStore, type Agent, type CredentialInfo } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";

let status: (init?: RequestInit) => Promise<Response>;
let reads = 0;
let posts: string[];
let credentials: CredentialInfo[];
beforeEach(() => {
  vi.useFakeTimers();
  useSessionStore.getState().setToken("");
  useSessionStore.getState().setToken("poll-account");
  reads = 0; posts = []; credentials = [];
  status = async () => Response.json({ state: "pending" });
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    const path = String(url);
    if (init?.method === "POST") posts.push(path);
    if (path.endsWith("/codex/start")) return Response.json({ login_id: "original-login" });
    if (path.endsWith("/codex/original-login/status")) { reads++; return status(init); }
    if (path.endsWith("/credentials")) return Response.json(credentials);
    return Response.json([]);
  }));
});
afterEach(() => { cleanup(); useSessionStore.getState().setToken(""); vi.useRealTimers(); vi.unstubAllGlobals(); });
async function start() {
  await act(async () => { render(<HarnessPage onToggleSidebar={() => {}} />); });
  fireEvent.click(screen.getByText("Sign in to Claude Code or Codex"));
  fireEvent.click(screen.getByRole("button", { name: /Codex.*Sign in with ChatGPT/ }));
  fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Original login" } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Continue" })); });
  await act(async () => { await vi.advanceTimersByTimeAsync(600); });
}

it("stops the original login ID on pending then 404 and never replays its POST", async () => {
  status = async () => reads === 1 ? Response.json({ state: "pending" }) : Response.json({ detail: "Not found" }, { status: 404 });
  await start();
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(screen.getByRole("alert")).toHaveTextContent("no longer available");
  expect(screen.queryByRole("button", { name: "Continue querying this sign-in" })).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(reads).toBe(2);
  expect(posts).toHaveLength(1);
});

it("bounds the first hanging GET and resumes only a read when the user continues", async () => {
  status = () => new Promise(() => {}); // Even a transport that ignores abort cannot hold the UI forever.
  await start();
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(screen.getByRole("alert")).toHaveTextContent("temporarily unconfirmed");
  expect(reads).toBe(1);
  status = async () => Response.json({ state: "pending" });
  fireEvent.click(screen.getByRole("button", { name: "Continue querying this sign-in" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(600); });
  expect(reads).toBe(2);
  expect(posts).toHaveLength(1);
  expect(screen.queryByRole("alert")).toBeNull();
});

it("recovers from pending then transient 503 then success without a new login", async () => {
  status = async () => reads === 1 ? Response.json({ state: "pending" }) : reads === 2
    ? Response.json({ detail: "Unavailable" }, { status: 503 }) : Response.json({ state: "success", credential: null });
  await start();
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(screen.getByRole("status")).toHaveTextContent("Sign-in completed");
  expect(reads).toBe(3);
  expect(posts).toHaveLength(1);
});

it("bounds the initial JSON body read and ignores its success delivered after timeout", async () => {
  let body!: ReadableStreamDefaultController<Uint8Array>;
  status = async () => new Response(new ReadableStream<Uint8Array>({ start(controller) { body = controller; } }));
  await start();
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(screen.getByRole("alert")).toHaveTextContent("temporarily unconfirmed");
  await act(async () => { body.enqueue(new TextEncoder().encode(JSON.stringify({ state: "success", credential: null }))); body.close(); });
  expect(screen.getByRole("alert")).toHaveTextContent("temporarily unconfirmed");
  expect(screen.queryByRole("status")).toBeNull();
  expect(posts).toHaveLength(1);
});

it.each(["headers", "body"])("counts the first failed read's elapsed time and bounds a later hanging %s read within that fault segment", async phase => {
  const failed = deferred<Response>();
  let body: ReadableStreamDefaultController<Uint8Array> | undefined;
  status = () => reads === 1 ? failed.promise : phase === "headers" ? new Promise(() => {})
    : Promise.resolve(new Response(new ReadableStream<Uint8Array>({ start(controller) { body = controller; } })));
  await start();
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); failed.resolve(Response.json({}, { status: 503 })); });
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(reads).toBe(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(17_999); });
  expect(screen.queryByRole("alert")).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.getByRole("alert")).toHaveTextContent("temporarily unconfirmed");
  expect(reads).toBe(2);
  if (body) await act(async () => { body!.enqueue(new TextEncoder().encode('{"state":"pending"}')); body!.close(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(reads).toBe(2);
  expect(posts).toHaveLength(1);
});

it("resets the continuous fault segment only after a complete valid pending response", async () => {
  const pending = deferred<Response>();
  status = () => reads === 1 ? Promise.resolve(Response.json({}, { status: 503 })) : reads === 2 ? pending.promise : new Promise(() => {});
  await start();
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  await act(async () => { await vi.advanceTimersByTimeAsync(20_000); pending.resolve(Response.json({ state: "pending" })); });
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(reads).toBe(3);
  await act(async () => { await vi.advanceTimersByTimeAsync(29_999); });
  expect(screen.queryByRole("alert")).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.getByRole("alert")).toHaveTextContent("temporarily unconfirmed");
});

it("keeps healthy pending authorization open beyond thirty seconds", async () => {
  status = async () => Response.json({ state: "pending", verification_url: "https://example.test/authorize", user_code: "VALID" });
  await start();
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(reads).toBe(31);
  expect(screen.getByText("VALID")).toBeVisible();
  expect(screen.queryByRole("alert")).toBeNull();
});

it.each([401, 403])("ends unauthorized HTTP %s observation without automatic retry", async code => {
  status = async () => Response.json({}, { status: code });
  await start();
  expect(screen.getByRole("alert")).toHaveTextContent(code === 401 ? "Authentication is required" : "do not have permission");
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(reads).toBe(1);
  expect(posts).toHaveLength(1);
});

it("does not count malformed pending bodies as healthy observations", async () => {
  status = async () => Response.json({ state: "pending", user_code: 42 });
  await start();
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(screen.getByRole("alert")).toHaveTextContent("temporarily unconfirmed");
});

it("refreshes current credentials without claiming the original unknown login succeeded", async () => {
  status = () => new Promise(() => {});
  await start();
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  const before = vi.mocked(fetch).mock.calls.length;
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Refresh current credentials" })); });
  expect(screen.getByRole("alert")).toHaveTextContent("temporarily unconfirmed");
  expect(screen.queryByRole("status")).toBeNull();
  const refreshes = vi.mocked(fetch).mock.calls.slice(before);
  expect(refreshes).toHaveLength(1);
  expect(refreshes.every(([url, init]) => String(url).endsWith("/credentials") && !init?.method)).toBe(true);
  expect(posts).toHaveLength(1);
  expect(reads).toBe(1);
});

it("starts another login only after an explicit new-sign-in action", async () => {
  status = async () => Response.json({}, { status: 404 });
  await start();
  fireEvent.click(screen.getByRole("button", { name: "Start a new Codex sign-in" }));
  expect(screen.getByLabelText("Label")).toHaveValue("Original login");
  expect(posts).toHaveLength(1);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Continue" })); });
  expect(posts).toHaveLength(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(600); });
  expect(reads).toBe(2);
  const requests = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/codex/start"));
  expect(requests.map(([, init]) => JSON.parse(init!.body as string).reauth_credential_id)).toEqual([null, null]);
});

it("preserves the reauthorization target and its agent binding when explicitly restarting a missing login", async () => {
  credentials = [{ id: "expired", label: "Bound credential", backend: "codex", auth_type: "oauth", created_at: "now",
    status: "needs_reconnect", needs_reconnect: true }];
  useSessionStore.setState({ agents: [{ id: "bound", name: "Bound agent", credential_id: "expired" }] as Agent[] });
  status = async () => Response.json({}, { status: 404 });
  await act(async () => { render(<HarnessPage onToggleSidebar={() => {}} />); });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Re-authorize →" })); });
  await act(async () => { await vi.advanceTimersByTimeAsync(600); });
  expect(screen.getByRole("alert")).toHaveTextContent("no longer available");
  fireEvent.click(screen.getByRole("button", { name: "Start a new Codex sign-in" }));
  expect(screen.getByLabelText("Label")).toHaveValue("Bound credential");
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Continue" })); });
  const requests = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/codex/start"));
  expect(requests.map(([, init]) => JSON.parse(init!.body as string).reauth_credential_id)).toEqual(["expired", "expired"]);
  status = async () => {
    credentials = [{ ...credentials[0], status: "active", needs_reconnect: false }];
    return Response.json({ state: "success", credential: credentials[0] });
  };
  await act(async () => { await vi.advanceTimersByTimeAsync(600); });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByText("Connected")).toBeVisible();
  expect(screen.getByText("Bound agent")).toBeVisible();
  expect(useSessionStore.getState().credentials.map(row => row.id)).toEqual(["expired"]);
  expect(useSessionStore.getState().agents[0].credential_id).toBe("expired");
});

it("cancels the status observation when its account retires", async () => {
  let signal!: AbortSignal;
  const reply = deferred<Response>();
  status = init => { signal = init!.signal!; return reply.promise; };
  await start();
  act(() => useSessionStore.getState().setToken("next-account"));
  expect(signal.aborted).toBe(true);
  expect(screen.queryByRole("dialog")).toBeNull();
  await act(async () => reply.resolve(Response.json({ state: "error", message: "Retired error" })));
  expect(screen.queryByText("Retired error")).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(reads).toBe(1);
});

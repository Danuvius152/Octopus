import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AccountPage } from "./AccountPage";
import { useSessionStore } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";
const info = (roots: string[]) => ({ workspace: "/workspace", extra_roots: roots, confined: true });
interface Request { body: Record<string, unknown>; response: ReturnType<typeof deferred<Response>> }
let serial = 0;
let owner: string;
let puts: Request[], confirms: Request[], registrations: Record<string, unknown>[];
let heldLoads: ReturnType<typeof deferred<Response>>[];
beforeEach(() => {
  owner = `workspace-owner-${++serial}`;
  puts = []; confirms = []; registrations = []; heldLoads = [];
  useSessionStore.getState().setToken("");
  useSessionStore.getState().setToken("workspace-a");
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    const path = String(url), other = new Headers(init?.headers).get("Authorization") === "Bearer workspace-b";
    if (path.endsWith("/identity")) return Response.json({ user_id: other ? `${owner}-other` : owner, label: other ? "Account B" : "Account A", is_admin: true });
    if (path.endsWith("/workspace-edits")) {
      const body = JSON.parse(String(init?.body));
      registrations.push(body);
      return Response.json({ ...body, service_generation: "workspace-service", sealed_through: 0, workspace: info(other ? ["/other/root"] : ["/repo/a", "/repo/b"]) });
    }
    if (init?.method === "PUT" || path.endsWith("/confirm")) {
      const request = { body: JSON.parse(String(init?.body)), response: deferred<Response>() };
      (init?.method === "PUT" ? puts : confirms).push(request);
      return request.response.promise;
    }
    if (path.endsWith("/workspace")) return Response.json(info(["/stale/root"]));
    return Response.json([]);
  }));
});
afterEach(async () => {
  cleanup();
  useSessionStore.getState().setToken("");
  await act(async () => {
    for (const response of [...puts, ...confirms].map(request => request.response).concat(heldLoads)) response.reject(new Error("Test request retired"));
  });
  vi.unstubAllGlobals();
});
async function mount() {
  const view = render(<AccountPage />);
  await screen.findByLabelText("Stop allowing /repo/a");
  return view;
}

it("admits only one same-batch retry of the editing baseline", async () => {
  const original = fetch, pending = deferred<Response>();
  heldLoads.push(pending);
  let openings = 0, body: Record<string, unknown>;
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    if (String(url).endsWith("/workspace-edits")) {
      body = JSON.parse(String(init?.body));
      if (++openings === 1) return Response.json({ detail: "Unavailable" }, { status: 503 });
      return pending.promise;
    }
    return original(url as RequestInfo, init);
  }));
  render(<AccountPage />);
  const retry = await screen.findByRole("button", { name: "Retry loading" });
  act(() => { retry.click(); retry.click(); });
  expect(openings).toBe(2);
  await act(async () => pending.resolve(Response.json({ ...body!, service_generation: "workspace-service", sealed_through: 0,
    workspace: info(["/repo/a", "/repo/b"]) })));
  expect(screen.getByLabelText("Stop allowing /repo/a")).toBeEnabled();
});
function proof(request: Request, roots: string[], extra: object = {}) {
  return Response.json({ flow_id: request.body.flow_id, service_generation: request.body.service_generation,
    sealed_through: request.body.sequence, workspace: info(roots), ...extra });
}

it("keeps the administrator page usable without randomUUID", async () => {
  vi.stubGlobal("crypto", { getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto) });
  await mount();
  expect(screen.getByRole("button", { name: "Change password" })).toBeVisible();
  expect(registrations[0].flow_id).toMatch(/^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/);
});

it("contains unavailable secure randomness to directory editing and can retry after recovery", async () => {
  const crypto = globalThis.crypto;
  vi.stubGlobal("crypto", {});
  render(<AccountPage />);
  await screen.findByRole("button", { name: "Change password" });
  expect(await screen.findByRole("alert")).toHaveTextContent("Secure randomness is unavailable");
  expect(registrations).toHaveLength(0);
  vi.stubGlobal("crypto", crypto);
  fireEvent.click(screen.getByRole("button", { name: "Retry loading" }));
  expect(await screen.findByLabelText("Stop allowing /repo/a")).toBeEnabled();
});

it("admits one same-batch click/Enter, keeps a refusal draft and adds again from the authoritative receipt", async () => {
  await mount();
  const input = screen.getByLabelText("Add a directory");
  fireEvent.change(input, { target: { value: "/typed/first" } });
  act(() => {
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.keyDown(input, { key: "Enter" });
  });
  expect(puts).toHaveLength(1);
  await act(async () => puts[0].response.resolve(Response.json({ detail: "Directory is unavailable" }, { status: 400 })));
  expect(input).toHaveValue("/typed/first");
  expect(input).toBeEnabled();
  expect(screen.getByRole("alert")).toHaveTextContent("Directory is unavailable");
  expect(screen.getByText("/repo/a")).toBeVisible();
  fireEvent.keyDown(input, { key: "Enter", isComposing: true });
  fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
  expect(puts).toHaveLength(1);
  fireEvent.keyDown(input, { key: "Enter" });
  await act(async () => puts[1].response.resolve(Response.json(info(["/repo/a", "/repo/b", "/canonical/first"]))));
  fireEvent.change(input, { target: { value: "/typed/second" } });
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
  expect(puts[2].body.extra_roots).toEqual(["/repo/a", "/repo/b", "/canonical/first", "/typed/second"]);
  await act(async () => puts[2].response.resolve(Response.json(info(["/repo/a", "/repo/b", "/canonical/first", "/canonical/second"]))));
  expect(screen.getByText("/canonical/first")).toBeVisible();
  expect(screen.getByText("/canonical/second")).toBeVisible();
  expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/workspace"))).toHaveLength(0);
});

it("requires an idempotent seal after an unknown write, retaining the original sequence when its receipt is lost", async () => {
  await mount();
  fireEvent.click(screen.getByLabelText("Stop allowing /repo/a"));
  await act(async () => puts[0].response.reject(new TypeError("Connection lost after upload")));
  expect(screen.getByLabelText("Add a directory")).toBeDisabled();
  // An ordinary read cannot serve as proof of the old writer's retirement.
  await fetch("/api/auth/workspace");
  expect(screen.getByLabelText("Stop allowing /repo/b")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Confirm directory permissions" }));
  expect(confirms[0].body).toMatchObject({ flow_id: puts[0].body.flow_id, sequence: puts[0].body.sequence });
  await act(async () => confirms[0].response.reject(new TypeError("Lost seal receipt")));
  fireEvent.click(screen.getByRole("button", { name: "Confirm directory permissions" }));
  expect(confirms[1].body).toEqual(confirms[0].body);
  await act(async () => confirms[1].response.resolve(proof(confirms[1], ["/repo/b"])));
  expect(screen.queryByText("/repo/a")).toBeNull();
  fireEvent.click(screen.getByLabelText("Stop allowing /repo/b"));
  expect(puts[1].body.extra_roots).toEqual([]);
  expect(Number(puts[1].body.sequence)).toBeGreaterThan(Number(puts[0].body.sequence));
  await act(async () => puts[1].response.resolve(Response.json(info([]))));
});

it("preserves the pending flow across form reopening and account A to B to A while ignoring late success and finally", async () => {
  const view = await mount();
  fireEvent.click(screen.getByLabelText("Stop allowing /repo/a"));
  view.unmount();
  render(<AccountPage />);
  await screen.findByRole("button", { name: "Confirm directory permissions" });
  expect(registrations).toHaveLength(1);
  act(() => useSessionStore.getState().setToken("workspace-b"));
  await screen.findByText("/other/root");
  expect(screen.queryByText("/repo/a")).toBeNull();
  act(() => useSessionStore.getState().setToken("workspace-a"));
  fireEvent.click(await screen.findByRole("button", { name: "Confirm directory permissions" }));
  expect(registrations).toHaveLength(2); // One for each account, never a replacement flow for A.
  expect(confirms[0].body).toMatchObject({ flow_id: puts[0].body.flow_id, sequence: puts[0].body.sequence });
  await act(async () => confirms[0].response.resolve(proof(confirms[0], ["/repo/a", "/repo/b"])));
  fireEvent.click(screen.getByLabelText("Stop allowing /repo/b"));
  expect(puts[1].body.extra_roots).toEqual(["/repo/a"]);
  await act(async () => puts[0].response.resolve(Response.json(info(["/repo/b"]))));
  expect(screen.getByLabelText("Stop allowing /repo/b")).toBeDisabled();
  expect(screen.getByLabelText("Add a directory")).toBeDisabled();
  await act(async () => puts[1].response.resolve(Response.json(info(["/repo/a"]))));
  expect(screen.queryByText("/repo/b")).toBeNull();
  expect(screen.getByText("/repo/a")).toBeVisible();
});

it("accepts a new service only with explicit old-writer retirement proof", async () => {
  await mount();
  fireEvent.click(screen.getByLabelText("Stop allowing /repo/a"));
  await act(async () => puts[0].response.resolve(Response.json({ detail: "Service retired" }, { status: 409 })));
  fireEvent.click(screen.getByRole("button", { name: "Confirm directory permissions" }));
  await act(async () => confirms[0].response.resolve(proof(confirms[0], ["/repo/b"], { service_generation: "new-service" })));
  expect(screen.getByLabelText("Add a directory")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Confirm directory permissions" }));
  await act(async () => confirms[1].response.resolve(proof(confirms[1], ["/repo/b"], {
    service_generation: "new-service", previous_generation_retired: true,
  })));
  await waitFor(() => expect(screen.getByLabelText("Add a directory")).toBeEnabled());
  fireEvent.click(screen.getByLabelText("Stop allowing /repo/b"));
  expect(puts[1].body.service_generation).toBe("new-service");
  await act(async () => puts[1].response.resolve(Response.json(info([]))));
});

import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AccountPage } from "./AccountPage";
import { useSessionStore } from "../stores/sessionStore";
import type { InviteInfo, UserInfo } from "../api";
import { deferred } from "../test-support/deferred";

const invite: InviteInfo = { code: "invite-a", created_at: "2026-11-01T00:00:00Z", expires_at: "2100-01-01T00:00:00Z", max_uses: 2, used_count: 0, revoked_at: null };
const person: UserInfo = { id: "person-a", username: "Person A", created_at: "2026-11-01T00:00:00Z", is_admin: false, disabled_at: null };

function routes(handle: (path: string, init?: RequestInit) => Response | Promise<Response> | undefined) {
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), window.location.origin).pathname.replace("/api/auth", "");
    const response = handle(path, init);
    if (response) return response;
    if (path === "/identity") return Response.json({ user_id: "management-owner", label: "Manager", is_admin: true });
    if (path.endsWith("/workspace-edits")) return Response.json({
      flow_id: JSON.parse(String(init?.body)).flow_id, service_generation: "management-service", sealed_through: 0,
      workspace: { workspace: "/workspace", extra_roots: [], confined: true },
    });
    if (path === "/workspace") return Response.json({ workspace: "/workspace", extra_roots: [], confined: true });
    if (path === "/invites") return Response.json([invite]);
    if (path === "/users") return Response.json([person]);
    throw new Error(`Unexpected management route: ${path}`);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

beforeEach(() => useSessionStore.getState().setToken("management-account-a"));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("offers copy only for available invites, including expiry while the page remains open", async () => {
  let now = Date.parse("2026-11-17T12:00:00Z");
  vi.spyOn(Date, "now").mockImplementation(() => now);
  const clipboard = vi.fn(async () => {});
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: clipboard } });
  const rows = [
    { ...invite, code: "available" },
    { ...invite, code: "expired", expires_at: "2026-11-17T11:00:00Z" },
    { ...invite, code: "exhausted", used_count: 2 },
    { ...invite, code: "both", expires_at: "2026-11-17T11:00:00Z", used_count: 2 },
    { ...invite, code: "revoked", revoked_at: "2026-11-17T11:00:00Z" },
    { ...invite, code: "revoked-expired-exhausted", revoked_at: "2026-11-17T11:00:00Z", expires_at: "2026-11-17T11:00:00Z", used_count: 2 },
    { ...invite, code: "no-expiry", expires_at: null },
    { ...invite, code: "expires-soon", expires_at: "2026-11-17T12:00:00.100Z" },
  ];
  routes(path => path === "/invites" ? Response.json(rows) : undefined);
  render(<AccountPage />);
  await screen.findByText("expires-soon");
  for (const code of ["expired", "exhausted", "both"]) {
    const row = screen.getByText(code).closest("li")!;
    expect(within(row).queryByRole("button", { name: "Copy invite code" })).toBeNull();
  }
  expect(screen.queryByText("revoked")).toBeNull();
  expect(screen.queryByText("revoked-expired-exhausted")).toBeNull();
  fireEvent.click(within(screen.getByText("available").closest("li")!).getByRole("button", { name: "Copy invite code" }));
  fireEvent.click(within(screen.getByText("no-expiry").closest("li")!).getByRole("button", { name: "Copy invite code" }));
  const soon = within(screen.getByText("expires-soon").closest("li")!).getByRole("button", { name: "Copy invite code" });
  now = Date.parse("2026-11-17T12:00:00.101Z");
  fireEvent.click(soon);
  expect(clipboard.mock.calls).toEqual([["available"], ["no-expiry"]]);
  await waitFor(() => expect(within(screen.getByText("expires-soon").closest("li")!).queryByRole("button", { name: "Copy invite code" })).toBeNull());
});

it.each(["invites", "users"] as const)("preserves existing %s after a failed refresh and recovers by GET", async (list) => {
  let reads = 0;
  routes(path => {
    if (path !== `/${list}`) return;
    reads++;
    return reads === 2 ? Response.json({ detail: "Refresh failed" }, { status: 502 })
      : Response.json(list === "invites" ? [invite] : [person]);
  });
  render(<AccountPage />);
  const text = list === "invites" ? invite.code : person.username;
  const row = await screen.findByText(text);
  const section = row.closest("section")!;
  fireEvent.click(within(section).getByRole("button", { name: `Refresh ${list}` }));
  expect(await within(section).findByRole("alert")).toHaveTextContent("Refresh failed");
  expect(within(section).getByText(text)).toBeTruthy();
  fireEvent.click(within(section).getByRole("button", { name: `Retry loading ${list}` }));
  await screen.findByRole("button", { name: `Refresh ${list}` });
  expect(within(section).queryByRole("alert")).toBeNull();
  expect(reads).toBe(3);
});

it.each(["invites", "users"] as const)("holds one write per %s target and retains GET-only recovery after confirmation fails", async (list) => {
  const writing = deferred<Response>();
  let reads = 0;
  let writes = 0;
  routes((path, init) => {
    if (path === `/${list}` && !init?.method) {
      reads++;
      if (reads === 2) return Response.json({ detail: "Confirmation unavailable" }, { status: 503 });
      return Response.json(list === "invites" ? (reads > 2 ? [] : [invite])
        : [{ ...person, disabled_at: reads > 2 ? "2026-11-17T12:00:00Z" : null }]);
    }
    if (path === `/invites/${invite.code}` || path === `/users/${person.id}/disabled`) {
      writes++;
      return writing.promise;
    }
  });
  render(<AccountPage />);
  const label = list === "invites" ? "Revoke invite" : "Disable account";
  const button = await screen.findByRole("button", { name: label });
  // Same-batch native clicks expose a gate implemented only by disabled props.
  act(() => { button.click(); button.click(); });
  expect(writes).toBe(1);
  await act(async () => writing.resolve(new Response(null, { status: 204 })));
  const section = button.closest("section")!;
  expect(await within(section).findByText(/write succeeded.*confirmation/i)).toBeTruthy();
  const retry = within(section).getByRole("button", { name: "Retry confirmation" });
  expect(button).toBeDisabled();
  fireEvent.click(retry);
  if (list === "invites") await screen.findByText("No live invites.");
  else await screen.findByRole("button", { name: "Enable account" });
  expect(writes).toBe(1);
  expect(reads).toBe(3);
});

it.each(["invites", "users"] as const)("shows a refused %s write and allows a later explicit attempt", async (list) => {
  let writes = 0;
  let reads = 0;
  routes((path, init) => {
    if (path === `/${list}` && !init?.method) {
      reads++;
      return Response.json(list === "invites" ? (writes > 1 ? [] : [invite]) : [{ ...person, disabled_at: writes > 1 ? "2026-11-17T12:00:00Z" : null }]);
    }
    if (path === `/invites/${invite.code}` || path === `/users/${person.id}/disabled`) {
      writes++;
      return writes === 1 ? Response.json({ detail: "Permission denied for this change" }, { status: 403 }) : new Response(null, { status: 204 });
    }
  });
  render(<AccountPage />);
  const button = await screen.findByRole("button", { name: list === "invites" ? "Revoke invite" : "Disable account" });
  fireEvent.click(button);
  expect(await screen.findByRole("alert")).toHaveTextContent("Permission denied for this change");
  expect(button).toBeEnabled();
  expect(reads).toBe(1);
  fireEvent.click(button);
  if (list === "invites") await screen.findByText("No live invites.");
  else await screen.findByRole("button", { name: "Enable account" });
  expect(writes).toBe(2);
  expect(reads).toBe(2);
});

it.each(["invites", "users"] as const)("keeps a lost %s write unknown and checks status without replaying it", async (list) => {
  let writes = 0;
  let reads = 0;
  routes((path, init) => {
    if (path === `/${list}` && !init?.method) {
      reads++;
      return Response.json(list === "invites" ? (writes ? [] : [invite]) : [{ ...person, disabled_at: writes ? "2026-11-17T12:00:00Z" : null }]);
    }
    if (path === `/invites/${invite.code}` || path === `/users/${person.id}/disabled`) {
      writes++;
      return Promise.reject(new TypeError("Network connection lost"));
    }
  });
  render(<AccountPage />);
  const button = await screen.findByRole("button", { name: list === "invites" ? "Revoke invite" : "Disable account" });
  fireEvent.click(button);
  expect(await screen.findByRole("alert")).toHaveTextContent("Write result unknown");
  expect(button).toBeDisabled();
  expect(reads).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: "Check status" }));
  if (list === "invites") await screen.findByText("No live invites.");
  else await screen.findByRole("button", { name: "Enable account" });
  expect(writes).toBe(1);
  expect(reads).toBe(2);
});

it.each(["invites", "users"] as const)("lets a newer %s confirmation settle both writes before an earlier snapshot returns", async (list) => {
  const first = deferred<Response>();
  const second = deferred<Response>();
  const earlierSnapshot = deferred<Response>();
  const writes: string[] = [];
  let reads = 0;
  const originalRows = list === "invites" ? [invite, { ...invite, code: "invite-b" }] : [person, { ...person, id: "person-b", username: "Person B" }];
  routes((path, init) => {
    if (path === `/${list}` && !init?.method) {
      reads++;
      if (reads === 2) return earlierSnapshot.promise;
      return Response.json(reads === 1 ? originalRows : list === "invites" ? []
        : [person, { ...person, id: "person-b", username: "Person B" }].map(row => ({ ...row, disabled_at: "2026-11-17T12:00:00Z" })));
    }
    if (list === "invites" ? path.startsWith("/invites/") : path.endsWith("/disabled")) {
      writes.push(path);
      return path.includes("-a") ? first.promise : second.promise;
    }
  });
  render(<AccountPage />);
  await screen.findByText(list === "invites" ? "invite-b" : "Person B");
  const label = list === "invites" ? "Revoke invite" : "Disable account";
  const buttons = screen.getAllByRole("button", { name: label });
  act(() => { buttons[0].click(); buttons[0].click(); buttons[1].click(); });
  expect(writes).toHaveLength(2);
  await act(async () => second.resolve(new Response(null, { status: 204 })));
  expect(reads).toBe(2);
  await act(async () => first.resolve(new Response(null, { status: 204 })));
  if (list === "invites") await screen.findByText("No live invites.");
  else await waitFor(() => expect(screen.getAllByRole("button", { name: "Enable account" })).toHaveLength(2));
  expect(reads).toBe(3);
  expect(screen.queryByText(/Reading the current list to confirm/)).toBeNull();
  if (list === "users") screen.getAllByRole("button", { name: "Enable account" }).forEach(button => expect(button).toBeEnabled());
  const stale = list === "invites" ? [invite] : [person, { ...person, id: "person-b", username: "Person B", disabled_at: "2026-11-17T12:00:00Z" }];
  await act(async () => earlierSnapshot.resolve(Response.json(stale)));
  if (list === "invites") await screen.findByText("No live invites.");
  else await waitFor(() => expect(screen.getAllByRole("button", { name: "Enable account" })).toHaveLength(2));
  expect(reads).toBe(3);
  expect(writes).toHaveLength(2);
});

it("synchronously reserves invite creation through a failed confirmation", async () => {
  const writing = deferred<Response>();
  let writes = 0;
  let reads = 0;
  routes((path, init) => {
    if (path !== "/invites") return;
    if (init?.method === "POST") { writes++; return writing.promise; }
    reads++;
    return reads === 2 ? Response.json({ detail: "Invite list unavailable" }, { status: 503 }) : Response.json(reads > 2 ? [invite] : []);
  });
  render(<AccountPage />);
  await screen.findByText("No live invites.");
  const button = screen.getByRole("button", { name: "New invite" });
  act(() => { button.click(); button.click(); });
  expect(writes).toBe(1);
  await act(async () => writing.resolve(Response.json(invite, { status: 201 })));
  expect(await screen.findByText(/write succeeded.*confirmation/i)).toBeTruthy();
  expect(button).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Retry confirmation" }));
  await screen.findByText(invite.code);
  expect(button).toBeEnabled();
  expect(writes).toBe(1);
  expect(reads).toBe(3);
});

it("keeps a lost invite creation unknown across old snapshots, unrelated creations and its late commit", async () => {
  let writes = 0;
  let reads = 0;
  let rows: InviteInfo[] = [];
  routes((path, init) => {
    if (path !== "/invites") return;
    if (init?.method === "POST") {
      writes++;
      return Promise.reject(new TypeError("Reply lost before the server committed"));
    }
    reads++;
    return Response.json(rows);
  });
  render(<AccountPage />);
  await screen.findByText("No live invites.");
  const create = screen.getByRole("button", { name: "New invite" });
  fireEvent.click(create);
  await screen.findByText(/Write result unknown/);
  for (const snapshot of [[], [{ ...invite, code: "another-admin-invite" }],
    [{ ...invite, code: "another-admin-invite" }, { ...invite, code: "original-late-invite" }]]) {
    rows = snapshot;
    fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    await waitFor(() => expect(create).toBeDisabled());
    await screen.findByRole("button", { name: "Check status" });
    expect(screen.getByRole("alert")).toHaveTextContent(/Write result unknown/);
    expect(create).toBeDisabled();
    act(() => create.click());
    expect(writes).toBe(1);
  }
  expect(reads).toBe(4);
  expect(screen.getByText("original-late-invite")).toBeTruthy();
});

it("blocks copying a successfully revoked invite while the retained list cannot be confirmed", async () => {
  const clipboard = vi.fn(async () => {});
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: clipboard } });
  let reads = 0;
  let writes = 0;
  routes((path, init) => {
    if (path === `/invites/${invite.code}` && init?.method === "DELETE") {
      writes++;
      return new Response(null, { status: 204 });
    }
    if (path === "/invites") {
      reads++;
      return reads === 2 ? Response.json({ detail: "Confirmation unavailable" }, { status: 503 })
        : Response.json(reads === 1 ? [invite] : [{ ...invite, revoked_at: "2026-11-17T12:00:00Z" }]);
    }
  });
  render(<AccountPage />);
  const revoke = await screen.findByRole("button", { name: "Revoke invite" });
  fireEvent.click(revoke);
  await screen.findByText(/Write succeeded; confirmation failed/);
  const copy = screen.queryByRole("button", { name: "Copy invite code" });
  if (copy) fireEvent.click(copy);
  expect(clipboard).not.toHaveBeenCalled();
  expect(screen.getByText(invite.code)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Retry confirmation" }));
  await screen.findByText("No live invites.");
  expect(writes).toBe(1);
  expect(reads).toBe(3);
});

for (const list of ["invites", "users"] as const) {
  it.each(["read", "write-success", "write-network", "write-error-body", "confirmation"] as const)(
    `isolates a late ${list} %s from a new operation after A→B→A`, async (stage) => {
      const originalAccount = useSessionStore.getState().accountGeneration;
      const oldResponse = deferred<Response>();
      const oldBody = deferred<unknown>();
      const newResponse = deferred<Response>();
      let oldReads = 0;
      let newReads = 0;
      let oldWrites = 0;
      let newWrites = 0;
      let bodyStarted = false;
      let newCommitted = false;
      routes((path, init) => {
        const original = useSessionStore.getState().accountGeneration === originalAccount;
        if (path === `/${list}` && !init?.method) {
          if (original) {
            oldReads++;
            if (stage === "read" || (stage === "confirmation" && oldReads === 2)) return oldResponse.promise;
          } else newReads++;
          return Response.json(list === "invites" ? (newCommitted ? [] : [invite])
            : [{ ...person, disabled_at: newCommitted ? "2026-11-17T12:00:00Z" : null }]);
        }
        if (path === `/invites/${invite.code}` || path === `/users/${person.id}/disabled`) {
          if (!original) { newWrites++; return newResponse.promise; }
          oldWrites++;
          if (stage === "confirmation") return new Response(null, { status: 204 });
          if (stage === "write-error-body") {
            const response = new Response(null, { status: 403 });
            response.json = () => { bodyStarted = true; return oldBody.promise; };
            return response;
          }
          return oldResponse.promise;
        }
      });
      render(<AccountPage />);
      const label = list === "invites" ? "Revoke invite" : "Disable account";
      if (stage === "read") await waitFor(() => expect(oldReads).toBe(1));
      else {
        fireEvent.click(await screen.findByRole("button", { name: label }));
        if (stage === "write-error-body") await waitFor(() => expect(bodyStarted).toBe(true));
        if (stage === "confirmation") await waitFor(() => expect(oldReads).toBe(2));
      }
      act(() => {
        useSessionStore.getState().setToken("management-account-b");
        useSessionStore.getState().setToken("management-account-a");
      });
      const button = await screen.findByRole("button", { name: label });
      fireEvent.click(button);
      expect(newWrites).toBe(1);
      expect(button).toBeDisabled();
      await act(async () => {
        if (stage === "write-error-body") oldBody.resolve({ detail: "Old account refusal" });
        else if (stage === "write-network") oldResponse.reject(new TypeError("Old account network failure"));
        else oldResponse.resolve(stage === "read" || stage === "confirmation" ? Response.json([]) : new Response(null, { status: 204 }));
      });
      expect(screen.queryByText(/Old account/)).toBeNull();
      expect(button).toBeDisabled();
      expect(screen.getByText(list === "invites" ? invite.code : person.username)).toBeTruthy();
      expect(newReads).toBe(1);
      expect(oldWrites).toBe(stage === "read" ? 0 : 1);
      newCommitted = true;
      await act(async () => newResponse.resolve(new Response(null, { status: 204 })));
      if (list === "invites") await screen.findByText("No live invites.");
      else await screen.findByRole("button", { name: "Enable account" });
      expect(newReads).toBe(2);
      expect(newWrites).toBe(1);
      expect(screen.queryByRole("alert")).toBeNull();
    },
  );
}

it.each(["headers", "body"] as const)("confirms beyond a pre-write refresh with suspended %s and ignores its late snapshot", async (stage) => {
  const beforeWrite = deferred<Response>();
  const beforeBody = deferred<unknown>();
  let reads = 0;
  let writes = 0;
  let refreshSignal: AbortSignal | null | undefined;
  routes((path, init) => {
    if (path === `/invites/${invite.code}`) { writes++; return new Response(null, { status: 204 }); }
    if (path !== "/invites") return;
    reads++;
    if (reads === 2) {
      refreshSignal = init?.signal;
      if (stage === "headers") return beforeWrite.promise;
      const response = Response.json([]);
      response.json = () => beforeBody.promise;
      return response;
    }
    return Response.json(reads > 2 ? [] : [invite]);
  });
  render(<AccountPage />);
  const revoke = await screen.findByRole("button", { name: "Revoke invite" });
  fireEvent.click(screen.getByRole("button", { name: "Refresh invites" }));
  await waitFor(() => expect(reads).toBe(2));
  fireEvent.click(revoke);
  await act(async () => {});
  expect(writes).toBe(1);
  await screen.findByText("No live invites.");
  expect(reads).toBe(3);
  expect(refreshSignal?.aborted).not.toBe(true);
  await act(async () => {
    beforeWrite.resolve(Response.json([invite]));
    beforeBody.resolve([invite]);
  });
  expect(screen.getByText("No live invites.")).toBeTruthy();
  expect(screen.queryByText(invite.code)).toBeNull();
  expect(reads).toBe(3);
  expect(writes).toBe(1);
});

it("retires an unmounted table before an old successful write can start confirmation", async () => {
  const first = deferred<Response>();
  const second = deferred<Response>();
  let reads = 0;
  let writes = 0;
  let committed = false;
  routes((path, init) => {
    if (path === "/users" && !init?.method) {
      reads++;
      return Response.json([{ ...person, disabled_at: committed ? "2026-11-17T12:00:00Z" : null }]);
    }
    if (path === `/users/${person.id}/disabled`) { writes++; return writes === 1 ? first.promise : second.promise; }
  });
  const previous = render(<AccountPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Disable account" }));
  previous.unmount();
  render(<AccountPage />);
  const button = await screen.findByRole("button", { name: "Disable account" });
  fireEvent.click(button);
  await act(async () => first.resolve(new Response(null, { status: 204 })));
  expect(button).toBeDisabled();
  expect(reads).toBe(2);
  expect(writes).toBe(2);
  committed = true;
  await act(async () => second.resolve(new Response(null, { status: 204 })));
  await screen.findByRole("button", { name: "Enable account" });
  expect(reads).toBe(3);
  expect(writes).toBe(2);
});

it.each(["headers", "body"] as const)("lets manual refresh replace a suspended %s read without releasing an unknown write", async (stage) => {
  const oldHeaders = deferred<Response>();
  const oldBody = deferred<unknown>();
  let reads = 0;
  let writes = 0;
  routes((path, init) => {
    if (path === "/invites" && init?.method === "POST") {
      writes++;
      return Promise.reject(new TypeError("Write reply lost"));
    }
    if (path !== "/invites") return;
    reads++;
    if (reads === 2) {
      if (stage === "headers") return oldHeaders.promise;
      const response = Response.json([]);
      response.json = () => oldBody.promise;
      return response;
    }
    return Response.json(reads > 2 ? [invite] : []);
  });
  render(<AccountPage />);
  await screen.findByText("No live invites.");
  const create = screen.getByRole("button", { name: "New invite" });
  fireEvent.click(create);
  await screen.findByText(/Write result unknown/);
  const check = screen.getByRole("button", { name: "Check status" });
  act(() => { check.click(); check.click(); });
  expect(reads).toBe(2);
  fireEvent.click(screen.getByRole("button", { name: "Refresh invites" }));
  await screen.findByText(invite.code);
  expect(reads).toBe(3);
  expect(screen.getByRole("alert")).toHaveTextContent(/Write result unknown/);
  expect(create).toBeDisabled();
  await act(async () => { oldHeaders.resolve(Response.json([])); oldBody.resolve([]); });
  expect(screen.getByText(invite.code)).toBeTruthy();
  expect(screen.getByRole("button", { name: "Check status" })).toBeEnabled();
  expect(create).toBeDisabled();
  expect(writes).toBe(1);
});

it.each(["invites", "users"] as const)("shows an initial %s read failure and retries only that list", async (list) => {
  let reads = 0;
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), window.location.origin).pathname;
    if (path.endsWith("/identity")) return Response.json({ user_id: "management-owner", label: "Manager", is_admin: true });
    if (path.endsWith("/workspace-edits")) return Response.json({
      flow_id: JSON.parse(String(init?.body)).flow_id, service_generation: "management-service", sealed_through: 0,
      workspace: { workspace: "/workspace", extra_roots: [], confined: true },
    });
    if (path.endsWith(`/${list}`)) {
      reads++;
      return reads === 1 ? Response.json({ detail: "Management unavailable" }, { status: 503 }) : Response.json([]);
    }
    if (path.endsWith("/workspace")) return Response.json({ workspace: "/workspace", extra_roots: [], confined: true });
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetch);
  render(<AccountPage />);

  const label = list === "invites" ? "Invites" : "People";
  const section = (await screen.findByRole("heading", { name: label })).closest("section")!;
  expect(await within(section).findByRole("alert")).toHaveTextContent("Management unavailable");
  expect(within(section).queryByText("No live invites.")).toBeNull();
  expect(within(section).queryByText("No people.")).toBeNull();
  fireEvent.click(within(section).getByRole("button", { name: `Retry loading ${list}` }));
  await within(section).findByText(list === "invites" ? "No live invites." : "No people.");
  expect(within(section).queryByRole("alert")).toBeNull();
  expect(reads).toBe(2);
  expect(fetch.mock.calls.filter(([url, init]) => String(url).endsWith(`/${list}`) && init?.method && init.method !== "GET")).toHaveLength(0);
});

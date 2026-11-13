import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ConnectorCatalogEntry, ConnectorInstallationInfo } from "../api";
import { deferred } from "../test-support/deferred";
import { useSessionStore } from "../stores/sessionStore";
import { ConnectorsPage } from "./ConnectorsPage";

const custom = { kind: "custom", display_name: "Custom connector", available: true, custom: true } as ConnectorCatalogEntry;
beforeEach(() => useSessionStore.setState({ ...useSessionStore.getInitialState(),
  token: "catalog-account", accountGeneration: useSessionStore.getState().accountGeneration + 1,
  connectorCatalog: [custom],
}));
afterEach(() => { cleanup(); useSessionStore.getState().setToken(""); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function mount() {
  const view = render(<ConnectorsPage onToggleSidebar={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "+ New Connector" }));
  return view;
}

function createCustom(slug = custom.kind) {
  fireEvent.click(screen.getByRole("button", { name: /Add custom connector/ }));
  for (const [label, value] of [
    ["Slug", slug], ["Display name", custom.display_name],
    ["Authorize URL", "https://local.test/auth"], ["Token URL", "https://local.test/token"],
    ["API base URL", "https://local.test/api"], ["Client ID", "client"], ["Client secret", "secret"],
  ]) fireEvent.change(screen.getByLabelText(label), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
}

function configure() {
  fireEvent.click(screen.getByRole("button", { name: "Set up" }));
  fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "client" } });
  fireEvent.change(screen.getByLabelText("Client secret"), { target: { value: "secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
}

it("an old catalog GET cannot restore a definition after DELETE and its confirmation", async () => {
  const old = deferred<Response>();
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (init?.method === "DELETE") return new Response(null, { status: 204 });
    if (path === "/api/connectors/catalog") return ++reads === 1 ? old.promise : Response.json([]);
    return Response.json([]);
  }));
  mount();
  fireEvent.click(screen.getByTitle("Remove connector"));
  await waitFor(() => expect(reads).toBe(2));
  await waitFor(() => expect(screen.queryByText(custom.display_name)).toBeNull());
  await act(async () => old.resolve(Response.json([custom])));
  expect(screen.queryByText(custom.display_name)).toBeNull();
  expect(useSessionStore.getState().connectorCatalog).toEqual([]);
});

it("a committed POST with a delayed body cannot resurrect a definition observed and then deleted", async () => {
  const body = deferred<string>();
  let rows: ConnectorCatalogEntry[] = [];
  let creates = 0;
  let deletes = 0;
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (init?.method === "POST") {
      creates++;
      rows = [custom];
      return new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          void body.promise.then((value) => { controller.enqueue(new TextEncoder().encode(value)); controller.close(); });
        },
      }), { status: 201, headers: { "Content-Type": "application/json" } });
    }
    if (init?.method === "DELETE") { deletes++; rows = []; return new Response(null, { status: 204 }); }
    if (path === "/api/connectors/catalog") { reads++; return Response.json(rows); }
    return Response.json([]);
  }));
  mount();
  await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
  createCustom();
  await waitFor(() => expect(creates).toBe(1));
  expect(screen.getByRole("button", { name: "Creating…" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  fireEvent.click(screen.getByRole("button", { name: "Refresh catalog" }));
  fireEvent.click(await screen.findByTitle("Remove connector"));
  await waitFor(() => expect(useSessionStore.getState().connectorCatalog).toEqual([]));
  await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
  const confirmed = reads;
  await act(async () => body.resolve(JSON.stringify(custom)));
  await waitFor(() => expect(reads).toBe(confirmed + 1));
  await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
  expect(useSessionStore.getState().connectorCatalog).toEqual([]);
  expect(screen.queryByTitle("Remove connector")).toBeNull();
  expect(creates).toBe(1);
  expect(deletes).toBe(1);
});

it.each((["success", "error"] as const).flatMap((outcome) =>
  [false, true].map((afterCurrent) => ({ outcome, afterCurrent }))))(
  "retires a GET started during DELETE: late $outcome, afterCurrent=$afterCurrent", async ({ outcome, afterCurrent }) => {
    const deletion = deferred<Response>();
    const old = deferred<Response>();
    const current = deferred<Response>();
    let reads = 0;
    let deletes = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") { deletes++; return deletion.promise; }
      if (new URL(url).pathname === "/api/connectors/catalog") {
        reads++;
        return reads === 1 ? Response.json([custom]) : reads === 2 ? old.promise : current.promise;
      }
      return Response.json([]);
    }));
    mount();
    await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
    fireEvent.click(screen.getByTitle("Remove connector"));
    await waitFor(() => expect(deletes).toBe(1));
    fireEvent.click(screen.getByRole("button", { name: "Refresh catalog" }));
    await waitFor(() => expect(reads).toBe(2));
    await act(async () => deletion.resolve(new Response(null, { status: 204 })));
    await waitFor(() => expect(reads).toBe(3));
    const settleOld = () => act(async () => {
      if (outcome === "success") old.resolve(Response.json([custom]));
      else old.reject(new Error("Retired read failed"));
    });
    if (!afterCurrent) await settleOld();
    expect(useSessionStore.getState().connectorCatalog).toEqual([]);
    expect(screen.getByRole("button", { name: "Refresh catalog" })).toBeDisabled();
    expect(screen.queryByRole("alert")).toBeNull();
    await act(async () => current.reject(new Error("Current read failed")));
    if (afterCurrent) await settleOld();
    expect(await screen.findByRole("alert")).toHaveTextContent("Current read failed");
    expect(screen.getByRole("button", { name: "Refresh catalog" })).toBeEnabled();
    expect(deletes).toBe(1);
  });

it.each(["success", "failure"] as const)(
  "an old custom save %s cannot change the draft or busy state of a newer custom save", async (outcome) => {
    const replies = [deferred<Response>(), deferred<Response>()];
    let writes = 0;
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") return replies[writes++].promise;
      return Response.json([]);
    }));
    mount();
    await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
    createCustom();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    createCustom("new-draft");
    await waitFor(() => expect(writes).toBe(2));
    await act(async () => replies[0].resolve(outcome === "success" ? Response.json(custom) :
      Response.json({ detail: "Old save failed" }, { status: 503 })));
    expect(screen.getByLabelText("Slug")).toHaveValue("new-draft");
    expect(screen.getByRole("button", { name: "Creating…" })).toBeDisabled();
    expect(screen.queryByText("Old save failed")).toBeNull();
    expect(screen.queryByText("Connector saved.")).toBeNull();
    await act(async () => replies[1].resolve(Response.json({ ...custom, kind: "new-draft" })));
    expect(await screen.findByRole("status")).toHaveTextContent("Connector saved.");
    expect(screen.queryByLabelText("Slug")).toBeNull();
  });

it.each([false, true])("applies two independent DELETE receipts in reverse=%s order", async (reverse) => {
  const other = { ...custom, kind: "other", display_name: "Other connector" };
  let rows = [custom, other];
  const replies = [deferred<Response>(), deferred<Response>()];
  const oldConfirmation = deferred<Response>();
  const deleted: string[] = [];
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (init?.method === "DELETE") {
      const kind = path.split("/").at(-1)!;
      deleted.push(kind);
      return replies[kind === custom.kind ? 0 : 1].promise;
    }
    if (path === "/api/connectors/catalog") return ++reads === 2 ? oldConfirmation.promise : Response.json(rows);
    return Response.json([]);
  }));
  mount();
  await screen.findByText(other.display_name);
  for (const entry of [custom, other]) {
    const card = screen.getByText(entry.display_name).closest(".connector-catalog-item") as HTMLElement;
    fireEvent.click(within(card).getByTitle("Remove connector"));
  }
  const order = reverse ? [1, 0] : [0, 1];
  for (const index of order) {
    rows = rows.filter((row) => row.kind !== [custom, other][index].kind);
    await act(async () => replies[index].resolve(new Response(null, { status: 204 })));
  }
  await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
  await act(async () => oldConfirmation.resolve(Response.json([[custom, other][order[1]]])));
  expect(useSessionStore.getState().connectorCatalog).toEqual([]);
  expect(screen.queryByTitle("Remove connector")).toBeNull();
  expect(deleted).toEqual([custom.kind, other.kind]);
});

it.each([false, true])("a late DELETE cannot remove a confirmed same-slug replacement: creation body delayed=%s", async (delayed) => {
  const deletion = deferred<Response>();
  const creationBody = deferred<string>();
  const replacement = { ...custom, display_name: "Replacement connector" };
  let rows: ConnectorCatalogEntry[] = [custom];
  let failing = false;
  let creates = 0;
  let deletes = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "DELETE") { deletes++; rows = []; return deletion.promise; }
    if (init?.method === "POST") {
      creates++; rows = [replacement];
      return delayed ? new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          void creationBody.promise.then((value) => { controller.enqueue(new TextEncoder().encode(value)); controller.close(); });
        },
      })) : Response.json(replacement);
    }
    if (new URL(url).pathname === "/api/connectors/catalog") return failing ?
      Response.json({ detail: "Confirmation unavailable" }, { status: 503 }) : Response.json(rows);
    return Response.json([]);
  }));
  mount();
  await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
  fireEvent.click(screen.getByTitle("Remove connector"));
  await waitFor(() => expect(deletes).toBe(1));
  createCustom();
  if (delayed) {
    await waitFor(() => expect(creates).toBe(1));
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh catalog" }));
  } else expect(await screen.findByRole("status")).toHaveTextContent("Connector saved.");
  await screen.findByText(replacement.display_name);
  await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
  failing = true;
  await act(async () => deletion.resolve(new Response(null, { status: 204 })));
  expect(await screen.findByRole("alert")).toHaveTextContent("Confirmation unavailable");
  expect(useSessionStore.getState().connectorCatalog).toEqual([replacement]);
  expect(screen.getByText(replacement.display_name)).toBeVisible();
  if (delayed) {
    await act(async () => creationBody.resolve(JSON.stringify(replacement)));
    await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.loading).toBe(false));
    expect(useSessionStore.getState().connectorCatalog).toEqual([replacement]);
  }
  expect(creates).toBe(1);
  expect(deletes).toBe(1);
});

it("a replaced definition's late DELETE confirms without removing its new installation", async () => {
  const deletion = deferred<Response>();
  const replacement = { ...custom, display_name: "Replacement connector" };
  const installed: ConnectorInstallationInfo = {
    id: "new-installation", kind: custom.kind, label: "Reconnected account",
    auth_type: "oauth", external_account_id: "new-account", scopes: [],
    enable_by_default: false, needs_reconnect: false, token_expires_at: null,
    last_refresh_error_code: null, created_at: "now",
  };
  let rows: ConnectorCatalogEntry[] = [custom];
  let installations: ConnectorInstallationInfo[] = [];
  let failing = false;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "DELETE") { rows = []; return deletion.promise; }
    if (init?.method === "POST") { rows = [replacement]; installations = [installed]; return Response.json(replacement); }
    const path = new URL(url).pathname;
    if (failing) return Response.json({ detail: "Confirmation unavailable" }, { status: 503 });
    return Response.json(path === "/api/connectors/catalog" ? rows : installations);
  }));
  const view = mount();
  await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
  fireEvent.click(screen.getByTitle("Remove connector"));
  createCustom();
  expect(await screen.findByRole("status")).toHaveTextContent("Connector saved.");
  view.unmount();
  mount();
  await waitFor(() => expect(useSessionStore.getState().connectorInstallations).toEqual([installed]));
  await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
  failing = true;
  await act(async () => deletion.resolve(new Response(null, { status: 204 })));
  await waitFor(() => expect(useSessionStore.getState().connectorRead.error).toBe("Confirmation unavailable"));
  expect(useSessionStore.getState().connectorInstallations).toEqual([installed]);
  expect(useSessionStore.getState().connectorCatalog).toEqual([replacement]);
});

it("uses creation confirmation rather than request order when DELETE commits before an earlier POST", async () => {
  const creation = deferred<Response>();
  const deletion = deferred<Response>();
  const replacement = { ...custom, display_name: "Replacement connector" };
  let rows: ConnectorCatalogEntry[] = [custom];
  let creates = 0;
  let deletes = 0;
  let failing = false;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") { creates++; return creation.promise; }
    if (init?.method === "DELETE") { deletes++; rows = []; return deletion.promise; }
    if (new URL(url).pathname === "/api/connectors/catalog") return failing ?
      Response.json({ detail: "Confirmation unavailable" }, { status: 503 }) : Response.json(rows);
    return Response.json([]);
  }));
  mount();
  await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
  createCustom();
  await waitFor(() => expect(creates).toBe(1));
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  fireEvent.click(screen.getByTitle("Remove connector"));
  await waitFor(() => expect(deletes).toBe(1));
  rows = [replacement];
  await act(async () => creation.resolve(Response.json(replacement)));
  await screen.findByText(replacement.display_name);
  await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
  failing = true;
  await act(async () => deletion.resolve(new Response(null, { status: 204 })));
  expect(await screen.findByRole("alert")).toHaveTextContent("Confirmation unavailable");
  expect(useSessionStore.getState().connectorCatalog).toEqual([replacement]);
  expect(creates).toBe(1);
  expect(deletes).toBe(1);
});

it("a refused same-slug creation does not revoke an earlier DELETE's publication rights", async () => {
  const deletion = deferred<Response>();
  let deleted = false;
  let creates = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "DELETE") { deleted = true; return deletion.promise; }
    if (init?.method === "POST") { creates++; return Response.json({ detail: "Creation refused" }, { status: 409 }); }
    if (new URL(url).pathname === "/api/connectors/catalog") return deleted ?
      Response.json({ detail: "Confirmation unavailable" }, { status: 503 }) : Response.json([custom]);
    return Response.json([]);
  }));
  mount();
  await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
  fireEvent.click(screen.getByTitle("Remove connector"));
  createCustom();
  await screen.findByText("Creation refused");
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  await act(async () => deletion.resolve(new Response(null, { status: 204 })));
  expect(await screen.findByRole("alert")).toHaveTextContent("Confirmation unavailable");
  expect(useSessionStore.getState().connectorCatalog).toEqual([]);
  expect(creates).toBe(1);
});

it.each(["create", "configure", "delete"] as const)(
  "%s reports the committed write separately from a failed confirmation and retries only GET", async (kind) => {
    const builtin = { ...custom, custom: false, available: false };
    let rows: ConnectorCatalogEntry[] = kind === "create" ? [] : [kind === "configure" ? builtin : custom];
    let reads = 0;
    let writes = 0;
    let failing = true;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const path = new URL(url).pathname;
      if (init?.method && init.method !== "GET") {
        writes++;
        rows = kind === "delete" ? [] : [kind === "configure" ? { ...builtin, available: true } : custom];
        return kind === "delete" ? new Response(null, { status: 204 }) : Response.json(kind === "configure" ? { configured: true } : custom);
      }
      if (path === "/api/connectors/catalog") {
        if (++reads > 1 && failing) return Response.json({ detail: "Confirmation unavailable" }, { status: 503 });
        return Response.json(rows);
      }
      if (path.endsWith("/oauth-client")) return Response.json({ client_id: "client" });
      return Response.json([]);
    }));
    mount();
    await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
    if (kind === "create") createCustom();
    else if (kind === "delete") fireEvent.click(screen.getByTitle("Remove connector"));
    else configure();
    expect(await screen.findByRole("status")).toHaveTextContent(kind === "delete" ? "Connector removed." : "Connector saved.");
    expect(await screen.findByRole("alert")).toHaveTextContent("Catalog not synchronized: Confirmation unavailable");
    expect(useSessionStore.getState().connectorCatalog).toEqual(rows);
    expect(useSessionStore.getState().connectorCatalogRead).toMatchObject({ loading: false, stale: true });
    failing = false;
    fireEvent.click(screen.getByRole("button", { name: "Refresh catalog" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false);
    expect(writes).toBe(1);
  });

it("a late configuration receipt cannot override a newer accepted catalog when its confirmation fails", async () => {
  const builtin = { ...custom, custom: false, available: false };
  const newer = { ...builtin, display_name: "Updated definition" };
  const save = deferred<Response>();
  let reads = 0;
  let writes = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (init?.method === "PUT") { writes++; return save.promise; }
    if (path === "/api/connectors/catalog") {
      reads++;
      return reads === 1 ? Response.json([builtin]) : reads === 2 ? Response.json([newer]) :
        Response.json({ detail: "Confirmation unavailable" }, { status: 503 });
    }
    if (path.endsWith("/oauth-client")) return Response.json({ client_id: "client" });
    return Response.json([]);
  }));
  mount();
  await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
  configure();
  await waitFor(() => expect(writes).toBe(1));
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  fireEvent.click(screen.getByRole("button", { name: "Refresh catalog" }));
  await screen.findByText(newer.display_name);
  await act(async () => save.resolve(Response.json({ configured: true })));
  expect(await screen.findByRole("alert")).toHaveTextContent("Confirmation unavailable");
  expect(useSessionStore.getState().connectorCatalog).toEqual([newer]);
  expect(screen.getByRole("button", { name: "Set up" })).toBeEnabled();
  expect(writes).toBe(1);
});

it.each((["read", "create", "configure", "delete"] as const).flatMap((operation) =>
  (["token", "account"] as const).map((boundary) => ({ operation, boundary }))))(
  "retires an old $operation across a $boundary change without publishing into the new scope", async ({ operation, boundary }) => {
    const old = deferred<Response>();
    const newer = { ...custom, display_name: "New scope catalog", available: false };
    let newScope = false;
    let newReads = 0;
    let writes = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const path = new URL(url).pathname;
      if (init?.method && init.method !== "GET") { writes++; return old.promise; }
      if (path === "/api/connectors/catalog") {
        if (newScope) { newReads++; return Response.json([newer]); }
        if (operation === "read") return old.promise;
        return Response.json(operation === "create" ? [] : [{ ...custom, available: operation !== "configure" }]);
      }
      if (path.endsWith("/oauth-client")) return Response.json({ client_id: "client" });
      return Response.json([]);
    }));
    mount();
    if (operation !== "read") {
      await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
      if (operation === "create") createCustom();
      else if (operation === "configure") configure();
      else fireEvent.click(screen.getByTitle("Remove connector"));
      await waitFor(() => expect(writes).toBe(1));
    }
    newScope = true;
    act(() => {
      if (boundary === "token") useSessionStore.getState().setToken("new-token");
      else useSessionStore.setState({ accountGeneration: useSessionStore.getState().accountGeneration + 1 });
    });
    await waitFor(() => expect(useSessionStore.getState().connectorCatalog).toEqual([newer]));
    await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
    const confirmed = newReads;
    await act(async () => old.resolve(operation === "delete" ? new Response(null, { status: 204 }) :
      Response.json(operation === "configure" ? { configured: true } : operation === "read" ? [custom] : custom)));
    expect(useSessionStore.getState().connectorCatalog).toEqual([newer]);
    expect(useSessionStore.getState().connectorCatalogRead).toMatchObject({ loading: false, stale: false, error: null });
    expect(newReads).toBe(confirmed);
    expect(screen.queryByText("Connector saved.")).toBeNull();
    expect(screen.queryByText("Connector removed.")).toBeNull();
  });

it("accepts same-account facts after remount without closing a new form draft", async () => {
  const creation = deferred<Response>();
  const authoritative = { ...custom, display_name: "Authoritative definition" };
  let rows: ConnectorCatalogEntry[] = [];
  let writes = 0;
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") { writes++; return creation.promise; }
    if (new URL(url).pathname === "/api/connectors/catalog") { reads++; return Response.json(rows); }
    return Response.json([]);
  }));
  const view = mount();
  await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
  createCustom();
  await waitFor(() => expect(writes).toBe(1));
  view.unmount();
  mount();
  await waitFor(() => expect(useSessionStore.getState().connectorCatalogRead.stale).toBe(false));
  fireEvent.click(screen.getByRole("button", { name: /Add custom connector/ }));
  fireEvent.change(screen.getByLabelText("Slug"), { target: { value: "new-draft" } });
  const confirmed = reads;
  rows = [authoritative];
  await act(async () => creation.resolve(Response.json(custom)));
  await waitFor(() => expect(reads).toBe(confirmed + 1));
  await waitFor(() => expect(useSessionStore.getState().connectorCatalog).toEqual([authoritative]));
  expect(screen.getByLabelText("Slug")).toHaveValue("new-draft");
  expect(screen.queryByText("Connector saved.")).toBeNull();
  expect(screen.queryByText("Creating…")).toBeNull();
  expect(writes).toBe(1);
});

it("bounds catalog response-body acquisition and leaves a read-only retry after timeout", async () => {
  vi.useFakeTimers();
  const body = deferred<string>();
  let reads = 0;
  let writes = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method && init.method !== "GET") writes++;
    if (new URL(url).pathname === "/api/connectors/catalog") {
      if (++reads > 1) return Response.json([custom]);
      return new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          void body.promise.then((value) => { controller.enqueue(new TextEncoder().encode(value)); controller.close(); });
        },
      }));
    }
    return Response.json([]);
  }));
  mount();
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(screen.getByRole("alert")).toHaveTextContent("Connector catalog timed out after 30 seconds");
  expect(useSessionStore.getState().connectorCatalog).toEqual([custom]);
  expect(screen.getByRole("button", { name: "Refresh catalog" })).toBeEnabled();
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); body.resolve("[]"); });
  expect(reads).toBe(1);
  expect(useSessionStore.getState().connectorCatalog).toEqual([custom]);
  expect(screen.getByRole("alert")).toHaveTextContent("timed out");
  fireEvent.click(screen.getByRole("button", { name: "Refresh catalog" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(useSessionStore.getState().connectorCatalogRead).toMatchObject({ loading: false, stale: false });
  expect(reads).toBe(2);
  expect(writes).toBe(0);
});

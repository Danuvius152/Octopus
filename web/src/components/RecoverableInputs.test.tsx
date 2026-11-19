import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ClientSubmissionStatus, ClientSubmissionSummary } from "../api";
import { useSessionStore } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";
import { deferred } from "../test-support/deferred";
import { RecoverableInputs } from "./RecoverableInputs";

const execution = { state: "withdrawn" as const, delivery_state: "none" as const, state_version: 2, execution_owner_request_id: "request", recoverable_input: true };
const item: ClientSubmissionSummary = { client_request_id: "original", request_id: "request", operation: "message", execution, preview: "Saved original", attachment_count: 0 };
const saved: ClientSubmissionStatus = { session_id: "s", client_request_id: "original", request_id: "request", accepted: true,
  operation: "message", execution, input: { kind: "message", content: "Saved original in full 中文", attachment_ids: [], attachments: [] } };

it("distinguishes native acceptance from processing when execution is unknown", async () => {
  const native = { ...item, execution: { ...execution, state: "unknown", recoverable_input: false,
    native_inputs: [{ version: 1, transport: "codex-app-server", attempt_id: "attempt", input_id: "request",
      rpc_id: "rpc-1", thread_id: "thread-1", turn_id: "turn-1", state: "accepted", controller: "confirmed" }] } };
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ items: [native] })));
  render(<RecoverableInputs sessionId="s" connected disabled={false} onRestore={vi.fn()} />);
  expect(await screen.findByText(/Native input was accepted; model processing and the result remain unconfirmed/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Restore to input" })).toBeNull();
});

it("still reports an unknown execution when its steer acceptance is confirmed", async () => {
  const native = { ...item, execution: { ...execution, state: "unknown", recoverable_input: false,
    native_inputs: [{ kind: "steer", state: "accepted" }] } };
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ items: [native] })));
  render(<RecoverableInputs sessionId="s" connected disabled={false} onRestore={vi.fn()} />);
  expect(await screen.findByText("Execution result unknown")).toBeTruthy();
  expect(screen.queryByText("Accepted by runtime · processing unconfirmed")).toBeNull();
  expect(screen.queryByRole("button", { name: "Restore to input" })).toBeNull();
});

it.each(["empty", "pending", "unknown"])("clears a %s notice once the server confirms steer acceptance", async (previous) => {
  let accepted = false;
  const fetch = vi.fn(async () => Response.json({ items: previous === "empty" || accepted ? [] : [{ ...item, execution: {
    ...execution, state: "running", recoverable_input: false,
    native_inputs: [{ kind: "steer", state: previous === "pending" ? "not_sent" : "unknown" }],
  } }] }));
  vi.stubGlobal("fetch", fetch);
  render(<RecoverableInputs sessionId="s" connected disabled={false} onRestore={vi.fn()} />);
  if (previous === "empty") await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  else await screen.findByText(previous === "pending" ? "Added to active turn · acceptance pending" : "Execution result unknown");
  accepted = true;
  act(() => handleWsMessage({ type: "dispatch_state", session_id: "s", dispatch_state: {
    generation: "steer-evidence-service", revision: 10, paused: false, active_request_id: "root", active_operation: "message" as const, items: [],
  } }));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByRole("region", { name: "Saved inputs" })).toBeNull());
  expect(screen.queryByRole("button", { name: "Restore to input" })).toBeNull();
});

beforeEach(() => useSessionStore.setState({ token: "recovery-test", activeSessionId: "s", dispatchStates: {}, dispatchGeneration: null }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function mockFetch(detail = saved) {
  const fetch = vi.fn(async (url: string) => Response.json(url.endsWith("/original") ? detail : { items: [item], next_cursor: null }));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

it("loads and reviews input without executing or restoring it; restore is explicit", async () => {
  const fetch = mockFetch();
  const restore = vi.fn();
  render(<RecoverableInputs sessionId="s" connected disabled={false} onRestore={restore} />);
  await screen.findByText("Not executed · input saved");
  expect(restore).not.toHaveBeenCalled();
  expect(fetch).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "Review saved input" }));
  await screen.findByText("Saved original in full 中文");
  expect(restore).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Restore to input" }));
  await waitFor(() => expect(restore).toHaveBeenCalledWith(saved, []));
});

it("has no restore action for an execution whose result is unknown", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ items: [{ ...item, execution: { ...execution, state: "unknown", recoverable_input: false } }] })));
  render(<RecoverableInputs sessionId="s" connected disabled={false} onRestore={vi.fn()} />);
  await screen.findByText("Execution result unknown");
  expect(screen.queryByRole("button", { name: "Restore to input" })).toBeNull();
});

it("reviews the unknown reason and original attachment evidence without restoring", async () => {
  const detail = { ...saved, message: "Delivery to the model was not acknowledged.",
    execution: { ...execution, state: "unknown" as const, recoverable_input: false, reason_code: "steer_delivery_unknown" },
    input: { ...saved.input!, attachment_ids: ["file"], attachments: [{
      id: "file", filename: "Original evidence.txt", size: 17, mime_type: "text/plain", sha256: "original-digest",
    }] } };
  const fetch = vi.fn(async (url: string, _init?: RequestInit) => Response.json(url.endsWith("/original") ? detail
    : { items: [{ ...item, execution: detail.execution, attachment_count: 1 }] }));
  vi.stubGlobal("fetch", fetch);
  const restore = vi.fn();
  render(<RecoverableInputs sessionId="s" connected disabled={false} onRestore={restore} />);
  fireEvent.click(await screen.findByRole("button", { name: "Review saved input" }));
  await screen.findByText("Delivery to the model was not acknowledged.");
  expect(screen.getByText(/Original evidence.txt/)).toBeVisible();
  expect(restore).not.toHaveBeenCalled();
  expect(fetch.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
});

it("keeps complete input when an attachment fails and clears a Dismiss failure on text-only restore", async () => {
  const attachment = { id: "abcdef123456", filename: "required.txt", size: 7, mime_type: "text/plain", sha256: "digest" };
  const detail = { ...saved, input: { ...saved.input!, attachment_ids: [attachment.id], attachments: [attachment] } };
  let listReads = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "DELETE") return new Response(null, { status: 503 });
    if (url.includes("/attachments/")) return new Response("Changed", { status: 409 });
    if (url.endsWith("/original")) return Response.json(detail);
    return Response.json({ items: ++listReads === 1 ? [{ ...item, attachment_count: 1 }] : [] });
  }));
  const restore = vi.fn();
  render(<RecoverableInputs sessionId="s" connected disabled={false} onRestore={restore} />);
  fireEvent.click(await screen.findByRole("button", { name: "Restore to input" }));
  await screen.findByRole("alert");
  expect(restore).not.toHaveBeenCalled();
  expect(screen.getByText("Saved original in full 中文")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Dismiss notice" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Couldn't dismiss saved input (503)"));
  fireEvent.click(screen.getByRole("button", { name: "Restore text and re-attach files" }));
  expect(restore).toHaveBeenCalledExactlyOnceWith(detail, []);
  expect(screen.queryByRole("alert")).toBeNull();
  reviseDispatch(10);
  await waitFor(() => expect(screen.queryByRole("region", { name: "Saved inputs" })).toBeNull());
});

it("restores the complete saved text and original attachment", async () => {
  const data = "saved file bytes";
  const attachment = { id: "file", filename: "original.txt", size: data.length, mime_type: "text/plain", sha256: "digest" };
  const detail = { ...saved, input: { ...saved.input!, attachment_ids: [attachment.id], attachments: [attachment] } };
  const attachmentResponse = new Response(data);
  vi.spyOn(attachmentResponse, "blob").mockResolvedValue(new Blob([data], { type: "text/plain" }));
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.endsWith("/original/attachments/file")) return attachmentResponse;
    if (url.endsWith("/original")) return Response.json(detail);
    return Response.json({ items: [{ ...item, attachment_count: 1 }] });
  }));
  const restore = vi.fn<(status: ClientSubmissionStatus, files: File[]) => void>();
  render(<RecoverableInputs sessionId="s" connected disabled={false} onRestore={restore} />);
  fireEvent.click(await screen.findByRole("button", { name: "Restore to input" }));
  await waitFor(() => expect(restore).toHaveBeenCalledTimes(1));
  const [[restored, files]] = restore.mock.calls;
  expect(restored.input).toMatchObject({ content: "Saved original in full 中文", attachment_ids: ["file"] });
  expect(files).toHaveLength(1);
  expect(files[0]).toMatchObject({ name: "original.txt", size: data.length, type: "text/plain" });
  const contents = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsText(files[0]);
  });
  expect(contents).toBe(data);
});

it("does not replace a draft edited while saved input is downloading", async () => {
  let resolve!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("/original") ? new Promise<Response>((finish) => { resolve = finish; })
    : Promise.resolve(Response.json({ items: [item] }))));
  const oldRestore = vi.fn();
  const { rerender } = render(<RecoverableInputs sessionId="s" connected disabled={false} onRestore={oldRestore} />);
  fireEvent.click(await screen.findByRole("button", { name: "Restore to input" }));
  const editedRestore = vi.fn();
  rerender(<RecoverableInputs sessionId="s" connected disabled onRestore={editedRestore} />);
  await act(async () => resolve(Response.json(saved)));
  await screen.findByText(/conversation or draft changed/);
  expect(oldRestore).not.toHaveBeenCalled();
  expect(editedRestore).not.toHaveBeenCalled();
});

it("follows the server cursor and dismisses only the selected notice", async () => {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "DELETE") return new Response(null, { status: 204 });
    return Response.json(url.includes("before=20") ? { items: [{ ...item, client_request_id: "older", preview: "Older saved input" }], next_cursor: null }
      : { items: [item], next_cursor: 20 });
  });
  vi.stubGlobal("fetch", fetch);
  render(<RecoverableInputs sessionId="s" connected disabled={false} onRestore={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Load more saved inputs" }));
  await screen.findByText("Older saved input");
  fireEvent.click(screen.getAllByRole("button", { name: "Dismiss notice" })[0]);
  await waitFor(() => expect(screen.queryByText("Saved original")).toBeNull());
  expect(screen.getByText("Older saved input")).toBeVisible();
});

function reviseDispatch(revision: number) {
  act(() => handleWsMessage({ type: "dispatch_state", session_id: "s", dispatch_state: {
    generation: "recovery-service", revision, paused: false,
    active_request_id: "root", active_operation: "message", items: [],
  } }));
}

it("replaces the refresh started by dispatch before the Dismiss response", async () => {
  const dismissal = deferred<Response>();
  const staleRefresh = deferred<Response>();
  const freshRefresh = deferred<Response>();
  let refreshes = 0;
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "DELETE") return dismissal.promise;
    if (++refreshes === 1) return Response.json({ items: [item] });
    return refreshes === 2 ? staleRefresh.promise : freshRefresh.promise;
  }));
  render(<RecoverableInputs sessionId="s" connected disabled={false} onRestore={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Dismiss notice" }));
  reviseDispatch(10);
  expect(refreshes).toBe(2);
  await act(async () => dismissal.resolve(new Response(null, { status: 204 })));
  expect(screen.queryByRole("region", { name: "Saved inputs" })).toBeNull();
  expect(refreshes).toBe(3);

  await act(async () => staleRefresh.resolve(Response.json({
    items: [{ ...item, client_request_id: "retired", preview: "Retired first page" }],
  })));
  expect(screen.queryByRole("region", { name: "Saved inputs" })).toBeNull();
  await act(async () => freshRefresh.resolve(Response.json({
    items: [{ ...item, client_request_id: "latest", preview: "New saved input" }],
  })));
  expect(screen.getByText("New saved input")).toBeVisible();
  expect(screen.queryByText("Saved original")).toBeNull();
});

it.each([
  { name: "same cursor, homepage first", cursor: 20, moreFirst: false, oldStatus: 200 },
  { name: "same cursor, pagination first", cursor: 20, moreFirst: true, oldStatus: 200 },
  { name: "changed cursor, obsolete rows", cursor: 10, moreFirst: false, oldStatus: 200 },
  { name: "changed cursor, obsolete error", cursor: 10, moreFirst: false, oldStatus: 503 },
  { name: "changed cursor, pagination first", cursor: 10, moreFirst: true, oldStatus: 200 },
])("settles new reads after Dismiss with $name", async ({ cursor, moreFirst, oldStatus }) => {
  const staleRefresh = deferred<Response>();
  const staleMore = deferred<Response>();
  const dismissal = deferred<Response>();
  const freshRefresh = deferred<Response>();
  const freshMore = deferred<Response>();
  const nextMore = deferred<Response>();
  let refreshes = 0;
  let pages = 0;
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "DELETE") return dismissal.promise;
    if (url.includes("before=10")) return nextMore.promise;
    if (url.includes("before=5")) return Response.json({
      items: [{ ...item, client_request_id: "third-page", preview: "Another fresh page" }], next_cursor: null,
    });
    if (url.includes("before=20")) return ++pages === 1 ? staleMore.promise : freshMore.promise;
    if (++refreshes === 1) return Response.json({ items: [item], next_cursor: 20 });
    return refreshes === 2 ? staleRefresh.promise : freshRefresh.promise;
  });
  vi.stubGlobal("fetch", fetch);
  render(<RecoverableInputs sessionId="s" connected disabled={false} onRestore={vi.fn()} />);
  const loadMore = await screen.findByRole("button", { name: "Load more saved inputs" });

  reviseDispatch(10);
  expect(refreshes).toBe(2);
  fireEvent.click(loadMore);
  expect(pages).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: "Dismiss notice" }));
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/original/recovery"), expect.objectContaining({ method: "DELETE" }));
  expect(screen.getByText("Saved original")).toBeVisible();
  await act(async () => dismissal.resolve(new Response(null, { status: 204 })));
  expect(screen.queryByText("Saved original")).toBeNull();

  await waitFor(() => expect(screen.getByRole("button", { name: "Load more saved inputs" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Load more saved inputs" }));
  expect(pages).toBe(2);
  await act(async () => {
    staleRefresh.resolve(Response.json({ items: [item], next_cursor: 20 }));
    staleMore.resolve(Response.json({ items: [{ ...item, client_request_id: "stale", preview: "Retired page" }], next_cursor: null }));
  });
  expect(screen.queryByText("Saved original")).toBeNull();
  expect(screen.queryByText("Retired page")).toBeNull();
  expect(screen.getByRole("button", { name: "Load more saved inputs" })).toBeDisabled();
  const pageResponse = oldStatus === 200 ? Response.json({
    items: [{ ...item, client_request_id: "fresh-more", preview: "Page read from cursor 20" }], next_cursor: moreFirst ? 5 : null,
  }) : new Response(null, { status: oldStatus });
  if (moreFirst) {
    await act(async () => freshMore.resolve(pageResponse));
    expect(screen.getByText("Page read from cursor 20")).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "Load more saved inputs" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Load more saved inputs" }));
    expect(await screen.findByText("Another fresh page")).toBeVisible();
  }
  await act(async () => freshRefresh.resolve(Response.json({
    items: [{ ...item, client_request_id: "remaining", preview: "Current first page" }], next_cursor: cursor,
  })));
  expect(screen.getByText("Current first page")).toBeVisible();
  if (cursor === 20) {
    if (!moreFirst) await act(async () => freshMore.resolve(pageResponse));
    expect(screen.getByText("Page read from cursor 20")).toBeVisible();
    if (moreFirst) expect(screen.getByText("Another fresh page")).toBeVisible();
  } else {
    await waitFor(() => expect(screen.getByRole("button", { name: "Load more saved inputs" })).toBeEnabled());
    if (!moreFirst) {
      const oldRead = fetch.mock.calls.filter(([url]) => url.includes("before=20")).at(-1);
      expect(oldRead?.[1]?.signal?.aborted).toBe(true);
    }
    fireEvent.click(screen.getByRole("button", { name: "Load more saved inputs" }));
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("before=10"), expect.anything());
    if (!moreFirst) await act(async () => freshMore.resolve(pageResponse));
    expect(screen.queryByText("Page read from cursor 20")).toBeNull();
    expect(screen.queryByText("Another fresh page")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Load more saved inputs" })).toBeDisabled();
    await act(async () => nextMore.resolve(Response.json({
      items: [{ ...item, client_request_id: "correct-page", preview: "Fresh page" }], next_cursor: null,
    })));
    expect(screen.getByText("Fresh page")).toBeVisible();
  }
  expect(screen.queryByText("Saved original")).toBeNull();
});

it.each(["before", "after"])("keeps a paginated notice when a refresh completes %s Dismiss fails", async (order) => {
  const refresh = deferred<Response>();
  const dismissal = deferred<Response>();
  const older = { ...item, client_request_id: "older", preview: "Older saved input" };
  const newItem = { ...item, client_request_id: "new", preview: "New first-page input" };
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "DELETE") return dismissal.promise;
    if (url.includes("before=20")) return Response.json({ items: [older], next_cursor: null });
    if (++reads === 1) return Response.json({ items: [item], next_cursor: 20 });
    if (reads === 2) return refresh.promise;
    return Response.json({ items: [newItem], next_cursor: 20 });
  }));
  render(<RecoverableInputs sessionId="s" connected disabled={false} onRestore={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Load more saved inputs" }));
  const olderNotice = (await screen.findByText("Older saved input")).closest("article")!;
  await waitFor(() => expect(within(olderNotice).getByRole("button", { name: "Dismiss notice" })).toBeEnabled());
  fireEvent.click(within(olderNotice).getByRole("button", { name: "Dismiss notice" }));
  reviseDispatch(10);
  expect(reads).toBe(2);
  if (order === "before") await act(async () => refresh.resolve(Response.json({ items: [newItem], next_cursor: 20 })));
  await act(async () => dismissal.resolve(new Response(null, { status: 503 })));
  expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't dismiss saved input (503)");
  if (order === "after") await act(async () => refresh.resolve(Response.json({ items: [newItem], next_cursor: 20 })));
  expect(screen.getByText("New first-page input")).toBeVisible();
  expect(screen.getByText("Older saved input")).toBeVisible();
  expect(screen.queryByText("Saved original")).toBeNull();
  expect(screen.getByRole("alert")).toHaveTextContent("Couldn't dismiss saved input (503)");
  fireEvent.click(screen.getByRole("button", { name: "Retry loading" }));
  await waitFor(() => expect(screen.queryByText("Older saved input")).toBeNull());
  expect(screen.getByText("New first-page input")).toBeVisible();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("clears a Review failure after a successful list refresh", async () => {
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.endsWith("/original")) return new Response(null, { status: 404 });
    return Response.json({ items: ++reads === 1 ? [item] : [] });
  }));
  render(<RecoverableInputs sessionId="s" connected disabled={false} onRestore={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Review saved input" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't read saved input (404)");
  reviseDispatch(10);
  await waitFor(() => expect(screen.queryByRole("region", { name: "Saved inputs" })).toBeNull());
});

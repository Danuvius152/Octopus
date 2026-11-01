import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ClientSubmissionStatus, ClientSubmissionSummary } from "../api";
import { useSessionStore } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";
import { RecoverableInputs } from "./RecoverableInputs";

const execution = { state: "withdrawn" as const, delivery_state: "none" as const, state_version: 2, execution_owner_request_id: "request", recoverable_input: true };
const item: ClientSubmissionSummary = { client_request_id: "original", request_id: "request", execution, preview: "Saved original", attachment_count: 0 };
const saved: ClientSubmissionStatus = { session_id: "s", client_request_id: "original", request_id: "request", accepted: true,
  execution, input: { content: "Saved original in full 中文", attachment_ids: [], attachments: [] } };

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
    generation: "steer-evidence-service", revision: 10, paused: false, active_request_id: "root", items: [],
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

it("keeps complete input when a required attachment cannot be recovered", async () => {
  const attachment = { id: "abcdef123456", filename: "required.txt", size: 7, mime_type: "text/plain", sha256: "digest" };
  const detail = { ...saved, input: { ...saved.input!, attachment_ids: [attachment.id], attachments: [attachment] } };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.includes("/attachments/") ? new Response("Changed", { status: 409 })
    : Response.json(url.endsWith("/original") ? detail : { items: [{ ...item, attachment_count: 1 }] })));
  const restore = vi.fn();
  render(<RecoverableInputs sessionId="s" connected disabled={false} onRestore={restore} />);
  fireEvent.click(await screen.findByRole("button", { name: "Restore to input" }));
  await screen.findByRole("alert");
  expect(restore).not.toHaveBeenCalled();
  expect(screen.getByText("Saved original in full 中文")).toBeVisible();
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

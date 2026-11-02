/**
 * Renderer tests for BgTaskChip. Covers the four user-visible states
 * (running, completed, failed, cancelled), the on-mount fetch fallback
 * for tasks not yet in the store, expansion → full output reveal,
 * and the cancel button behavior.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";

import { BgTaskChip } from "./BgTaskChip";
import { useSessionStore, type BgTask } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";
import { loadBgTaskSnapshot } from "../lib/hydrateSession";

function makeTask(overrides: Partial<BgTask> = {}): BgTask {
  return {
    id: "t1",
    session_id: "s1",
    command: "yes | head -c 100",
    description: "fill buffer",
    working_dir: "/tmp",
    status: "running",
    exit_code: null,
    stdout: "",
    stderr: "",
    truncated: false,
    started_at: "2026-05-18T00:00:00Z",
    completed_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.setState({ token: "tok", bgTasks: {} });
});

afterEach(() => {
  cleanup();
  useSessionStore.setState({ bgTasks: {} });
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("BgTaskChip", () => {
  it.each(["unknown", null] as const)("keeps newer HTTP list delivery (%s) while an old detail fills output", async (disposition) => {
    const old = makeTask({ status: "completed", exit_code: 0,
      delivery_disposition: "stopped", delivery_error: "Old delivery" });
    let detailResponse!: (response: Response) => void;
    let listResponse!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn()
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { detailResponse = resolve; }))
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { listResponse = resolve; })));
    useSessionStore.getState().upsertBgTask("s1", old);
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    await act(async () => screen.getByRole("button", { expanded: false }).click());
    const list = loadBgTaskSnapshot("s1");
    await act(async () => {
      listResponse(Response.json([{ ...old, delivery_disposition: disposition,
        delivery_error: disposition ? "New delivery" : null }]));
      await list;
    });
    await act(async () => detailResponse(Response.json({ ...old, stdout: "Full HTTP detail" })));
    expect(screen.getByText("Full HTTP detail")).toBeInTheDocument();
    expect(screen.queryByText("Old delivery")).toBeNull();
    if (disposition) expect(screen.getByText("New delivery")).toBeInTheDocument();
    expect(useSessionStore.getState().bgTasks.s1[0]).toMatchObject({
      status: "completed", delivery_disposition: disposition, delivery_error: disposition ? "New delivery" : null,
    });
  });

  it("keeps completed WS facts while a late running detail supplies output", async () => {
    let respond!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { respond = resolve; })));
    useSessionStore.getState().upsertBgTask("s1", makeTask());
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    await act(async () => screen.getByRole("button", { expanded: false }).click());
    act(() => {
      handleWsMessage({ type: "bg_completed", session_id: "s1", task_id: "t1",
        status: "completed", exit_code: 0, completed_at: "finished", truncated: true });
      handleWsMessage({ type: "bg_delivery", session_id: "s1", task_id: "t1",
        delivery_disposition: "unknown", delivery_error: "Later delivery error" });
    });
    await act(async () => respond(Response.json(makeTask({ stdout: "Full original output" }))));
    expect(screen.getByText("bg · completed")).toBeInTheDocument();
    expect(screen.getByText("Full original output")).toBeInTheDocument();
    expect(screen.getByText("Later delivery error")).toBeInTheDocument();
    expect(useSessionStore.getState().bgTasks.s1[0]).toMatchObject({
      status: "completed", exit_code: 0, completed_at: "finished", truncated: true,
    });
  });

  it.each(["unmount", "account", "service"] as const)("discards details after %s invalidates the observation", async (boundary) => {
    let respond!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { respond = resolve; })));
    const view = render(<BgTaskChip sessionId="s1" taskId="t1" />);
    act(() => {
      if (boundary === "unmount") view.unmount();
      else if (boundary === "account") useSessionStore.setState({ accountGeneration: 22 });
      else useSessionStore.setState({ dispatchGeneration: "replacement" });
    });
    await act(async () => respond(Response.json(makeTask({ status: "completed", stdout: "obsolete" }))));
    expect(useSessionStore.getState().bgTasks.s1).toBeUndefined();
  });

  it("accepts newer detail diagnostics after an old list inserted the task", async () => {
    const old = makeTask({ status: "completed", exit_code: 0, delivery_disposition: null, delivery_error: null });
    let respond!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { respond = resolve; })));
    const baseline = useSessionStore.getState().bgTasks.s1 ?? [];
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    act(() => {
      handleWsMessage({ type: "bg_delivery", session_id: "s1", task_id: "t1",
        delivery_disposition: "unknown", delivery_error: "New reason" });
      useSessionStore.getState().setBgTasks("s1", [old], baseline);
    });
    await act(async () => respond(new Response(JSON.stringify({ ...old,
      delivery_disposition: "unknown", delivery_error: "New reason", stdout: "Full fetched output" }), { status: 200 })));
    await waitFor(() => expect(screen.getByText("Full fetched output")).toBeInTheDocument());
    expect(screen.getByText("Return admission unconfirmed")).toBeInTheDocument();
    expect(screen.getByText("New reason")).toBeInTheDocument();
  });

  it.each([
    ["mount", null, "unknown"],
    ["mount", "unknown", null],
    ["expanded", null, "stopped"],
    ["expanded", "unknown", null],
  ] as const)("preserves delivery events across a late %s detail response (%s → %s)", async (entry, oldDisposition, disposition) => {
    const old = makeTask({ status: "completed", exit_code: 0,
      delivery_disposition: oldDisposition, delivery_error: oldDisposition ? "Old reason" : null });
    let respond!: (response: Response) => void;
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => { respond = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    if (entry === "expanded") useSessionStore.getState().upsertBgTask("s1", old);
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    if (entry === "expanded") {
      await act(async () => screen.getByRole("button", { expanded: false }).click());
    }
    expect(fetchMock).toHaveBeenCalledTimes(1);
    act(() => {
      if (entry === "mount") useSessionStore.getState().upsertBgTask("s1", old);
      handleWsMessage({ type: "bg_delivery", session_id: "s1", task_id: "t1",
        delivery_disposition: disposition, delivery_error: disposition ? "New reason" : null });
    });
    await act(async () => respond(new Response(JSON.stringify({ ...old, stdout: "Full fetched output" }), { status: 200 })));
    await waitFor(() => expect(screen.getByText("Full fetched output")).toBeInTheDocument());
    if (disposition) {
      expect(screen.getByText(disposition === "unknown" ? "Return admission unconfirmed" : "Automatic return stopped")).toBeInTheDocument();
      expect(screen.getByText("New reason")).toBeInTheDocument();
    } else {
      expect(screen.queryByText("Return admission unconfirmed")).toBeNull();
    }
    expect(screen.queryByText("Old reason")).toBeNull();
  });

  it.each([
    ["unknown", "Return admission unconfirmed"],
    ["stopped", "Automatic return stopped"],
  ] as const)("keeps command success separate from %s return admission", (disposition, label) => {
    useSessionStore.getState().upsertBgTask("s1", makeTask({ status: "completed", exit_code: 0 }));
    handleWsMessage({
      type: "bg_delivery", session_id: "s1", task_id: "t1",
      delivery_disposition: disposition, delivery_error: "Original return reason",
    });
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    expect(screen.getByText("bg · completed")).toBeInTheDocument();
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.getByText("Original return reason")).toBeInTheDocument();
    act(() => handleWsMessage({
      type: "bg_delivery", session_id: "s1", task_id: "t1",
      delivery_disposition: null, delivery_error: null,
    }));
    expect(screen.queryByText(label)).toBeNull();
    expect(screen.getByText("bg · completed")).toBeInTheDocument();
  });

  it("shows the live task limits and the actual timeout reason", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 404 })));
    handleWsMessage({
      type: "bg_started", session_id: "s1", task_id: "t1", command: "sleep 10",
      description: "quiet phase", started_at: "2026-10-13T00:00:00Z",
      idle_timeout_seconds: 60, timeout_seconds: 1800,
    });
    handleWsMessage({
      type: "bg_completed", session_id: "s1", task_id: "t1", status: "failed",
      exit_code: -15, stop_reason: "idle_timeout", idle_timeout_seconds: 60,
      timeout_seconds: 1800, completed_at: "2026-10-13T00:01:01Z",
    });
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    expect(screen.getByText("Stopped after 60s without output")).toBeInTheDocument();
    await act(async () => screen.getByRole("button", { expanded: false }).click());
    expect(screen.getByText("Idle timeout: 60s · Total timeout: 1800s")).toBeInTheDocument();
  });

  it.each([
    [0, 2, "Idle timeout: disabled · Total timeout: 2s"],
    [null, null, "Idle timeout: unknown · Total timeout: unknown"],
  ] as const)("reads limits from task details without inventing missing history", async (idle, total, label) => {
    const task = makeTask({ status: "failed", exit_code: -15,
      idle_timeout_seconds: idle, timeout_seconds: total, stop_reason: null });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(task), { status: 200 })));
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    await waitFor(() => expect(screen.getByText(/bg · failed/i)).toBeInTheDocument());
    await act(async () => screen.getByRole("button", { expanded: false }).click());
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.getByText("Stop reason not recorded")).toBeInTheDocument();
  });

  it("shows a loading placeholder when the task isn't in the store yet", () => {
    // fetch will return null (404-ish); chip stays in placeholder state.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response("not found", { status: 404 })
      )
    );
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    expect(screen.getByText(/loading bg task/i)).toBeInTheDocument();
  });

  it("renders the running state with description + cancel button", () => {
    // Stub fetch to return an empty JSON object — the chip's mount-time
    // refetch shouldn't crash the test; we already pre-seeded the store.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 }))
    );
    useSessionStore.getState().upsertBgTask("s1", makeTask());
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    expect(screen.getByText(/bg · running/i)).toBeInTheDocument();
    expect(screen.getByText("fill buffer")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /cancel/i })
    ).toBeInTheDocument();
  });

  it("renders the completed state without a cancel button", () => {
    // Stub fetch to return an empty JSON object — the chip's mount-time
    // refetch shouldn't crash the test; we already pre-seeded the store.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 }))
    );
    useSessionStore.getState().upsertBgTask(
      "s1",
      makeTask({ status: "completed", exit_code: 0 })
    );
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    expect(screen.getByText(/bg · completed/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /cancel/i })).toBeNull();
  });

  it("shows an exit-code badge for failed tasks", () => {
    // Stub fetch to return an empty JSON object — the chip's mount-time
    // refetch shouldn't crash the test; we already pre-seeded the store.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 }))
    );
    useSessionStore.getState().upsertBgTask(
      "s1",
      makeTask({ status: "failed", exit_code: 7 })
    );
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    expect(screen.getByText(/bg · failed/i)).toBeInTheDocument();
    expect(screen.getByText(/exit 7/i)).toBeInTheDocument();
  });

  it("expanding shows full stdout/stderr panes", async () => {
    // Stub fetch to return an empty JSON object — the chip's mount-time
    // refetch shouldn't crash the test; we already pre-seeded the store.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 }))
    );
    useSessionStore.getState().upsertBgTask(
      "s1",
      makeTask({
        status: "completed",
        exit_code: 0,
        stdout: "line one\nline two\nline three",
        stderr: "",
      })
    );
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    // Last line is shown as the collapsed preview.
    expect(screen.getByText("line three")).toBeInTheDocument();
    // Expand.
    const trigger = screen.getByRole("button", { expanded: false });
    act(() => trigger.click());
    await waitFor(() =>
      expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument()
    );
    expect(screen.getByText(/stdout/i)).toBeInTheDocument();
    expect(screen.getByText(/line one/)).toBeInTheDocument();
  });

  it("clicking cancel POSTs to the cancel endpoint", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // The cancel POST is the URL we want to assert on.
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/cancel") && init?.method === "POST") {
        return new Response(JSON.stringify({ cancelled: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      // Any other fetch (the initial chip metadata refetch) is a no-op.
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    useSessionStore.getState().upsertBgTask("s1", makeTask());
    render(<BgTaskChip sessionId="s1" taskId="t1" />);

    const cancelBtn = screen.getByRole("button", { name: /cancel/i });
    act(() => cancelBtn.click());

    await waitFor(() => {
      const cancelCall = fetchMock.mock.calls.find((args) => {
        const [url, init] = args as [
          RequestInfo | URL,
          RequestInit | undefined,
        ];
        return (
          typeof url === "string" &&
          url.includes("/bg-tasks/t1/cancel") &&
          init?.method === "POST"
        );
      });
      expect(cancelCall).toBeDefined();
    });
  });

  it("fetches the task on mount when it isn't already in the store", async () => {
    const fetched: BgTask = makeTask({ status: "completed", exit_code: 0 });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify(fetched), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      )
    );
    render(<BgTaskChip sessionId="s1" taskId="t1" />);
    await waitFor(() =>
      expect(screen.getByText(/bg · completed/i)).toBeInTheDocument()
    );
  });
});

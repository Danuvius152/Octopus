/**
 * The Monitor page.
 *
 * The case it exists for: a connector that fails every day must be visible as
 * data, not only as a log line nobody greps (docs/post-mortems/2026-09-25-polish-pass.md).
 * So the tests that matter are the ones where something IS wrong and the page
 * says so — and the one where nothing has been recorded yet, which must read as
 * "nothing recorded" rather than as a clean bill of health.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { MonitorPage } from "./MonitorPage";
import { useSessionStore } from "../stores/sessionStore";

const overview = (o: Record<string, unknown> = {}) => ({
  window: "24h",
  counts: [{ kind: "turn", n: 12, failures: 1 }],
  turns: [
    {
      backend: "claude-code",
      n: 12,
      ok_n: 11,
      avg_ms: 4200.0,
      max_ms: 91000.0,
      p50_ms: 3100.0,
      p95_ms: 88000.0,
      p99_ms: 91000.0,
    },
  ],
  errors: [{ kind: "connector_call", error_code: "connector_unavailable", n: 11, last_seen: 1758800000 }],
  resources: [{ metric: "mcp_sidecar_count", n: 60, avg: 0, peak: 0, latest: 0 }],
  ...o,
});

function mockFetch(payload: unknown, ok = true, status = 200) {
  return vi.fn().mockResolvedValue({
    ok,
    status,
    json: async () => payload,
  } as Response);
}

it("shows daily metrics without native phase and counter diagnostics", async () => {
  vi.stubGlobal("fetch", mockFetch(overview({
    codex_runs: [{ request_id: "native-request", input_state: "accepted", resource_state: "proven",
      preparation_ms: 120, root_active_ms: 2000, retirement_ms: 5000, metrics_unavailable: 1 }],
    codex_native_usage: [{ native_thread_id: "native-thread", historical: 1, total_tokens: 987 }],
    codex_steers: [{ input_id: "native-input", ack_ms: 15, state: "accepted" }],
    codex_retirement: [{ request_id: "native-request", phase: "graceful_exit", grace_ms: 2000, elapsed_ms: 15 }],
  })));
  render(<MonitorPage />);
  await waitFor(() => expect(screen.getByText(/12 turns/)).toBeTruthy());
  for (const title of ["Native execution phases", "Native counters and input timing",
    "Provider cumulative snapshots", "Steer acceptance timing", "Owner budget clocks",
    "Retirement grace phases"]) {
    expect(screen.queryByText(title)).toBeNull();
  }
  expect(screen.queryByText("native-request")).toBeNull();
  expect(screen.getByText("Recent Codex turns")).toBeTruthy();
  expect(screen.getByText("Connector logical resource requests")).toBeTruthy();
  expect(screen.getByText("resources")).toBeTruthy();
});

beforeEach(() => {
  useSessionStore.setState({ token: "t" });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("MonitorPage", () => {
  it("separates logical resource requests from historical failure samples", async () => {
    vi.stubGlobal("fetch", mockFetch(overview({
      connectors: [
        { installation: "mixed-connector", n: 3, ok_n: 2, success_pct: 66.7,
          avg_ms: 300, max_ms: 600, last_seen: 1758800000, legacy_failure_samples: 11 },
        { installation: "legacy-only", n: 0, ok_n: 0, success_pct: null,
          avg_ms: null, max_ms: null, last_seen: null, legacy_failure_samples: 7 },
      ],
    })));
    render(<MonitorPage />);
    await waitFor(() => expect(screen.getByText("mixed-connector")).toBeTruthy());
    const mixed = screen.getByText("mixed-connector").closest("tr")!;
    expect(within(mixed).getByText("66.7%")).toBeTruthy();
    expect(within(mixed).getByText("300ms")).toBeTruthy();
    expect(within(mixed).getByText("11")).toBeTruthy();
    const legacy = screen.getByText("legacy-only").closest("tr")!;
    expect(within(legacy).queryByText("0.0%")).toBeNull();
    expect(within(legacy).getAllByText("—")).toHaveLength(4);
    expect(screen.getByText("Connector logical resource requests")).toBeTruthy();
    expect(screen.getByText("legacy failure samples")).toBeTruthy();
    expect(screen.getByText("event samples")).toBeTruthy();
    expect(screen.getByText(/Historical failure samples do not enter/)).toBeTruthy();
  });

  it("shows the turn count and the window in the header", async () => {
    vi.stubGlobal("fetch", mockFetch(overview()));
    render(<MonitorPage />);
    await waitFor(() => expect(screen.getByText(/12 turns/)).toBeTruthy());
    expect(screen.getByText(/last 24h/)).toBeTruthy();
  });

  it("surfaces a persistently failing connector", async () => {
    vi.stubGlobal("fetch", mockFetch(overview()));
    render(<MonitorPage />);
    await waitFor(() =>
      expect(screen.getByText("connector_unavailable")).toBeTruthy()
    );
    // The kind is named too, so the row says WHAT failed, not just how it failed.
    expect(screen.getByText("connector_call")).toBeTruthy();
    // 11 appears in more than one table (also turns.ok_n), so assert the row
    // rather than the bare number.
    const row = screen.getByText("connector_unavailable").closest("tr");
    expect(row?.textContent).toContain("11");
  });

  it("keeps the latency tail visible rather than averaging it away", async () => {
    vi.stubGlobal("fetch", mockFetch(overview()));
    render(<MonitorPage />);
    // p99 of 91s must render as its own column, not be hidden behind the mean.
    await waitFor(() => expect(screen.getByText("p99")).toBeTruthy());
    expect(screen.getAllByText("91.0s").length).toBeGreaterThan(0);
  });

  it("reports an empty section as 'nothing recorded', never as blank", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(overview({ counts: [], turns: [], errors: [], resources: [] }))
    );
    render(<MonitorPage />);
    await waitFor(() => expect(screen.getByText("resources")).toBeTruthy());
    for (const title of ["Codex effective output TPS",
      "Recent logical requests (up to 20, within window)", "Connector logical resource requests",
      "event samples", "turns", "top error event samples", "resources"]) {
      const section = screen.getByText(title).closest("section")!;
      expect(within(section).getByText(/Nothing recorded in this window/)).toBeTruthy();
    }
  });

  it("explains a 503 instead of showing an error", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch({ detail: "No metrics yet — the store is created when the server starts." }, false, 503)
    );
    render(<MonitorPage />);
    await waitFor(() => expect(screen.getByText(/No metrics yet/)).toBeTruthy());
  });

  it("refetches when the window changes", async () => {
    const f = mockFetch(overview());
    vi.stubGlobal("fetch", f);
    render(<MonitorPage />);
    await waitFor(() => expect(f).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByText("7d"));
    await waitFor(() => expect(f).toHaveBeenCalledTimes(2));
    expect(String(f.mock.calls[1][0])).toContain("window=7d");
  });

  it("sends the bearer token", async () => {
    const f = mockFetch(overview());
    vi.stubGlobal("fetch", f);
    render(<MonitorPage />);
    await waitFor(() => expect(f).toHaveBeenCalled());
    expect(f.mock.calls[0][1].headers.Authorization).toBe("Bearer t");
  });
});


it("shows effective TPS, coverage and a missing-timing state", async () => {
  vi.stubGlobal("fetch", mockFetch(overview({ codex: [
    { model: "gpt-test", weighted_tps: 25, latest_tps: 20, latest_at: 1758800000,
      responses: 3, timed_responses: 2, coverage_pct: 66.7, output_tokens: 350,
      reasoning_tokens: 240, avg_turn_ttft_ms: 2000 },
    { model: "missing-model", weighted_tps: null, latest_tps: null, latest_at: null,
      responses: 1, timed_responses: 0, coverage_pct: 0, output_tokens: 25,
      reasoning_tokens: null, avg_turn_ttft_ms: null },
  ] })));
  render(<MonitorPage />);
  await screen.findByText("gpt-test");
  const row = screen.getByText("gpt-test").closest("tr");
  expect(row?.textContent).toContain("25.00 tok/s");
  expect(row?.textContent).toContain("20.00 tok/s");
  expect(row?.textContent).toContain("66.7%");
  expect(row?.textContent).toContain("Partial timing");
  expect(screen.getByText("missing-model").closest("tr")?.textContent).toContain("Missing timing");
  expect(screen.getByText(/Includes reasoning/)).toBeTruthy();
});

it("polls every five seconds only while visible and resumes on focus", async () => {
  vi.useFakeTimers();
  let visibility = "visible";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility as DocumentVisibilityState);
  const fetcher = mockFetch(overview());
  vi.stubGlobal("fetch", fetcher);
  render(<MonitorPage />);
  await act(async () => {});
  expect(fetcher).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(fetcher).toHaveBeenCalledTimes(2);
  visibility = "hidden";
  fireEvent(document, new Event("visibilitychange"));
  await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
  expect(fetcher).toHaveBeenCalledTimes(2);
  visibility = "visible";
  await act(async () => { fireEvent(document, new Event("visibilitychange")); });
  expect(fetcher).toHaveBeenCalledTimes(3);
});

it("keeps refreshes serial while a visible request is pending", async () => {
  vi.useFakeTimers();
  let finish!: (response: Response) => void;
  const fetcher = vi.fn().mockImplementation(() => new Promise<Response>((resolve) => { finish = resolve; }));
  vi.stubGlobal("fetch", fetcher);
  render(<MonitorPage />);
  await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(fetcher).toHaveBeenCalledTimes(1);
  await act(async () => { finish({ ok: true, json: async () => overview() } as Response); });
  expect(screen.getByText(/12 turns/)).toBeTruthy();
});

it("reports refresh failure while keeping the last measured data", async () => {
  const fetcher = mockFetch(overview());
  vi.stubGlobal("fetch", fetcher);
  render(<MonitorPage />);
  await waitFor(() => expect(screen.getByText(/12 turns/)).toBeTruthy());
  fetcher.mockRejectedValueOnce(new Error("Monitor unavailable"));
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await waitFor(() => expect(screen.getByText("Monitor unavailable")).toBeTruthy());
  expect(screen.getByText(/12 turns/)).toBeTruthy();
});

it("isolates data and errors across window and token changes", async () => {
  const pending: Array<(response: Response) => void> = [];
  vi.stubGlobal("fetch", vi.fn().mockImplementation(() => new Promise<Response>((resolve) => pending.push(resolve))));
  render(<MonitorPage />);
  fireEvent.click(screen.getByRole("button", { name: "7d" }));
  await act(async () => {
    pending[1]({ ok: false, status: 500, json: async () => ({ detail: "7d failed" }) } as Response);
  });
  expect(screen.getByText("7d failed")).toBeTruthy();
  act(() => { useSessionStore.setState({ token: "new-token" }); });
  expect(screen.queryByText("7d failed")).toBeNull();
  await act(async () => {
    pending[2]({ ok: true, json: async () => overview({ counts: [{ kind: "turn", n: 99 }] }) } as Response);
    pending[0]({ ok: true, json: async () => overview() } as Response);
  });
  expect(screen.getByText(/99 turns/)).toBeTruthy();
  expect(screen.queryByText(/12 turns/)).toBeNull();
});

/**
 * Renderer tests for ResearchCard (native-deep-research.md §7): live phase
 * while running, a result line on completion, an error line on failure, and a
 * cancel button that POSTs only while running.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ResearchCard } from "./ResearchCard";
import { useSessionStore, type ResearchJob } from "../stores/sessionStore";

function job(overrides: Partial<ResearchJob> = {}): ResearchJob {
  return {
    id: "r1",
    session_id: "s1",
    question: "What is X?",
    status: "running",
    phase: "search",
    ...overrides,
  };
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  useSessionStore.setState({ token: "tok", research: {} });
  fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ResearchCard", () => {
  it("shows a rejected cancellation without changing the research outcome", async () => {
    fetchMock.mockResolvedValue(Response.json({ detail: "Research control unavailable" }, { status: 503 }));
    useSessionStore.setState({ research: { s1: [job()] } });
    render(<ResearchCard sessionId="s1" />);
    fireEvent.click(screen.getByTitle("Cancel research"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Research control unavailable");
    expect(useSessionStore.getState().research.s1[0].status).toBe("running");
  });

  it("renders nothing with no jobs", () => {
    const { container } = render(<ResearchCard sessionId="s1" />);
    expect(container.firstChild).toBeNull();
  });

  it("shows the running phase + a cancel button", () => {
    useSessionStore.setState({ research: { s1: [job({ phase: "verify" })] } });
    render(<ResearchCard sessionId="s1" />);
    expect(screen.getByText("What is X?")).toBeTruthy();
    expect(screen.getByText(/Verifying claims/)).toBeTruthy();
    expect(screen.getByTitle("Cancel research")).toBeTruthy();
  });

  it("shows a result line and no cancel when completed", () => {
    useSessionStore.setState({
      research: { s1: [job({ status: "completed", phase: "done", injection_status: "delivered", verified: 3, sources: ["u"] })] },
    });
    render(<ResearchCard sessionId="s1" />);
    expect(screen.getByText(/Report accepted for follow-up; processing unconfirmed/)).toBeTruthy();
    expect(screen.getByText(/3 verified findings/)).toBeTruthy();
    expect(screen.queryByTitle("Cancel research")).toBeNull();
  });

  it("does not claim delivery while the report awaits admission", () => {
    useSessionStore.setState({ research: { s1: [job({ status: "completed", phase: "done" })] } });
    render(<ResearchCard sessionId="s1" />);
    expect(screen.queryByText(/Report delivered below/)).toBeNull();
    expect(screen.getByText(/delivery pending/)).toBeTruthy();
  });

  it("confirms the exact displayed delivery version separately from processing", async () => {
    const receipt = { request_id: "follow-up", version: 3, stage: "processed" as const };
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ delivery_receipt: {
      ...receipt, browser_confirmed_version: 3, browser_confirmed_at: "now",
    } })));
    useSessionStore.getState().setResearch("s1", [job({ status: "completed", delivery_receipt: receipt })]);
    render(<ResearchCard sessionId="s1" />);
    expect(screen.getByText(/Follow-up processed/)).toBeTruthy();
    await waitFor(() => expect(screen.getByText(/Browser received this status/)).toBeTruthy());
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/research/r1/seen"), expect.objectContaining({
      method: "POST", body: JSON.stringify({ request_id: "follow-up", version: 3 }),
    }));
  });

  it("shows retained material and the suppressed delivery reason after reload", () => {
    useSessionStore.getState().setResearch("s1", [job({
      status: "completed", phase: "done", injection_status: "failed", error: "Original work is closed",
    })]);
    render(<ResearchCard sessionId="s1" />);
    expect(screen.queryByText(/Report delivered below/)).toBeNull();
    expect(screen.getByText(/Report retained as material/)).toBeTruthy();
    expect(screen.getByText(/Original work is closed/)).toBeTruthy();
  });

  it("keeps access to a saved report when delivery failed or remains unknown", () => {
    useSessionStore.getState().setResearch("s1", [job({ status: "completed", injection_status: "unknown", report_path: "/owned/research/saved.md" })]);
    render(<ResearchCard sessionId="s1" />);
    expect(screen.getByRole("link", { name: "Download report" }).getAttribute("href"))
      .toContain("/api/sessions/s1/research/r1/report?token=tok");
    expect(screen.getByText(/admission is unconfirmed/)).toBeTruthy();
  });

  it("shows the error on failure", () => {
    useSessionStore.setState({
      research: { s1: [job({ status: "failed", error: "boom" })] },
    });
    render(<ResearchCard sessionId="s1" />);
    expect(screen.getByText("boom")).toBeTruthy();
  });

  it("ignores a partial upsert for an unknown id (no malformed card)", () => {
    // A progress/completed event arriving without a prior research_started
    // (missed after reconnect / in a 2nd tab) must NOT create a card with no
    // question/status (Vera review).
    useSessionStore.getState().upsertResearch("s1", { id: "ghost", phase: "verify" });
    render(<ResearchCard sessionId="s1" />);
    expect(screen.queryByText(/Verifying claims/)).toBeNull();
    expect(useSessionStore.getState().research["s1"] ?? []).toHaveLength(0);
  });

  it("cancel POSTs to the cancel route", async () => {
    useSessionStore.setState({ research: { s1: [job()] } });
    render(<ResearchCard sessionId="s1" />);
    fireEvent.click(screen.getByTitle("Cancel research"));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([u]) =>
          String(u).endsWith("/api/sessions/s1/research/r1/cancel")
        )
      ).toBe(true)
    );
  });

  it("merges the authoritative completed response without a broadcast", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify(job({ status: "completed", phase: "done" }))));
    useSessionStore.setState({ research: { s1: [job()] } });
    render(<ResearchCard sessionId="s1" />);
    fireEvent.click(screen.getByTitle("Cancel research"));
    await waitFor(() => expect(screen.getByText(/Report ready/)).toBeTruthy());
    expect(screen.queryByTitle("Cancel research")).toBeNull();
  });

  it.each(["account", "service", "session", "unmount"])("ignores a late cancel response after %s changes", async (change) => {
    let reply!: (value: Response) => void;
    fetchMock.mockImplementation(() => new Promise<Response>((resolve) => { reply = resolve; }));
    useSessionStore.setState({ research: { s1: [job()] }, activeSessionId: "s1", dispatchGeneration: "service-a" });
    const view = render(<ResearchCard sessionId="s1" />);
    fireEvent.click(screen.getByTitle("Cancel research"));
    await act(async () => {
      if (change === "account") useSessionStore.setState({ accountGeneration: useSessionStore.getState().accountGeneration + 1 });
      if (change === "service") useSessionStore.setState({ dispatchGeneration: "service-b" });
      if (change === "session") useSessionStore.setState({ activeSessionId: "s2" });
      if (change === "unmount") view.unmount();
      reply(new Response(JSON.stringify(job({ status: "completed", phase: "done" }))));
    });
    expect(useSessionStore.getState().research.s1[0].status).toBe("running");
  });
});

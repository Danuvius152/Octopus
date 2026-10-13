import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ModelPickerDialog } from "./ModelPickerDialog";
import { useSessionStore, type Agent, type SessionInfo } from "../stores/sessionStore";

const session = {
  id: "session1", name: "Existing", working_dir: "/tmp", created_at: "2026-10-11",
  status: "idle", backend: "codex", model: "model-a", reasoning_effort: null,
  claude_session_id: "original-thread", agent_id: "agent1",
} as SessionInfo;
const catalog = {
  default_model: "model-a", default_reasoning_effort: "max",
  reasoning_efforts: ["low", "high", "max", "ultra"],
  service_tiers: ["default", "fast"], default_service_tier: "default",
  models: [
    { id: "model-a", name: "Model A", reasoning_efforts: ["low", "high", "max", "ultra"], service_tiers: ["default", "fast"] },
    { id: "model-b", name: "Model B", reasoning_efforts: ["low", "high"], service_tiers: ["default"] },
  ],
};
let fetchMock: ReturnType<typeof vi.fn>;
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { "Content-Type": "application/json" },
});

beforeEach(() => {
  useSessionStore.setState({ token: "token", sessions: [session], agents: [], backendModels: {} });
  fetchMock = vi.fn(async (_url: unknown, init?: RequestInit) => {
    if (init?.method === "PATCH") return json({ ...session, ...JSON.parse(String(init.body)) });
    return json(catalog);
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("defaults to Normal and changes only speed while retaining the conversation", async () => {
  const close = vi.fn();
  render(<ModelPickerDialog session={session} open onOpenChange={close} />);
  const selector = screen.getByLabelText("Speed");
  await waitFor(() => expect(selector).toBeEnabled());
  expect(selector).toHaveDisplayValue("Agent default (Normal)");
  expect(selector).not.toHaveTextContent("Ultrafast");
  fireEvent.change(selector, { target: { value: "fast" } });
  fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(String(request?.[1].body))).toEqual({ service_tier: "fast" });
  expect(useSessionStore.getState().sessions[0]).toMatchObject({
    model: "model-a", reasoning_effort: null, service_tier: "fast", claude_session_id: "original-thread",
  });
});

it("can explicitly use Normal over an agent's Fast default and restore inheritance", async () => {
  useSessionStore.setState({ agents: [{ id: "agent1", service_tier: "fast" } as Agent] });
  const selected = { ...session, service_tier: "fast" as const };
  const { rerender } = render(<ModelPickerDialog session={selected} open onOpenChange={vi.fn()} />);
  await waitFor(() => expect(screen.getByLabelText("Speed")).toBeEnabled());
  expect(screen.getByLabelText("Speed")).toHaveTextContent("Agent default (Fast)");
  fireEvent.change(screen.getByLabelText("Speed"), { target: { value: "default" } });
  fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true));
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(String(request?.[1].body))).toEqual({ service_tier: "default" });
  const normal = { ...session, service_tier: "default" as const };
  rerender(<ModelPickerDialog session={normal} open onOpenChange={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("Speed"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(2));
  const requests = fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(String(requests[1][1].body))).toEqual({ service_tier: null });
});

it("changes effort alone and preserves the model, thread and concurrent sessions", async () => {
  const close = vi.fn();
  render(<ModelPickerDialog session={session} open onOpenChange={close} />);
  const selector = screen.getByLabelText("Reasoning effort");
  await waitFor(() => expect(selector).toBeEnabled());
  expect(selector).toHaveTextContent("Agent default (max)");
  act(() => useSessionStore.setState({ sessions: [session, { ...session, id: "added-elsewhere" }] }));
  fireEvent.change(selector, { target: { value: "high" } });
  fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(String(request?.[1].body))).toEqual({ reasoning_effort: "high" });
  const updated = useSessionStore.getState().sessions;
  expect(updated).toHaveLength(2);
  expect(updated[0]).toMatchObject({ model: "model-a", reasoning_effort: "high", claude_session_id: "original-thread" });
});

it("offers strengths supported by the selected model and can restore inheritance independently", async () => {
  const selected = { ...session, reasoning_effort: "high" };
  render(<ModelPickerDialog session={selected} open onOpenChange={vi.fn()} />);
  await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "model-b" }));
  expect(screen.getByLabelText("Reasoning effort")).not.toHaveTextContent("Ultra");
  expect(screen.getByRole("option", { name: "Fast — unavailable for this model" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true));
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(String(request?.[1].body))).toEqual({ model: "model-b", reasoning_effort: null });
});

it("shows a server rejection and keeps the saved settings", async () => {
  fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
    init?.method === "PATCH" ? json({ detail: "Effort unavailable for this model" }, 400) : json(catalog),
  );
  const close = vi.fn();
  render(<ModelPickerDialog session={session} open onOpenChange={close} />);
  await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "high" } });
  fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  expect(await screen.findByText("Effort unavailable for this model")).toBeVisible();
  expect(close).not.toHaveBeenCalled();
  expect(useSessionStore.getState().sessions[0].reasoning_effort).toBeNull();
});

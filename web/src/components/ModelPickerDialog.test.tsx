import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ModelPickerDialog } from "./ModelPickerDialog";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";

const session = {
  id: "session1", name: "Existing", working_dir: "/tmp", created_at: "2026-10-11",
  status: "idle", backend: "codex", model: "model-a", reasoning_effort: null,
  claude_session_id: "original-thread", agent_id: "agent1",
} as SessionInfo;
const catalog = {
  default_model: "model-a", default_reasoning_effort: "max",
  reasoning_efforts: ["low", "high", "max", "ultra"],
  models: [
    { id: "model-a", name: "Model A", reasoning_efforts: ["low", "high", "max", "ultra"] },
    { id: "model-b", name: "Model B", reasoning_efforts: ["low", "high"] },
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

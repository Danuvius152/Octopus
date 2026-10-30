import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ModelPickerDialog } from "./ModelPickerDialog";
import { sessions } from "../lib/sessionList";
import { useSessionStore, type Agent, type CredentialInfo, type SessionInfo } from "../stores/sessionStore";

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
  useSessionStore.getState().setToken("token");
  useSessionStore.setState({ sessions: [session], agents: [{ id: "agent1", name: "Saved assistant", backend: "codex" } as Agent],
    credentials: [], backendModels: {}, mainView: "chat", editingAgentId: null });
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

it("explains the selected conversation's effective values, sources and actual backend", async () => {
  const selected = { ...session, model: "model-b", service_tier: "default" as const };
  useSessionStore.setState({
    sessions: [selected], activeAgentId: "unrelated-agent",
    agents: [{ id: "agent1", name: "Saved assistant", backend: "claude-code", model: "model-a",
      reasoning_effort: "high", service_tier: "fast", credential_id: "claude-sign-in" } as Agent],
    credentials: [{ id: "claude-sign-in", label: "Claude sign-in", backend: "claude-code" } as CredentialInfo],
  });
  const close = vi.fn();
  render(<ModelPickerDialog session={selected} open onOpenChange={close} />);
  await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
  const current = within(screen.getByRole("table", { name: "Current effective settings" }));
  expect(current.getByRole("row", { name: /Model model-b Session override/ })).toBeVisible();
  expect(current.getByRole("row", { name: /Reasoning effort high Agent default/ })).toBeVisible();
  expect(current.getByRole("row", { name: /Speed Normal Session override/ })).toBeVisible();
  expect(current.getByRole("row", { name: /Credential Claude sign-in Agent default/ })).toBeVisible();
  expect(screen.getByText(/This conversation uses codex/)).toHaveTextContent("Agent default backend: claude-code");
  expect(screen.getByText(/header credential picker changes only the sign-in/i)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Agent settings" }));
  expect(useSessionStore.getState()).toMatchObject({ mainView: "agent-form", editingAgentId: "agent1" });
  expect(close).toHaveBeenCalledWith(false);
});

it("does not describe a selected credential with missing details as the host login", async () => {
  const selected = { ...session, credential_id: "not-loaded" };
  render(<ModelPickerDialog session={selected} open onOpenChange={vi.fn()} />);
  await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
  const current = within(screen.getByRole("table", { name: "Current effective settings" }));
  expect(current.getByRole("row", { name: /Credential Selected credential Session override/ })).toBeVisible();
});

it("shows the backend's binding rejection and keeps the inherited credential source visible", async () => {
  useSessionStore.setState({ agents: [{ id: "agent1", credential_id: "other-backend" } as Agent] });
  fetchMock.mockResolvedValue(json({ detail: "Credential backend does not match the selected backend" }, 400));
  render(<ModelPickerDialog session={session} open onOpenChange={vi.fn()} />);
  expect(await screen.findByText("codex: Credential backend does not match the selected backend")).toBeVisible();
  const current = within(screen.getByRole("table", { name: "Current effective settings" }));
  expect(current.getByRole("row", { name: /Credential Selected credential Agent default/ })).toBeVisible();
  expect(fetchMock.mock.calls[0][0]).toBe("/api/backends/codex/models?credential_id=other-backend");
});

it("keeps inherited settings unknown while Agent details are unavailable", () => {
  useSessionStore.setState({ agents: [] });
  render(<ModelPickerDialog session={session} open onOpenChange={vi.fn()} />);
  const current = within(screen.getByRole("table", { name: "Current effective settings" }));
  expect(current.getByRole("row", { name: /Model model-a Session override/ })).toBeVisible();
  expect(current.getByRole("row", { name: /Reasoning effort Unknown Agent details unavailable/ })).toBeVisible();
  expect(current.getByRole("row", { name: /Credential Unknown Agent details unavailable/ })).toBeVisible();
  expect(screen.getByLabelText("Reasoning effort")).toBeDisabled();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("repairs an incompatible inherited credential and effort in one request", async () => {
  const selected = { ...session, model: "model-b" };
  useSessionStore.setState({ sessions: [selected], agents: [{
    id: "agent1", backend: "claude-code", credential_id: "claude-key", reasoning_effort: "max",
  } as Agent], credentials: [
    { id: "claude-key", backend: "claude-code", label: "Claude sign-in" } as CredentialInfo,
    { id: "codex-key", backend: "codex", label: "Codex sign-in" } as CredentialInfo,
  ] });
  fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "PATCH") return json({ ...selected, ...JSON.parse(String(init.body)) });
    if (String(url).includes("credential_id=claude-key")) return json({ detail: "Credential backend mismatch" }, 400);
    return json(catalog);
  });
  const close = vi.fn();
  render(<ModelPickerDialog session={selected} open onOpenChange={close} />);
  expect(await screen.findByText("codex: Credential backend mismatch")).toBeVisible();
  expect(screen.getByLabelText("Reasoning effort")).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Credential"), { target: { value: "codex-key" } });
  await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "low" } });
  fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  const changes = fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH");
  expect(changes).toHaveLength(1);
  expect(JSON.parse(String(changes[0][1].body))).toEqual({ credential_id: "codex-key", reasoning_effort: "low" });
  expect(useSessionStore.getState().sessions[0]).toMatchObject({
    backend: "codex", model: "model-b", credential_id: "codex-key", reasoning_effort: "low", claude_session_id: "original-thread",
  });
});

it("restores credential inheritance without changing other overrides", async () => {
  const selected = { ...session, credential_id: "session-key", reasoning_effort: "high" };
  useSessionStore.setState({ sessions: [selected], agents: [{ id: "agent1", credential_id: "agent-key" } as Agent], credentials: [
    { id: "agent-key", backend: "codex", label: "Agent sign-in" } as CredentialInfo,
    { id: "session-key", backend: "codex", label: "Session sign-in" } as CredentialInfo,
  ] });
  fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) => init?.method === "PATCH"
    ? json({ ...selected, ...JSON.parse(String(init.body)) }) : json(catalog));
  const close = vi.fn();
  render(<ModelPickerDialog session={selected} open onOpenChange={close} />);
  await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Credential"), { target: { value: "" } });
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("credential_id=agent-key"))).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(String(request?.[1].body))).toEqual({ credential_id: null });
  expect(useSessionStore.getState().sessions[0]).toMatchObject({ model: "model-a", reasoning_effort: "high", credential_id: null });
});

it.each([200, 400])("a late model-settings response for another session does not close or write errors into this dialog (%s)", async (status) => {
  let finish!: (response: Response) => void;
  fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) => init?.method === "PATCH"
    ? new Promise<Response>((resolve) => { finish = resolve; }) : json(catalog));
  const a = { ...session, id: "a" }, b = { ...session, id: "b", model: "model-b" };
  useSessionStore.setState({ sessions: [a, b] });
  const close = vi.fn(), applied = vi.fn();
  const { rerender } = render(<ModelPickerDialog session={a} open onOpenChange={close} onApplied={applied} />);
  await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "low" } });
  fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(finish).toBeTypeOf("function"));
  rerender(<ModelPickerDialog session={b} open onOpenChange={close} onApplied={applied} />);
  await act(async () => finish(status === 200 ? json({ ...a, reasoning_effort: "low" }) : json({ detail: "Original A refusal" }, 400)));
  expect(close).not.toHaveBeenCalled();
  expect(applied).not.toHaveBeenCalled();
  expect(screen.queryByText("Original A refusal")).toBeNull();
  expect(screen.getByLabelText("Model name")).toHaveValue("model-b");
  expect(useSessionStore.getState().sessions.find((s) => s.id === "b")?.reasoning_effort).toBeNull();
  if (status === 200) expect(useSessionStore.getState().sessions.find((s) => s.id === "a")?.reasoning_effort).toBe("low");
});

it("confirms a model write that succeeds after reconnect has already read the old settings", async () => {
  const accountGeneration = useSessionStore.getState().accountGeneration;
  const first = { socket: {}, accountGeneration }, second = { socket: {}, accountGeneration };
  let finish!: (response: Response) => void;
  let saved = false;
  fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "PATCH") return new Promise<Response>((resolve) => { finish = resolve; });
    if (String(url).endsWith("/api/sessions")) return json([{ ...session, service_tier: saved ? "fast" : null }]);
    return json(catalog);
  });
  await act(async () => { sessions.connect(first); });
  render(<ModelPickerDialog session={session} open onOpenChange={vi.fn()} />);
  await waitFor(() => expect(screen.getByLabelText("Speed")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Speed"), { target: { value: "fast" } });
  fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(finish).toBeTypeOf("function"));
  await act(async () => { sessions.disconnect(first); sessions.connect(second); });
  expect(useSessionStore.getState().sessions[0].service_tier).toBeNull();
  await act(async () => { saved = true; finish(json({ ...session, service_tier: "fast" })); });
  await waitFor(() => expect(useSessionStore.getState().sessions[0].service_tier).toBe("fast"));
  expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/api/sessions"))).toHaveLength(3);
  expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(1);
  sessions.disconnect(second);
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

it.each(["codex", "claude-code"] as const)("%s changes effort alone and preserves the model, thread and concurrent sessions", async (backend) => {
  const selected = { ...session, backend };
  useSessionStore.setState({ sessions: [selected] });
  const close = vi.fn();
  render(<ModelPickerDialog session={selected} open onOpenChange={close} />);
  const selector = screen.getByLabelText("Reasoning effort");
  await waitFor(() => expect(selector).toBeEnabled());
  expect(selector).toHaveTextContent("Agent default (max)");
  act(() => useSessionStore.setState({ sessions: [selected, { ...selected, id: "added-elsewhere" }] }));
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

import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ModelPickerDialog } from "./ModelPickerDialog";
import { sessions } from "../lib/sessionList";
import { useSessionStore, type Agent, type CredentialInfo, type SessionInfo } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";
import { backendCatalogFixture } from "../test-support/backendCatalog";

const session = {
  id: "session1", name: "Existing", working_dir: "/tmp", created_at: "2026-10-11",
  status: "idle", backend: "codex", model: "model-a", reasoning_effort: null,
  claude_session_id: "original-thread", agent_id: "agent1", configuration_version: 2,
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
    credentials: [], mainView: "chat", editingAgentId: null,
    backendCatalog: { status: "ready", catalog: backendCatalogFixture(["codex", "claude-code"]) } });
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
  const selector = await screen.findByLabelText("Speed");
  await waitFor(() => expect(selector).toBeEnabled());
  expect(selector).toHaveDisplayValue("Agent default (Normal)");
  expect(selector).not.toHaveTextContent("Ultrafast");
  fireEvent.change(selector, { target: { value: "fast" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(String(request?.[1].body))).toEqual({ service_tier: "fast", expected_backend: "codex", expected_configuration_version: 2 });
  expect(useSessionStore.getState().sessions[0]).toMatchObject({
    model: "model-a", reasoning_effort: null, service_tier: "fast", claude_session_id: "original-thread",
  });
});

it("shows the newer configuration and waits for confirmation before retrying the retained draft", async () => {
  const opened = { ...session, reasoning_effort: "low", service_tier: "fast" as const };
  const current = { ...opened, configuration_version: 3, model: "model-b",
    reasoning_effort: "high", service_tier: "default" as const };
  const patches: unknown[] = [];
  fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "PATCH") {
      const patch = JSON.parse(String(init.body));
      patches.push(patch);
      if (patches.length === 1) return json({ detail: {
        code: "configuration_version_conflict", message: "The conversation settings changed.",
      } }, 409);
      return json({ ...current, ...patch, configuration_version: 4 });
    }
    return json(String(url).endsWith("/info") ? current : catalog);
  });
  const close = vi.fn();
  const { rerender } = render(<ModelPickerDialog session={opened} open onOpenChange={close} />);
  const effort = await screen.findByLabelText("Reasoning effort");
  await waitFor(() => expect(effort).toBeEnabled());
  rerender(<ModelPickerDialog session={current} open onOpenChange={close} />);
  fireEvent.change(effort, { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));

  expect(await screen.findByText("Current saved settings")).toBeVisible();
  await waitFor(() => expect(screen.getByLabelText("Model")).toHaveDisplayValue("Model B"));
  expect(effort).toHaveValue("");
  expect(patches).toEqual([{ reasoning_effort: null, expected_backend: "codex", expected_configuration_version: 2 }]);
  expect(close).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  expect(patches).toEqual([
    { reasoning_effort: null, expected_backend: "codex", expected_configuration_version: 2 },
    { reasoning_effort: null, expected_backend: "codex", expected_configuration_version: 3 },
  ]);
});

it.each([false, true])("rebases edits after an unconfirmed engine switch (switch back: %s)", async (switchBack) => {
  const opened = { ...session, claude_session_id: null, has_inputs: false, message_count: 0 };
  const current: SessionInfo = { ...opened, backend: "claude-code", defaults_source: "backend",
    model: "model-b", reasoning_effort: "low", service_tier: "default", configuration_version: 3 };
  const patches: Record<string, unknown>[] = [];
  fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "PATCH") {
      const patch = JSON.parse(String(init.body));
      patches.push(patch);
      if (patches.length === 1) throw new TypeError("Connection lost");
      if (patches.length === 2) return json({ detail: { code: "configuration_version_conflict" } }, 409);
      return json({ ...current, ...patch, configuration_version: 4 });
    }
    if (String(url).endsWith("/info")) return json(current);
    if (String(url).endsWith(`/sessions/${opened.id}`)) return json({ ...current, messages: [] });
    return json(catalog);
  });
  const close = vi.fn();
  render(<ModelPickerDialog session={opened} open onOpenChange={close} />);
  fireEvent.click(screen.getByRole("radio", { name: "Claude Code" }));
  await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "high" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  expect(await screen.findByText("Connection lost")).toBeVisible();
  if (switchBack) fireEvent.click(screen.getByRole("radio", { name: "Codex" }));
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  expect(await screen.findByText("Current saved settings")).toBeVisible();
  await waitFor(() => expect(screen.getByLabelText("Model")).toHaveDisplayValue(switchBack ? "Agent default (Model A)" : "Model B"));
  expect(patches).toHaveLength(2);
  expect(close).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  expect(patches[2]).toEqual({
    expected_backend: "claude-code", expected_configuration_version: 3,
    ...(switchBack ? { backend: "codex", reasoning_effort: null, model: null, service_tier: null, credential_id: null }
      : { backend: "claude-code", reasoning_effort: "high" }),
  });
});

it("keeps edits to both engines when the saved engine changes during a conflict", async () => {
  const opened = { ...session, claude_session_id: null, has_inputs: false, message_count: 0 };
  const saved: SessionInfo = { ...opened, backend: "claude-code", defaults_source: "backend",
    model: "model-a", reasoning_effort: "high", configuration_version: 3 };
  const patches: unknown[] = [];
  fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "PATCH") {
      const patch = JSON.parse(String(init.body));
      patches.push(patch);
      if (patches.length === 1) return json({ detail: { code: "configuration_version_conflict" } }, 409);
      return json({ ...saved, ...patch, configuration_version: 4 });
    }
    return json(String(url).endsWith("/info") ? saved : catalog);
  });
  const close = vi.fn();
  render(<ModelPickerDialog session={opened} open onOpenChange={close} />);
  const chooseEngine = (name: string) => fireEvent.click(screen.getByRole("radio", { name }));
  const chooseField = async (name: string, value: string) => {
    const selector = await screen.findByLabelText(name);
    await waitFor(() => expect(selector).toBeEnabled());
    fireEvent.change(selector, { target: { value } });
  };
  chooseEngine("Claude Code");
  await chooseField("Model", "model-b");
  await chooseField("Reasoning effort", "low");
  await chooseField("Reasoning effort", "");
  chooseEngine("Codex");
  await chooseField("Reasoning effort", "high");
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));

  expect(await screen.findByText("Current saved settings")).toBeVisible();
  expect(screen.getByLabelText("Reasoning effort")).toHaveValue("high");
  chooseEngine("Claude Code");
  await waitFor(() => expect(screen.getByLabelText("Model")).toHaveDisplayValue("Model B"));
  expect(screen.getByLabelText("Reasoning effort")).toHaveValue("");
  expect(patches).toHaveLength(1);

  fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  expect(patches[1]).toEqual({ backend: "claude-code", model: "model-b", reasoning_effort: null,
    expected_backend: "claude-code", expected_configuration_version: 3 });
});

it.each(["both engines", "engine catalog down", "Claude only"] as const)(
  "can inspect an earlier draft and confirm a clear after another page switches engines (%s)", async (availability) => {
  const opened = { ...session, model: "model-b", reasoning_effort: "low", claude_session_id: null };
  const saved: SessionInfo = { ...opened, backend: "claude-code", defaults_source: "backend",
    model: "model-b", reasoning_effort: "high", configuration_version: 3 };
  const patches: unknown[] = [];
  fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "PATCH") {
      const patch = JSON.parse(String(init.body));
      patches.push(patch);
      if (patches.length === 1) return json({ detail: { code: "configuration_version_conflict" } }, 409);
      return json({ ...saved, ...patch, configuration_version: 4 });
    }
    if (String(url).endsWith("/api/backends")) return json({ detail: "Unavailable" }, 503);
    return json(String(url).endsWith("/info") ? saved : catalog);
  });
  if (availability === "engine catalog down") useSessionStore.setState({ backendCatalog: { status: "idle" } });
  if (availability === "Claude only") useSessionStore.setState({
    backendCatalog: { status: "ready", catalog: backendCatalogFixture(["claude-code"]) },
  });
  const close = vi.fn();
  render(<ModelPickerDialog session={opened} open onOpenChange={close} />);
  const effort = await screen.findByLabelText("Reasoning effort");
  await waitFor(() => expect(effort).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Model"), { target: { value: "model-a" } });
  fireEvent.change(effort, { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  expect(await screen.findByText("Current saved settings")).toBeVisible();
  expect(screen.getByRole("radio", { name: "Claude Code" })).toBeChecked();
  await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toHaveValue(""));
  expect(screen.getByLabelText("Model")).toHaveDisplayValue("Model B");
  const retained = screen.getByRole("region", { name: "Unapplied edits for Codex" });
  expect(within(retained).getByText("model-a")).toBeVisible();
  expect(within(retained).getByText(/Applying the current changes discards these edits/)).toBeVisible();
  if (availability === "both engines") {
    expect(screen.getByRole("radio", { name: "Codex" })).toBeEnabled();
  } else {
    expect(screen.queryByRole("radio", { name: "Codex" })).toBeNull();
    expect(within(retained).getByText(/Codex is currently unavailable/)).toBeVisible();
  }
  if (availability === "engine catalog down") {
    await waitFor(() => expect(useSessionStore.getState().backendCatalog.status).toBe("failed"));
  }
  expect(patches).toEqual([{ model: "model-a", reasoning_effort: null,
    expected_backend: "codex", expected_configuration_version: 2 }]);
  expect(close).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  expect(patches[1]).toEqual({ reasoning_effort: null,
    expected_backend: "claude-code", expected_configuration_version: 3 });
});

it.each([
  ["Clear reasoning effort override", null],
  ["Use earlier reasoning effort choice (high)", "high"],
] as const)("requires a choice when a second conflict restores an older draft: %s", async (choice, effortToSave) => {
  const opened = { ...session, reasoning_effort: "low", claude_session_id: null };
  const claude: SessionInfo = { ...opened, backend: "claude-code", defaults_source: "backend",
    reasoning_effort: "high", configuration_version: 3 };
  const codex: SessionInfo = { ...opened, reasoning_effort: "low", configuration_version: 4 };
  const patches: unknown[] = [];
  fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "PATCH") {
      const patch = JSON.parse(String(init.body));
      patches.push(patch);
      if (patches.length < 3) return json({ detail: { code: "configuration_version_conflict" } }, 409);
      return json({ ...codex, ...patch, configuration_version: 5 });
    }
    return json(String(url).endsWith("/info") ? (patches.length === 1 ? claude : codex) : catalog);
  });
  const close = vi.fn();
  render(<ModelPickerDialog session={opened} open onOpenChange={close} />);
  await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "high" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(screen.getByRole("radio", { name: "Claude Code" })).toBeChecked());
  await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));

  await waitFor(() => expect(screen.getByRole("radio", { name: "Codex" })).toBeChecked());
  const apply = screen.getByRole("button", { name: "Apply changes" });
  expect(apply).toBeDisabled();
  expect(patches).toEqual([
    { reasoning_effort: "high", expected_backend: "codex", expected_configuration_version: 2 },
    { reasoning_effort: null, expected_backend: "claude-code", expected_configuration_version: 3 },
  ]);

  fireEvent.click(screen.getByRole("button", { name: choice }));
  expect(patches).toHaveLength(2);
  expect(apply).toBeEnabled();
  fireEvent.click(apply);
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  expect(patches[2]).toEqual({ reasoning_effort: effortToSave,
    expected_backend: "codex", expected_configuration_version: 4 });
});

it("shows the pending clear even if the new engine catalog is unavailable", async () => {
  const opened = { ...session, reasoning_effort: "low", claude_session_id: null };
  const saved: SessionInfo = { ...opened, backend: "claude-code", defaults_source: "backend",
    reasoning_effort: "high", configuration_version: 3 };
  const patches: unknown[] = [];
  fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "PATCH") {
      const patch = JSON.parse(String(init.body));
      patches.push(patch);
      if (patches.length === 1) return json({ detail: { code: "configuration_version_conflict" } }, 409);
      return json({ ...saved, ...patch, configuration_version: 4 });
    }
    if (String(url).endsWith("/info")) return json(saved);
    if (String(url).includes("/claude-code/models")) return json({ detail: "Catalog unavailable" }, 503);
    return json(catalog);
  });
  const close = vi.fn();
  render(<ModelPickerDialog session={opened} open onOpenChange={close} />);
  const effort = await screen.findByLabelText("Reasoning effort");
  await waitFor(() => expect(effort).toBeEnabled());
  fireEvent.change(effort, { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Catalog unavailable");
  const changes = screen.getByRole("region", { name: "Changes to apply" });
  expect(within(changes).getByText("Reasoning effort")).toBeVisible();
  expect(within(changes).getByText("Use default")).toBeVisible();
  expect(patches).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  expect(patches[1]).toEqual({ reasoning_effort: null,
    expected_backend: "claude-code", expected_configuration_version: 3 });
});

it("can reselect the saved model and prevent another page's delayed save", async () => {
  const opened = { ...session, configuration_version: 0 };
  let saved = opened;
  const olderPatch = { expected_backend: "codex", expected_configuration_version: 0, model: "model-b" };
  const olderReceived = deferred<void>();
  const pageBFinished = deferred<void>();
  const patches: unknown[] = [];
  fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) => {
    if (init?.method !== "PATCH") return json(catalog);
    const patch = JSON.parse(String(init.body));
    patches.push(patch);
    if (patch.model === olderPatch.model) {
      olderReceived.resolve();
      await pageBFinished.promise;
    }
    if (patch.expected_configuration_version !== saved.configuration_version) {
      return json({ detail: { code: "configuration_version_conflict" } }, 409);
    }
    saved = { ...saved, model: patch.model, configuration_version: saved.configuration_version + 1 };
    return json(saved);
  });
  const older = fetch("/api/sessions/session1", { method: "PATCH", body: JSON.stringify(olderPatch) });
  await olderReceived.promise;
  try {
    const close = vi.fn();
    render(<ModelPickerDialog session={opened} open onOpenChange={close} />);
    const selector = await screen.findByLabelText("Model");
    await waitFor(() => expect(selector).toBeEnabled());
    fireEvent.change(selector, { target: { value: "model-b" } });
    fireEvent.change(selector, { target: { value: "model-a" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  } finally {
    pageBFinished.resolve();
  }
  expect((await older).status).toBe(409);
  expect(patches).toEqual([olderPatch, { ...olderPatch, model: "model-a" }]);
  expect(saved).toMatchObject({ model: "model-a", configuration_version: 1 });
});

it("uses the conversation engine and shows inherited values on the individual fields", async () => {
  const selected = { ...session, model: "model-b", service_tier: "default" as const };
  useSessionStore.setState({
    sessions: [selected], activeAgentId: "unrelated-agent",
    agents: [{ id: "agent1", backend: "claude-code", model: "model-a", reasoning_effort: "high",
      service_tier: "fast", credential_id: "claude-sign-in" } as Agent],
    credentials: [{ id: "claude-sign-in", label: "Claude sign-in", backend: "claude-code" } as CredentialInfo],
  });
  render(<ModelPickerDialog session={selected} open onOpenChange={vi.fn()} />);
  await waitFor(() => expect(screen.getByLabelText("Model")).toBeEnabled());
  expect(screen.getByRole("radio", { name: "Codex" })).toBeChecked();
  expect(screen.getByLabelText("Model")).toHaveDisplayValue("Model B");
  expect(screen.getByLabelText("Reasoning effort")).toHaveDisplayValue("Agent default (high)");
  expect(screen.getByLabelText("Speed")).toHaveDisplayValue("Normal");
  expect(screen.getByText("Sign-in", { exact: true }).closest("details")).toHaveAttribute("open");
  expect(screen.getByLabelText("Sign-in for this conversation")).toHaveDisplayValue("Agent default (Claude sign-in)");
  expect(screen.queryByRole("button", { name: "Agent settings" })).toBeNull();
});

it("does not describe a selected credential with missing details as the host login", async () => {
  const selected = { ...session, credential_id: "not-loaded" };
  render(<ModelPickerDialog session={selected} open onOpenChange={vi.fn()} />);
  const details = screen.getByText("Sign-in", { exact: true }).closest("details");
  expect(details).not.toHaveAttribute("open");
  fireEvent.click(screen.getByText("Sign-in", { exact: true }));
  expect(screen.getByLabelText("Sign-in for this conversation")).toHaveDisplayValue("Selected sign-in (unavailable)");
  await waitFor(() => expect(screen.getByLabelText("Model")).toBeEnabled());
});

it("shows the backend's binding rejection and keeps the inherited credential source visible", async () => {
  useSessionStore.setState({ agents: [{ id: "agent1", credential_id: "other-backend" } as Agent] });
  fetchMock.mockResolvedValue(json({ detail: "Credential backend does not match the selected backend" }, 400));
  render(<ModelPickerDialog session={session} open onOpenChange={vi.fn()} />);
  expect(await screen.findByText("codex: Credential backend does not match the selected backend")).toBeVisible();
  expect(screen.getByLabelText("Sign-in for this conversation")).toHaveDisplayValue("Agent default (selected credential)");
  expect(fetchMock.mock.calls[0][0]).toBe("/api/backends/codex/models?credential_id=other-backend&saved_model=model-a");
});

it("keeps inherited settings unknown while Agent details are unavailable", () => {
  useSessionStore.setState({ agents: [] });
  render(<ModelPickerDialog session={session} open onOpenChange={vi.fn()} />);
  expect(screen.getByLabelText("Model")).toBeDisabled();
  expect(screen.getByText("Agent defaults are unavailable.")).toBeVisible();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("loads a different engine's directory and sends the complete settings of an empty conversation", async () => {
  const empty = { ...session, claude_session_id: null, message_count: 0, has_inputs: false };
  useSessionStore.setState({ sessions: [empty] });
  fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "PATCH") return json({ ...empty, ...JSON.parse(String(init.body)), defaults_source: "backend" });
    if (String(url).includes("/claude-code/")) {
      return json({ models: [{ id: "sonnet", name: "Sonnet", reasoning_efforts: ["high"], service_tiers: [] }] });
    }
    return json(catalog);
  });
  const close = vi.fn();
  render(<ModelPickerDialog session={empty} open onOpenChange={close} />);
  fireEvent.click(screen.getByRole("radio", { name: "Claude Code" }));
  const model = screen.getByLabelText("Model");
  expect(model).toHaveValue("");
  expect(await screen.findByRole("option", { name: "Sonnet" })).toHaveValue("sonnet");
  expect(screen.queryByRole("option", { name: "Model A" })).toBeNull();
  fireEvent.change(model, { target: { value: "sonnet" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(String(request?.[1].body))).toEqual({
    backend: "claude-code", expected_backend: "codex", expected_configuration_version: 2, model: "sonnet",
    reasoning_effort: null, service_tier: null, credential_id: null,
  });
  expect(useSessionStore.getState().sessions[0]).toMatchObject({ backend: "claude-code", defaults_source: "backend" });
});

it("discards earlier catalog values while a previously selected engine is refreshing", async () => {
  const refreshing = deferred<Response>();
  const otherEngine = deferred<Response>();
  let codexReads = 0;
  fetchMock.mockImplementation(async (url: unknown) => {
    if (!String(url).includes("/codex/")) return otherEngine.promise;
    return ++codexReads === 1 ? json(catalog) : refreshing.promise;
  });
  render(<ModelPickerDialog session={session} open onOpenChange={vi.fn()} />);
  const model = screen.getByLabelText("Model");
  await screen.findByRole("option", { name: "Model B" });
  fireEvent.change(model, { target: { value: "model-b" } });
  fireEvent.click(screen.getByRole("radio", { name: "Claude Code" }));
  fireEvent.click(screen.getByRole("radio", { name: "Codex" }));
  expect(model).toBeDisabled();
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  await act(async () => otherEngine.resolve(json({ models: [{ id: "other-model", name: "Other Model" }] })));
  expect(model).toBeDisabled();
  await act(async () => refreshing.resolve(json({ ...catalog, models: [{ ...catalog.models[1], name: "Updated Model B" }] })));
  expect(model).toHaveDisplayValue("Updated Model B");
  expect(screen.getByRole("button", { name: "Apply" })).toBeEnabled();
});

it("offers retry when the Codex directory is unreadable", async () => {
  fetchMock.mockResolvedValueOnce(json({ models: [], catalog_error: "Open Codex with this sign-in, then retry." }));
  render(<ModelPickerDialog session={session} open onOpenChange={vi.fn()} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Open Codex with this sign-in");
  fireEvent.click(screen.getByRole("button", { name: "Retry catalog" }));
  expect(await screen.findByRole("option", { name: "Model B" })).toHaveValue("model-b");
  expect(screen.queryByText("Open Codex with this sign-in, then retry.")).not.toBeInTheDocument();
});

it("clears saved overrides using the known default when the catalog fails", async () => {
  const saved = { ...session, reasoning_effort: "high", service_tier: "fast" as const };
  useSessionStore.setState({ sessions: [saved] });
  fetchMock.mockResolvedValueOnce(json({ models: [], default_model: "cli-model", catalog_error: "Cannot read catalog" }));
  const close = vi.fn();
  render(<ModelPickerDialog session={saved} open onOpenChange={close} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Cannot read catalog");
  expect(screen.getByRole("option", { name: "Agent default (cli-model)" })).toBeEnabled();
  for (const label of ["Model", "Reasoning effort", "Speed"]) {
    const control = screen.getByLabelText(label);
    expect(control).toBeEnabled();
    fireEvent.change(control, { target: { value: "" } });
  }
  expect(screen.queryByRole("option", { name: "Model B" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(String(request?.[1].body))).toEqual({
    expected_backend: "codex", expected_configuration_version: 2, model: null, reasoning_effort: null, service_tier: null,
  });
});

it("keeps a saved model outside the picker and uses its actual capabilities for repairs", async () => {
  const saved = { ...session, model: "hidden", reasoning_effort: "high", service_tier: "fast" as const };
  const reading = deferred<Response>();
  fetchMock.mockImplementation(async () => reading.promise);
  render(<ModelPickerDialog session={saved} open onOpenChange={vi.fn()} />);
  expect(screen.getByLabelText("Reasoning effort")).toHaveDisplayValue("High — deeper reasoning");
  expect(screen.getByLabelText("Speed")).toHaveDisplayValue("Fast");
  expect(screen.queryByText(/unavailable for this model/)).toBeNull();
  await act(async () => reading.resolve(json({ ...catalog, saved_model_options: [
    { id: "hidden", name: "Hidden", reasoning_efforts: ["low"], service_tiers: ["default"] },
  ] })));
  expect(fetchMock.mock.calls[0][0]).toBe("/api/backends/codex/models?saved_model=hidden");
  expect(screen.getByLabelText("Model")).toHaveDisplayValue("hidden · outside catalog");
  expect(screen.getByRole("option", { name: "Low — faster responses" })).toBeEnabled();
  expect(screen.queryByRole("option", { name: "Maximum" })).toBeNull();
  expect(screen.getByRole("option", { name: "Fast — unavailable for this model" })).toBeDisabled();
});

it("shows how to repair an inherited explicit Normal value on Claude", async () => {
  const selected = { ...session, backend: "claude-code" as const };
  useSessionStore.setState({ agents: [{ id: "agent1", backend: "codex", service_tier: "default" } as Agent] });
  fetchMock.mockResolvedValue(json({ models: [], service_tiers: [] }));
  render(<ModelPickerDialog session={selected} open onOpenChange={vi.fn()} />);
  const speed = await screen.findByLabelText("Speed");
  expect(speed).toHaveDisplayValue("Agent default (Normal) — unavailable for this model");
  expect(screen.getByRole("option", { name: "Normal — unavailable for this model" })).toBeDisabled();
  expect(screen.getByText(/expand Model defaults and set Speed/)).toBeVisible();
});

it("asks the user to create a new conversation if the first input won the race", async () => {
  const empty = { ...session, claude_session_id: null, message_count: 0, has_inputs: false };
  useSessionStore.setState({ sessions: [empty] });
  fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) => init?.method === "PATCH"
    ? json({ detail: { code: "conversation_started", message: "Already started" } }, 409) : json(catalog));
  render(<ModelPickerDialog session={empty} open onOpenChange={vi.fn()} />);
  fireEvent.click(screen.getByRole("radio", { name: "Claude Code" }));
  await waitFor(() => expect(screen.getByLabelText("Model")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Model"), { target: { value: "model-b" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  expect(await screen.findByRole("button", { name: "New session with Claude Code" })).toBeEnabled();
  expect(screen.getByLabelText("Model")).toHaveValue("model-b");
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
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
  fireEvent.change(screen.getByLabelText("Sign-in for this conversation"), { target: { value: "codex-key" } });
  await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "low" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  const changes = fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH");
  expect(changes).toHaveLength(1);
  expect(JSON.parse(String(changes[0][1].body))).toEqual({ credential_id: "codex-key", reasoning_effort: "low", expected_backend: "codex", expected_configuration_version: 2 });
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
  fireEvent.change(screen.getByLabelText("Sign-in for this conversation"), { target: { value: "" } });
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => new URL(String(url), window.location.origin)
    .searchParams.get("credential_id") === "agent-key")).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(String(request?.[1].body))).toEqual({ credential_id: null, expected_backend: "codex", expected_configuration_version: 2 });
  expect(useSessionStore.getState().sessions[0]).toMatchObject({ model: "model-a", reasoning_effort: "high", credential_id: null });
});

it.each([200, 400, 503])("a late model-settings response for another session does not close or write errors into this dialog (%s)", async (status) => {
  let finish!: (response: Response) => void;
  fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) => init?.method === "PATCH"
    ? new Promise<Response>((resolve) => { finish = resolve; }) : json(catalog));
  const a = { ...session, id: "a" }, b = { ...session, id: "b", model: "model-b" };
  useSessionStore.setState({ sessions: [a, b] });
  const close = vi.fn(), applied = vi.fn();
  const { rerender } = render(<ModelPickerDialog session={a} open onOpenChange={close} onApplied={applied} />);
  await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "low" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(finish).toBeTypeOf("function"));
  rerender(<ModelPickerDialog session={b} open onOpenChange={close} onApplied={applied} />);
  await act(async () => finish(status === 200 ? json({ ...a, reasoning_effort: "low" }) : json({ detail: "Original A failure" }, status)));
  expect(close).not.toHaveBeenCalled();
  expect(applied).not.toHaveBeenCalled();
  expect(screen.queryByText("Original A failure")).toBeNull();
  expect(screen.getByLabelText("Model")).toHaveValue("model-b");
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
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
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
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
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true));
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(String(request?.[1].body))).toEqual({ service_tier: "default", expected_backend: "codex", expected_configuration_version: 2 });
  const normal = { ...session, service_tier: "default" as const };
  rerender(<ModelPickerDialog session={normal} open={false} onOpenChange={vi.fn()} />);
  rerender(<ModelPickerDialog session={normal} open onOpenChange={vi.fn()} />);
  await waitFor(() => expect(screen.getByLabelText("Speed")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Speed"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(2));
  const requests = fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(String(requests[1][1].body))).toEqual({ service_tier: null, expected_backend: "codex", expected_configuration_version: 2 });
});

it.each(["codex", "claude-code"] as const)("%s changes effort alone and preserves the model, thread and concurrent sessions", async (backend) => {
  const selected = { ...session, backend };
  useSessionStore.setState({ sessions: [selected] });
  const close = vi.fn();
  render(<ModelPickerDialog session={selected} open onOpenChange={close} />);
  const selector = await screen.findByLabelText("Reasoning effort");
  await waitFor(() => expect(selector).toBeEnabled());
  expect(selector).toHaveTextContent("Agent default (max)");
  act(() => useSessionStore.setState({ sessions: [selected, { ...selected, id: "added-elsewhere" }] }));
  fireEvent.change(selector, { target: { value: "high" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(String(request?.[1].body))).toEqual({ reasoning_effort: "high", expected_backend: backend, expected_configuration_version: 2 });
  const updated = useSessionStore.getState().sessions;
  expect(updated).toHaveLength(2);
  expect(updated[0]).toMatchObject({ model: "model-a", reasoning_effort: "high", claude_session_id: "original-thread" });
});

it("offers strengths supported by the selected model and can restore inheritance independently", async () => {
  const selected = { ...session, reasoning_effort: "high" };
  render(<ModelPickerDialog session={selected} open onOpenChange={vi.fn()} />);
  await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Model"), { target: { value: "model-b" } });
  expect(screen.getByLabelText("Reasoning effort")).not.toHaveTextContent("Ultra");
  expect(screen.queryByLabelText("Speed")).toBeNull();
  fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true));
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(String(request?.[1].body))).toEqual({ model: "model-b", reasoning_effort: null, expected_backend: "codex", expected_configuration_version: 2 });
});

it("shows a server rejection and keeps the saved settings", async () => {
  fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
    init?.method === "PATCH" ? json({ detail: "Effort unavailable for this model" }, 400) : json(catalog),
  );
  const close = vi.fn();
  render(<ModelPickerDialog session={session} open onOpenChange={close} />);
  await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "high" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  expect(await screen.findByText("Effort unavailable for this model")).toBeVisible();
  fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "" } });
  expect(screen.getByRole("button", { name: "Apply" })).toBeEnabled();
  expect(close).not.toHaveBeenCalled();
  expect(useSessionStore.getState().sessions[0].reasoning_effort).toBeNull();
});

it.each([
  { failure: "network", message: "Connection lost after save" },
  { failure: "deadline", message: "Saving conversation settings could not be confirmed." },
  { failure: 408, message: "Could not save settings (HTTP 408)." },
  { failure: 503, message: "Could not save settings (HTTP 503)." },
] as const)("restores inheritance after an unconfirmed PATCH ($failure)", async ({ failure, message }) => {
  let saved = { ...session };
  const snapshot = deferred<Response>();
  const originalReply = deferred<Response>();
  const patches: unknown[] = [];
  const dispatch = { generation: "model-service", revision: 0, paused: false,
    active_request_id: null, active_operation: null, items: [] };
  useSessionStore.setState({ dispatchGeneration: dispatch.generation });
  fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "PATCH") {
      const patch = JSON.parse(String(init.body));
      patches.push(patch);
      if (patch.expected_configuration_version !== saved.configuration_version) {
        return json({ detail: { code: "configuration_version_conflict" } }, 409);
      }
      saved = { ...saved, ...patch, configuration_version: saved.configuration_version + 1 };
      if (patches.length === 1) {
        if (failure === "network") throw new Error(message);
        if (failure === "deadline") return originalReply.promise;
        return json({}, failure);
      }
      return json(saved);
    }
    if (String(url).includes("/models")) return json(catalog);
    if (String(url).endsWith("/info")) return json(saved);
    if (String(url).endsWith(`/api/sessions/${session.id}?view=browser`)) return snapshot.promise;
    if (String(url).endsWith("/api/sessions")) return json([saved]);
    return json([]);
  });
  const close = vi.fn(), applied = vi.fn();
  function Settings() {
    const observed = useSessionStore((state) => state.sessions.find((item) => item.id === session.id));
    return observed ? <ModelPickerDialog session={observed} open onOpenChange={close} onApplied={applied} /> : null;
  }
  render(<Settings />);
  const effort = await screen.findByLabelText("Reasoning effort");
  await waitFor(() => expect(effort).toBeEnabled());
  const apply = screen.getByRole("button", { name: "Apply" });
  expect(effort).toHaveValue("");
  fireEvent.change(effort, { target: { value: "low" } });
  if (failure === "deadline") {
    vi.useFakeTimers();
    try {
      fireEvent.click(apply);
      const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
      expect(request?.[1]?.signal?.aborted).toBe(false);
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      expect(request?.[1]?.signal?.aborted).toBe(true);
    } finally { vi.useRealTimers(); }
  } else {
    fireEvent.click(apply);
  }
  expect(await screen.findByText(message)).toBeVisible();
  expect(saved.reasoning_effort).toBe("low");

  fireEvent.change(effort, { target: { value: "" } });
  if (failure === "deadline") {
    await act(async () => originalReply.resolve(json(saved)));
    expect(close).not.toHaveBeenCalled();
    expect(applied).not.toHaveBeenCalled();
    expect(effort).toHaveValue("");
    expect(apply).toBeEnabled();
  }
  saved = { ...saved, service_tier: "fast" };
  await act(async () => snapshot.resolve(json({ ...saved, messages: [], dispatch_state: dispatch, active_attempt: null })));
  expect(useSessionStore.getState().sessions[0]).toMatchObject({ reasoning_effort: "low", service_tier: "fast" });
  expect(effort).toHaveValue("");
  expect(screen.getByLabelText("Speed")).toHaveValue("");
  expect(apply).toBeEnabled();
  fireEvent.click(apply);
  expect(await screen.findByText("Current saved settings")).toBeVisible();
  expect(effort).toHaveValue("");
  expect(close).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  expect(patches).toEqual([
    { reasoning_effort: "low", expected_backend: "codex", expected_configuration_version: 2 },
    { reasoning_effort: null, expected_backend: "codex", expected_configuration_version: 2 },
    { reasoning_effort: null, expected_backend: "codex", expected_configuration_version: 3 },
  ]);
  expect(saved).toMatchObject({ reasoning_effort: null, service_tier: "fast" });
});

it("keeps an earlier unconfirmed field after a later PATCH is rejected", async () => {
  const patches: unknown[] = [];
  fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
    if (init?.method !== "PATCH") {
      return String(url).includes("/models") ? json(catalog) : json({ detail: "Unavailable" }, 503);
    }
    const patch = JSON.parse(String(init.body));
    patches.push(patch);
    if (patches.length === 1) throw new Error("Connection lost after save");
    if (patch.service_tier === "fast") return json({ detail: "Speed unavailable for this model" }, 400);
    return json({ ...session, ...patch });
  });
  const close = vi.fn();
  render(<ModelPickerDialog session={session} open onOpenChange={close} />);
  const effort = await screen.findByLabelText("Reasoning effort");
  const speed = screen.getByLabelText("Speed");
  const apply = screen.getByRole("button", { name: "Apply" });
  await waitFor(() => expect(effort).toBeEnabled());
  fireEvent.change(effort, { target: { value: "low" } });
  fireEvent.click(apply);
  expect(await screen.findByText("Connection lost after save")).toBeVisible();

  fireEvent.change(effort, { target: { value: "" } });
  fireEvent.change(speed, { target: { value: "fast" } });
  fireEvent.click(apply);
  expect(await screen.findByText("Speed unavailable for this model")).toBeVisible();

  fireEvent.change(speed, { target: { value: "" } });
  expect(apply).toBeEnabled();
  fireEvent.click(apply);
  await waitFor(() => expect(close).toHaveBeenCalledWith(false));
  expect(patches).toEqual([
    { reasoning_effort: "low", expected_backend: "codex", expected_configuration_version: 2 },
    { reasoning_effort: null, service_tier: "fast", expected_backend: "codex", expected_configuration_version: 2 },
    { reasoning_effort: null, service_tier: null, expected_backend: "codex", expected_configuration_version: 2 },
  ]);
});

it("blocks a duplicate POST after creation could not be confirmed", async () => {
  fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "POST") return json({}, 503);
    if (String(url).endsWith("/api/sessions")) return json([session]);
    return json(catalog);
  });
  const close = vi.fn();
  render(<ModelPickerDialog session={session} open onOpenChange={close} />);
  fireEvent.click(screen.getByRole("radio", { name: "Claude Code" }));
  const create = screen.getByRole("button", { name: "New session with Claude Code" });
  fireEvent.click(create);
  expect(await screen.findByText("Creation could not be confirmed. Check the session list before creating another conversation.")).toBeVisible();
  expect(create).toBeDisabled();
  fireEvent.submit(create.closest("form")!);
  expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  expect(close).not.toHaveBeenCalled();
});

it.each([
  ["account", 200], ["account", 503], ["service", 200], ["service", 503],
  ["connection", 200], ["connection", 503], ["view-return", 200],
] as const)(
  "does not apply an old opening's notice or close after %s changes (HTTP %s)", async (transition, status) => {
    useSessionStore.setState({ dispatchGeneration: "original-service" });
    const receipt = deferred<Response>();
    let saved = false;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "PATCH") return receipt.promise;
      if (String(url).endsWith("/api/sessions")) return json([{ ...session, model: saved ? "model-b" : session.model }]);
      return json(catalog);
    });
    const close = vi.fn(), applied = vi.fn();
    render(<ModelPickerDialog session={session} open onOpenChange={close} onApplied={applied} />);
    await waitFor(() => expect(screen.getByLabelText("Model")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "model-b" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    act(() => {
      const store = useSessionStore.getState();
      if (transition === "account") store.setToken("foreign-account");
      else if (transition === "service") useSessionStore.setState({ dispatchGeneration: "replacement-service" });
      else if (transition === "connection") sessions.connect({ socket: {}, accountGeneration: store.accountGeneration });
      else { store.openManage("connectors"); store.showChat(); }
    });
    if (transition === "service") expect(useSessionStore.getState().dispatchGeneration).toBe("replacement-service");
    await act(async () => {
      saved = status === 200;
      receipt.resolve(saved ? json({ ...session, model: "model-b" }) : json({ detail: "Original save failure" }, status));
    });
    expect(applied).not.toHaveBeenCalled();
    expect(close).toHaveBeenCalledTimes(transition === "view-return" ? 1 : 0);
    expect(screen.queryByText("Original save failure")).toBeNull();
    if (transition === "account") {
      expect(useSessionStore.getState().sessions).toEqual([]);
      expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
    } else if (status === 503) {
      const model = screen.getByLabelText("Model");
      expect(model).toHaveValue("model-b");
      fireEvent.change(model, { target: { value: "model-a" } });
      expect(screen.getByRole("button", { name: "Apply" })).toBeEnabled();
    } else {
      await waitFor(() => expect(useSessionStore.getState().sessions[0].model).toBe("model-b"));
    }
  });

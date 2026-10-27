import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/connectors";
import type { ConnectorCatalogEntry, ConnectorInstallationInfo, ConnectorOAuthStatusResponse } from "../api";
import { useSessionStore } from "../stores/sessionStore";
import { ConnectorsPage } from "./ConnectorsPage";

vi.mock("../api/connectors", () => ({
  cancelConnectorOAuth: vi.fn(), createCustomConnector: vi.fn(), deleteCustomConnector: vi.fn(),
  deleteInstallation: vi.fn(), fetchCatalog: vi.fn(), fetchInstallations: vi.fn(),
  getOAuthClient: vi.fn(), pollConnectorOAuth: vi.fn(), setOAuthClient: vi.fn(), startConnectorOAuth: vi.fn(),
}));

beforeEach(() => {
  useSessionStore.setState({ ...useSessionStore.getInitialState(), token: "oauth-account", dispatchGeneration: "service-a" });
  vi.mocked(api.fetchCatalog).mockResolvedValue([{ kind: "fake", display_name: "Fake", available: true } as ConnectorCatalogEntry]);
  vi.mocked(api.fetchInstallations).mockResolvedValue([]);
  vi.mocked(api.startConnectorOAuth).mockResolvedValue({ login_id: "original-login", authorize_url: "https://fake.test/auth" });
  vi.spyOn(window, "open").mockReturnValue(null);
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.clearAllMocks(); });

it.each(["success", "cancelled"] as const)("waits for authoritative %s when closing a pending connection", async (status) => {
  let reply!: (result: ConnectorOAuthStatusResponse) => void;
  vi.mocked(api.cancelConnectorOAuth).mockImplementation(() => new Promise((resolve) => { reply = resolve; }));
  const view = render(<ConnectorsPage onToggleSidebar={() => {}} />);
  fireEvent.click(view.container.querySelector(".btn-connector-add")!);
  fireEvent.click(await screen.findByRole("button", { name: "Connect" }));
  await screen.findByText("Waiting for sign-in in the opened tab…");
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  await screen.findByText("Confirming connection cancellation…");
  expect(api.cancelConnectorOAuth).toHaveBeenCalledWith("oauth-account", "original-login");
  const connected = [{ id: "saved-installation", kind: "fake", label: "Connected account", needs_reconnect: false } as ConnectorInstallationInfo];
  vi.mocked(api.fetchInstallations).mockResolvedValue(connected);
  await act(async () => reply({ status, installation_id: status === "success" ? "saved-installation" : null, message: null }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(useSessionStore.getState().connectorInstallations).toEqual(status === "success" ? connected : []);
});

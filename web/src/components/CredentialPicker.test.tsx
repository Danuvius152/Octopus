/**
 * The header credential chip is the only way to repoint a live session's
 * engine sign-in — it exists because a session's credential used to be fixed
 * at creation, stranding the conversation when that sign-in lapsed or was
 * deleted. These tests cover what it offers and what it sends.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

/** Radix opens its menu on pointerdown, which jsdom doesn't synthesize — the
 * keyboard path (Enter on the trigger) is equivalent and is the one a
 * keyboard user takes anyway. */
function openMenu() {
  fireEvent.keyDown(screen.getByLabelText(/Change this session/), {
    key: "Enter",
  });
}

import { CredentialPicker } from "./CredentialPicker";
import {
  useSessionStore,
  type Agent,
  type CredentialInfo,
  type SessionInfo,
} from "../stores/sessionStore";

const session = (o: Partial<SessionInfo> = {}) =>
  ({
    id: "s1",
    name: "root",
    working_dir: "/tmp",
    status: "idle",
    created_at: "2026-01-01T00:00:00Z",
    message_count: 0,
    agent_id: "ag1",
    origin: "user",
    backend: "claude-code",
    credential_id: null,
    archived: false,
    ...o,
  }) as SessionInfo;

const credential = (o: Partial<CredentialInfo> = {}) =>
  ({
    id: "c1",
    label: "archer-cc",
    backend: "claude-code",
    auth_type: "oauth",
    status: "active",
    needs_reconnect: false,
    ...o,
  }) as CredentialInfo;

const agent = (o: Partial<Agent> = {}) =>
  ({ id: "ag1", name: "Octo", credential_id: null, ...o }) as Agent;

let fetchMock: ReturnType<typeof vi.fn>;

function mount(s: SessionInfo, creds: CredentialInfo[], agents: Agent[] = [agent()]) {
  useSessionStore.setState({ token: "tok", credentials: creds, agents, sessions: [s] });
  return render(<CredentialPicker session={s} />);
}

beforeEach(() => {
  fetchMock = vi.fn(
    async () =>
      new Response(JSON.stringify(session({ credential_id: "c2" })), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("CredentialPicker", () => {
  it("shows the host default when nothing is attached", () => {
    mount(session(), []);
    expect(screen.getByLabelText(/Change this session/)).toHaveTextContent(
      "host default"
    );
  });

  it("shows the agent's credential when the session inherits it", () => {
    mount(session(), [credential()], [agent({ credential_id: "c1" })]);
    expect(screen.getByLabelText(/Change this session/)).toHaveTextContent(
      "archer-cc"
    );
  });

  it("keeps an explicit credential with missing details distinct from the host login", () => {
    mount(session({ credential_id: "missing-details" }), []);
    expect(screen.getByLabelText(/Change this session/)).toHaveTextContent("selected credential");
    expect(screen.getByLabelText(/Change this session/)).not.toHaveTextContent("host default");
  });

  it("shows a rejected inherited binding change and preserves the current override", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: "Credential backend does not match claude-code" }), { status: 400 }));
    mount(session({ credential_id: "c1" }), [credential()], [agent({ credential_id: "wrong-backend" })]);
    openMenu();
    fireEvent.click(screen.getByText(/Agent's credential/));
    expect(await screen.findByRole("alert")).toHaveTextContent("Credential backend does not match claude-code");
    expect(useSessionStore.getState().sessions[0].credential_id).toBe("c1");
  });

  it.each([false, true])("keeps an earlier session's rejection out of the selected session (late=%s)", async (late) => {
    let reject!: (response: Response) => void;
    fetchMock.mockImplementation(() => new Promise<Response>((resolve) => { reject = resolve; }));
    const a = session({ id: "a", credential_id: "c1" });
    const b = session({ id: "b", credential_id: "c1" });
    const { rerender } = mount(a, [credential()]);
    openMenu();
    fireEvent.click(screen.getByText(/Host default sign-in/));
    await waitFor(() => expect(reject).toBeTypeOf("function"));
    const refused = () => reject(new Response(JSON.stringify({ detail: "Original session A rejection" }), { status: 400 }));
    if (!late) {
      await act(async () => refused());
      expect(await screen.findByRole("alert")).toHaveTextContent("Original session A rejection");
    }
    rerender(<CredentialPicker session={b} />);
    if (late) await act(async () => refused());
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByLabelText(/Change this session/)).toBeEnabled();
  });

  it("offers only credentials for this session's engine", () => {
    mount(session(), [
      credential(),
      credential({ id: "c2", label: "codex-gpt", backend: "codex" }),
    ]);
    openMenu();
    expect(screen.getByText("archer-cc")).toBeTruthy();
    expect(screen.queryByText("codex-gpt")).toBeNull();
  });

  it("PATCHes the session and updates the store when one is picked", async () => {
    mount(session(), [credential({ id: "c2", label: "new-key" })]);
    openMenu();
    fireEvent.click(screen.getByText("new-key"));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([u, init]) => {
          const body = (init as RequestInit | undefined)?.body;
          return (
            String(u).endsWith("/api/sessions/s1") &&
            (init as RequestInit)?.method === "PATCH" &&
            typeof body === "string" &&
            JSON.parse(body).credential_id === "c2"
          );
        })
      ).toBe(true)
    );
    await waitFor(() =>
      expect(useSessionStore.getState().sessions[0].credential_id).toBe("c2")
    );
  });

  it("can fall back to the agent's credential with an explicit null", async () => {
    mount(session({ credential_id: "c1" }), [credential()]);
    openMenu();
    fireEvent.click(screen.getByText(/Host default sign-in|Agent's credential/));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => {
          const body = (init as RequestInit | undefined)?.body;
          return typeof body === "string" && JSON.parse(body).credential_id === null;
        })
      ).toBe(true)
    );
  });

  it("flags a lapsed credential in the list", () => {
    mount(session(), [credential({ needs_reconnect: true })]);
    openMenu();
    expect(screen.getByText("expired")).toBeTruthy();
  });

  it("says so when this engine has no credentials at all", () => {
    mount(session(), [credential({ id: "c9", backend: "codex" })]);
    openMenu();
    expect(screen.getByText(/No claude-code credentials/)).toBeTruthy();
  });
});

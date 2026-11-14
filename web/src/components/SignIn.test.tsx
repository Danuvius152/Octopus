/** Setup proof is exchanged outside account state; unknown state stays closed. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { SignIn } from "./SignIn";
import { useSessionStore } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** A `fetch` double that answers `/api/auth/state` and records every POST. */
function stubFetch({
  accountsExist,
  post,
}: {
  accountsExist: boolean;
  post?: (path: string, body: unknown) => { ok: boolean; body: unknown };
}) {
  const calls: { path: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = String(url).replace(window.location.origin, "");
      if (!init || init.method !== "POST") {
        return { ok: true, json: async () => ({ accounts_exist: accountsExist }) };
      }
      const body = JSON.parse(String(init.body));
      calls.push({ path, body });
      const reply = post?.(path, body) ?? { ok: true, body: { token: "issued" } };
      return { ok: reply.ok, status: reply.ok ? 200 : 401, json: async () => reply.body };
    })
  );
  return calls;
}

describe("SignIn", () => {
  beforeEach(() => {
    useSessionStore.getState().setToken("");
    stubFetch({ accountsExist: false });
  });

  it.each(["login", "setup", "register"] as const)("admits only one %s POST across Enter/click, freezes fields and allows retry after rejection", async mode => {
    const replies: ReturnType<typeof deferred<Response>>[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: unknown, init?: RequestInit) => {
      if (init?.method !== "POST") return Response.json({ accounts_exist: mode !== "setup" });
      const reply = deferred<Response>(); replies.push(reply); return reply.promise;
    }));
    const signedIn = vi.fn();
    render(<SignIn onSignedIn={signedIn} />);
    await screen.findByLabelText("Username");
    if (mode === "register") fireEvent.click(screen.getByText("I have an invite code"));
    const username = screen.getByLabelText(mode === "register" ? "Choose a username" : "Username");
    const password = screen.getByLabelText(mode === "register" ? "Choose a password" : "Password");
    fireEvent.change(username, { target: { value: "owner" } });
    fireEvent.change(password, { target: { value: "password1" } });
    if (mode === "setup") fireEvent.change(screen.getByLabelText("Setup token"), { target: { value: "proof" } });
    if (mode === "register") fireEvent.change(screen.getByLabelText("Invite code"), { target: { value: "invite" } });
    for (const flags of [{ isComposing: true }, { keyCode: 229 }]) {
      const event = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true, ...flags });
      fireEvent(password, event);
      expect(event.defaultPrevented).toBe(false);
    }
    expect(replies).toHaveLength(0);
    const button = screen.getByRole("button", { name: mode === "setup" ? "Set up Octopus" : mode === "register" ? "Create account" : "Sign in" });
    act(() => { fireEvent.keyDown(password, { key: "Enter" }); button.click(); fireEvent.keyDown(username, { key: "Enter" }); });
    expect(replies).toHaveLength(1);
    for (const field of document.querySelectorAll("input")) expect(field).toBeDisabled();
    await act(async () => replies[0].resolve(Response.json({ detail: "Try again" }, { status: 401 })));
    expect(username).toBeEnabled();
    expect(password).toHaveValue("password1");
    expect(screen.getByText("Try again")).toBeVisible();
    fireEvent.click(button);
    expect(replies).toHaveLength(2);
    await act(async () => replies[1].resolve(Response.json({ token: "current-token" })));
    expect(signedIn).toHaveBeenCalledExactlyOnceWith("current-token");
  });

  it.each(["mode", "mode round trip", "account round trip"])("keeps a successor sign-in locked when an old reply arrives after %s", async transition => {
    const replies: ReturnType<typeof deferred<Response>>[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: unknown, init?: RequestInit) => {
      if (init?.method !== "POST") return Response.json({ accounts_exist: true });
      const reply = deferred<Response>(); replies.push(reply); return reply.promise;
    }));
    const signedIn = vi.fn();
    render(<SignIn onSignedIn={signedIn} />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign in" }));
    if (transition.startsWith("mode")) {
      fireEvent.click(screen.getByText("I have an invite code"));
      if (transition === "mode round trip") fireEvent.click(screen.getByText("Back to sign in"));
    } else act(() => { useSessionStore.getState().setToken("different-account"); useSessionStore.getState().setToken(""); });
    const currentButton = screen.getByRole("button", { name: transition === "mode" ? "Create account" : "Sign in" });
    fireEvent.click(currentButton);
    expect(replies).toHaveLength(2);
    await act(async () => replies[0].resolve(Response.json({ token: "obsolete-token" })));
    expect(signedIn).not.toHaveBeenCalled();
    expect(currentButton).toBeDisabled();
    await act(async () => replies[1].resolve(Response.json({ token: "current-token" })));
    expect(signedIn).toHaveBeenCalledExactlyOnceWith("current-token");
  });

  it("requires first-account credentials and a setup-only proof", async () => {
    render(<SignIn onSignedIn={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText("Setup token")).toBeTruthy());
    expect(screen.getByLabelText("Username")).toBeTruthy();
    expect(screen.getByLabelText("Password")).toBeTruthy();
  });

  it("exchanges setup proof without storing it as an account bearer", async () => {
    const calls = stubFetch({ accountsExist: false });
    const seen: string[] = [];
    render(<SignIn onSignedIn={(t) => seen.push(t)} />);
    await screen.findByLabelText("Setup token");
    fireEvent.change(screen.getByLabelText("Setup token"), {
      target: { value: "  setup-proof  " },
    });
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "owner" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "owner-password" } });
    fireEvent.click(screen.getByText("Set up Octopus"));
    await waitFor(() => expect(seen).toEqual(["issued"]));
    expect(calls).toEqual([{ path: "/api/auth/setup", body: { username: "owner", password: "owner-password" } }]);
    const post = vi.mocked(globalThis.fetch).mock.calls.find(([, init]) => init?.method === "POST");
    expect(new Headers(post?.[1]?.headers).get("Authorization")).toBe("Bearer setup-proof");
    expect(useSessionStore.getState().token).toBe("");
    expect(JSON.stringify(localStorage)).not.toContain("setup-proof");
  });

  it("asks for a username and password once an account exists", async () => {
    stubFetch({ accountsExist: true });
    render(<SignIn onSignedIn={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText("Username")).toBeTruthy());
    expect(screen.queryByLabelText("Token")).toBeNull();
  });

  it("exchanges the password for the bearer the server issues", async () => {
    const calls = stubFetch({ accountsExist: true });
    const seen: string[] = [];
    render(<SignIn onSignedIn={(t) => seen.push(t)} />);
    await waitFor(() => expect(screen.getByLabelText("Username")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "archer" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByText("Sign in"));
    await waitFor(() => expect(seen).toEqual(["issued"]));
    expect(calls).toEqual([
      { path: "/api/auth/login", body: { username: "archer", password: "pw" } },
    ]);
    // The password itself is never what gets stored.
    expect(seen[0]).not.toBe("pw");
  });

  it("shows the server's reason when sign-in is refused", async () => {
    stubFetch({
      accountsExist: true,
      post: () => ({ ok: false, body: { detail: "Too many attempts" } }),
    });
    render(<SignIn onSignedIn={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText("Username")).toBeTruthy());
    fireEvent.click(screen.getByText("Sign in"));
    await waitFor(() => expect(screen.getByText("Too many attempts")).toBeTruthy());
  });

  it("registers with an invite code", async () => {
    const calls = stubFetch({ accountsExist: true });
    const seen: string[] = [];
    render(<SignIn onSignedIn={(t) => seen.push(t)} />);
    await waitFor(() => expect(screen.getByLabelText("Username")).toBeTruthy());
    fireEvent.click(screen.getByText("I have an invite code"));
    fireEvent.change(screen.getByLabelText("Invite code"), {
      target: { value: "code-1" },
    });
    fireEvent.change(screen.getByLabelText("Choose a username"), {
      target: { value: "vera" },
    });
    fireEvent.change(screen.getByLabelText("Choose a password"), {
      target: { value: "pw" },
    });
    fireEvent.click(screen.getByText("Create account"));
    await waitFor(() => expect(seen).toEqual(["issued"]));
    expect(calls[0]).toEqual({
      path: "/api/auth/register",
      body: { invite_code: "code-1", username: "vera", password: "pw" },
    });
  });

  it("keeps all credential forms closed until installation state is confirmed", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<SignIn onSignedIn={() => {}} />);
    expect(screen.queryByRole("button", { name: "Connect" })).toBeNull();
    expect(screen.queryByLabelText("Setup token")).toBeNull();
    expect(screen.queryByLabelText("Username")).toBeNull();
    expect(screen.getByRole("status")).toBeTruthy();
  });

  it("requires an explicit retry when the server can't be reached", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    render(<SignIn onSignedIn={() => {}} />);
    await screen.findByRole("button", { name: "Retry" });
    expect(screen.queryByLabelText("Setup token")).toBeNull();
    expect(screen.queryByLabelText("Username")).toBeNull();
    stubFetch({ accountsExist: true });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByLabelText("Username");
  });

  it.each([null, {}, { accounts_exist: null }, { accounts_exist: 0 }, { accounts_exist: "false" }])(
    "rejects an unconfirmed installation state (%j)", async (body) => {
      vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => body })));
      render(<SignIn onSignedIn={() => {}} />);
      await screen.findByRole("button", { name: "Retry" });
      expect(screen.queryByLabelText("Setup token")).toBeNull();
      expect(screen.queryByLabelText("Username")).toBeNull();
    }
  );

  it.each(["http", "json"])("rejects a failed state response (%s)", async (failure) => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: failure !== "http", json: async () => { throw new Error("Invalid JSON"); },
    })));
    render(<SignIn onSignedIn={() => {}} />);
    await screen.findByRole("button", { name: "Retry" });
    expect(screen.queryByLabelText("Setup token")).toBeNull();
    expect(screen.queryByLabelText("Username")).toBeNull();
  });

  it.each(["headers", "body", "unmount"])("ignores retired setup replies (%s)", async (phase) => {
    let finish!: () => void;
    let started = false;
    const reply = { ok: true, json: async () => ({ token: "new-setup-bearer" }) };
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method !== "POST") return { ok: true, json: async () => ({ accounts_exist: false }) };
      if (phase === "body") return {
        ok: true, json: () => new Promise((resolve) => {
          started = true;
          finish = () => resolve({ token: "new-setup-bearer" });
        }),
      };
      return new Promise((resolve) => { started = true; finish = () => resolve(reply); });
    }));
    const signedIn = vi.fn();
    const view = render(<SignIn onSignedIn={signedIn} />);
    await screen.findByLabelText("Setup token");
    fireEvent.change(screen.getByLabelText("Setup token"), { target: { value: "setup-proof" } });
    fireEvent.click(screen.getByText("Set up Octopus"));
    await waitFor(() => expect(started).toBe(true));
    if (phase === "unmount") view.unmount();
    else act(() => useSessionStore.getState().setToken("current-account"));
    await act(async () => finish());
    expect(signedIn).not.toHaveBeenCalled();
    expect(localStorage.getItem("octopus_token")).not.toBe("new-setup-bearer");
  });
});

/**
 * Who this browser is signed in as, asked once.
 *
 * It matters that a failure leaves the identity `null` rather than inventing
 * one: `null` means "not known yet", and the sidebar reads it as "show the
 * operator's rows", which is right for an install that has no accounts and for
 * a moment before the answer lands. An invented `is_admin: false` would hide
 * the Monitor page from the operator of a single-user install.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { loadIdentity } from "./loadIdentity";
import { useSessionStore } from "../stores/sessionStore";

beforeEach(() => {
  useSessionStore.getState().setToken("a-token");
  useSessionStore.getState().setIdentity(null);
});

afterEach(() => vi.unstubAllGlobals());

describe("loadIdentity", () => {
  it.each([200, 401])("ignores an old account identity response (%i)", async (status) => {
    let finish!: (value: unknown) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { finish = resolve; })));
    const pending = loadIdentity();
    useSessionStore.getState().setToken("b-token");
    const identity = { label: "B", user_id: "b", is_admin: false };
    useSessionStore.getState().setIdentity(identity);
    finish({ ok: status === 200, status, json: async () => ({ label: "A", user_id: "a", is_admin: true }) });
    await pending;
    expect(useSessionStore.getState().token).toBe("b-token");
    expect(useSessionStore.getState().identity).toEqual(identity);
  });

  it("clears account data on identity loss and preserves a verified token rotation", () => {
    const store = useSessionStore.getState();
    store.setIdentity({ label: "A", user_id: "a", is_admin: true });
    store.setActiveSessionId("private-a");
    store.setMessages("private-a", [{ role: "user", type: "text", content: "private" }]);
    const generation = useSessionStore.getState().accountGeneration;
    store.setToken("rotated-a", { sameAccount: true });
    expect(useSessionStore.getState().accountGeneration).toBe(generation);
    expect(useSessionStore.getState().messages["private-a"]).toHaveLength(1);
    store.setToken("");
    expect(useSessionStore.getState().messages).toEqual({});
    expect(useSessionStore.getState().activeSessionId).toBeNull();
    expect(useSessionStore.getState().identity).toBeNull();
    expect(useSessionStore.getState().accountGeneration).toBe(generation + 1);
  });
  it("stores what the route answers", async () => {
    const body = { label: "archer", user_id: "u1", is_admin: true };
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => body })));
    await loadIdentity();
    expect(useSessionStore.getState().identity).toEqual(body);
  });

  it("sends the bearer", async () => {
    const calls: RequestInit[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        calls.push(init);
        return { ok: true, json: async () => ({ label: "x", user_id: null, is_admin: false }) };
      })
    );
    await loadIdentity();
    expect(
      (calls[0].headers as Record<string, string>).Authorization
    ).toBe("Bearer a-token");
  });

  it("clears the token on 401 so the app falls back to sign-in", async () => {
    useSessionStore.getState().setIdentity({ label: "x", user_id: "u", is_admin: false });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 401, json: async () => null }))
    );
    await loadIdentity();
    // The dead token is dropped — App renders <SignIn> when token is falsy —
    // rather than left to 401 every request and strand an empty main view.
    expect(useSessionStore.getState().token).toBe("");
    expect(useSessionStore.getState().identity).toBeNull();
  });

  it("keeps the token on a server error (a restart is not a logout)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 503, json: async () => null }))
    );
    await loadIdentity();
    expect(useSessionStore.getState().token).toBe("a-token");
  });

  it("leaves the identity unknown when the server is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    await loadIdentity();
    expect(useSessionStore.getState().identity).toBeNull();
  });

  it("does nothing without a bearer", async () => {
    useSessionStore.getState().setToken("");
    const spy = vi.fn();
    vi.stubGlobal("fetch", spy);
    await loadIdentity();
    expect(spy).not.toHaveBeenCalled();
  });
});

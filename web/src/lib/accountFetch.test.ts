import { afterEach, expect, it, vi } from "vitest";
import { accountFetch } from "./accountFetch";
import { useSessionStore } from "../stores/sessionStore";

afterEach(() => vi.unstubAllGlobals());

it("rejects an old body even after its response headers arrived", async () => {
  useSessionStore.getState().setToken("a");
  let finish!: (body: unknown) => void;
  vi.stubGlobal("fetch", vi.fn(async () => ({ json: () => new Promise((resolve) => { finish = resolve; }) })));
  const response = await accountFetch("/api/sessions", { headers: { Authorization: "Bearer a" } });
  const body = response.json();
  useSessionStore.getState().setToken("b");
  finish([{ id: "private-a" }]);
  await expect(body).rejects.toMatchObject({ name: "AbortError" });
});

it("refuses a callback carrying credentials from a retired sign-in", async () => {
  useSessionStore.getState().setToken("b");
  const spy = vi.fn();
  vi.stubGlobal("fetch", spy);
  await expect(accountFetch("/api/sessions", { headers: { Authorization: "Bearer a" } }))
    .rejects.toMatchObject({ name: "AbortError" });
  expect(spy).not.toHaveBeenCalled();
});

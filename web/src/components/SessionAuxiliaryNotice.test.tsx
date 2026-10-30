import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionStore } from "../stores/sessionStore";
import { SessionAuxiliaryNotice } from "./SessionAuxiliaryNotice";

beforeEach(() => useSessionStore.setState({ ...useSessionStore.getInitialState(), token: "synthetic-token" }, true));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("retries auxiliary data without reloading the transcript or losing live cards", async () => {
  useSessionStore.getState().setSessionAuxiliaryError("s", "background", "Could not refresh background task details.");
  let complete!: (response: Response) => void;
  const fetcher = vi.fn((_url: RequestInfo | URL) => new Promise<Response>((resolve) => { complete = resolve; }));
  vi.stubGlobal("fetch", fetcher);
  render(<SessionAuxiliaryNotice sessionId="s" />);
  fireEvent.click(screen.getByRole("button", { name: "Retry background task details" }));
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0][0]).toContain("/s/bg-tasks");
  expect(screen.queryByRole("alert")).toBeNull();
  await act(async () => complete(Response.json([])));
  await waitFor(() => expect(useSessionStore.getState().bgTasks.s).toEqual([]));
  expect(useSessionStore.getState().messages.s).toBeUndefined();
});

it("drops old-account retry failures without showing them to the next account", async () => {
  useSessionStore.getState().setSessionAuxiliaryError("s", "research", "Could not refresh research updates.");
  let fail!: (error: Error) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((_resolve, reject) => { fail = reject; })));
  render(<SessionAuxiliaryNotice sessionId="s" />);
  fireEvent.click(screen.getByRole("button", { name: "Retry research updates" }));
  act(() => useSessionStore.getState().setToken("next-account"));
  await act(async () => fail(new Error("offline")));
  expect(screen.queryByRole("alert")).toBeNull();
});

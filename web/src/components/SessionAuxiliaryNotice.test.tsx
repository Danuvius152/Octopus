import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionStore } from "../stores/sessionStore";
import { SessionAuxiliaryNotice } from "./SessionAuxiliaryNotice";
import { BgTaskChip } from "./BgTaskChip";

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
  expect(fetcher.mock.calls[0][0]).toContain("/s/bg-task-summaries");
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

it("keeps the retry attached to its transcript when a virtual row unmounts", () => {
  const state = useSessionStore.getState();
  state.setMessages("s", [{
    role: "assistant", type: "tool_use", tool_name: "mcp__bg__run",
    runtime_metadata: { request_id: "request", attempt_id: "attempt", tool_call_id: "call" },
  }, {
    role: "tool", type: "tool_result",
    runtime_metadata: { request_id: "request", attempt_id: "attempt", tool_call_id: "call",
      receipt: { kind: "bg", task_id: "task" } },
  }]);
  state.setAuxiliaryTaskFailure("background", "s", "task", true);
  const notice = <SessionAuxiliaryNotice sessionId="s" />;
  const content = () => <>{notice}<BgTaskChip sessionId="s" taskId="task" /></>;
  const view = render(content());
  expect(screen.getByRole("button", { name: "Retry task summary" })).toBeVisible();
  view.rerender(<>{notice}</>);
  expect(screen.queryByRole("alert")).toBeNull();
  view.rerender(content());
  expect(screen.getByRole("button", { name: "Retry task summary" })).toBeVisible();
  act(() => useSessionStore.getState().setMessages("s", []));
  view.rerender(<>{notice}</>);
  expect(screen.getByRole("button", { name: "Retry background task details" })).toBeVisible();
});

import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { AccountPage } from "./AccountPage";
import { useSessionStore } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";

const writes: { token: string | null; body: unknown; response: ReturnType<typeof deferred<Response>> }[] = [];

beforeEach(() => {
  useSessionStore.getState().setToken("password-account-a");
  writes.length = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), window.location.origin).pathname;
    const token = new Headers(init?.headers).get("Authorization");
    if (path.endsWith("/password")) {
      const response = deferred<Response>();
      writes.push({ token, body: JSON.parse(String(init?.body)), response });
      return response.promise;
    }
    if (path.endsWith("/identity")) return Response.json({ user_id: "password-owner", label: "Password owner", is_admin: false });
    if (path.endsWith("/workspace")) return Response.json({ workspace: "/workspace", extra_roots: [], confined: true });
    throw new Error(`Unexpected password test request: ${path}`);
  }));
});

afterEach(async () => {
  cleanup();
  useSessionStore.getState().setToken("");
  await act(async () => { for (const write of writes) write.response.reject(new Error("Original form retired")); });
  vi.unstubAllGlobals();
});

async function mount() {
  render(<AccountPage />);
  await screen.findByLabelText("Current password");
  return {
    current: screen.getByLabelText("Current password"), next: screen.getByLabelText("New password"),
    submit: screen.getByRole("button", { name: "Change password" }),
  };
}

function fill(current = "original password", next = "new password") {
  fireEvent.change(screen.getByLabelText("Current password"), { target: { value: current } });
  fireEvent.change(screen.getByLabelText("New password"), { target: { value: next } });
}

it("leaves both IME composition Enter forms to the editor and submits once on ordinary Enter", async () => {
  const form = await mount();
  fill();
  fireEvent.keyDown(form.next, { key: "Enter", isComposing: true });
  fireEvent.keyDown(form.next, { key: "Enter", keyCode: 229 });
  expect(writes).toHaveLength(0);
  expect(fireEvent.keyDown(form.next, { key: "Enter" })).toBe(false);
  expect(writes).toHaveLength(1);
  await act(async () => writes[0].response.resolve(new Response(null, { status: 204 })));
  expect(form.current).toHaveValue("");
  expect(form.next).toHaveValue("");
  expect(form.submit).toBeEnabled();
  expect(screen.getByText("Password changed.")).toBeVisible();
});

it.each(["click/Enter", "Enter/click", "Enter/Enter"])("admits one same-batch password POST for %s", async (sequence) => {
  const form = await mount();
  fill();
  act(() => {
    for (const event of sequence.split("/")) {
      if (event === "click") fireEvent.click(form.submit);
      else fireEvent.keyDown(form.next, { key: "Enter" });
    }
  });
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({ token: "Bearer password-account-a",
    body: { current_password: "original password", new_password: "new password" } });
  expect(form.submit).toBeDisabled();
  await act(async () => writes[0].response.resolve(Response.json({ detail: "Current password was refused" }, { status: 403 })));
  expect(screen.getByText("Current password was refused")).toBeVisible();
  expect(form.current).toHaveValue("original password");
  expect(form.next).toHaveValue("new password");
  expect(form.submit).toBeEnabled();
  fireEvent.keyDown(form.next, { key: "Enter" });
  expect(writes).toHaveLength(2);
});

it.each(["success", "refusal", "network", "refusal body"])("retires the old password form through same-batch A→B→A before its %s settles", async (reply) => {
  const original = await mount();
  fill();
  fireEvent.click(original.submit);
  const body = deferred<unknown>();
  if (reply === "refusal body") {
    const response = new Response(null, { status: 409 });
    response.json = () => body.promise;
    await act(async () => writes[0].response.resolve(response));
  }
  act(() => {
    useSessionStore.getState().setToken("password-account-b");
    useSessionStore.getState().setToken("password-account-a");
  });
  const current = await screen.findByLabelText("Current password");
  const next = screen.getByLabelText("New password");
  const submit = screen.getByRole("button", { name: "Change password" });
  expect(current).toHaveValue("");
  expect(next).toHaveValue("");
  expect(submit).toBeEnabled();
  fill("current proof after return", "new password after return");
  fireEvent.click(submit);
  expect(writes).toHaveLength(2);
  await act(async () => {
    if (reply === "network") writes[0].response.reject(new TypeError("Old connection failed"));
    else if (reply === "refusal body") body.resolve({ detail: "Old form refusal" });
    else writes[0].response.resolve(reply === "success" ? new Response(null, { status: 204 })
      : Response.json({ detail: "Old form refusal" }, { status: 409 }));
  });
  expect(submit).toBeDisabled();
  expect(current).toHaveValue("current proof after return");
  expect(next).toHaveValue("new password after return");
  expect(screen.queryByText("Old form refusal")).toBeNull();
  expect(screen.queryByText("Could not reach Octopus.")).toBeNull();
  expect(screen.queryByText("Password changed.")).toBeNull();
  await act(async () => writes[1].response.resolve(new Response(null, { status: 204 })));
  expect(submit).toBeEnabled();
  expect(current).toHaveValue("");
  expect(next).toHaveValue("");
  expect(screen.getByText("Password changed.")).toBeVisible();
});

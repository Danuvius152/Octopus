import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ConnectorsPage } from "./ConnectorsPage";
import { useSessionStore } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";

const catalog = ["gmail", "github"].map((kind) => ({ kind, display_name: kind, available: false }));
let fetcher: ReturnType<typeof vi.fn>;

function background(url: string) {
  return Response.json(url.endsWith("/catalog") ? catalog : []);
}
async function open(kind = "gmail") {
  const card = (await screen.findByText(kind)).closest(".connector-catalog-item") as HTMLElement;
  fireEvent.click(within(card).getByRole("button", { name: "Set up" }));
}
function mount() {
  render(<ConnectorsPage onToggleSidebar={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "+ New Connector" }));
}
beforeEach(() => {
  useSessionStore.setState({
    ...useSessionStore.getInitialState(), token: "setup-account",
    accountGeneration: useSessionStore.getState().accountGeneration + 1,
  });
  fetcher = vi.fn(async (url: string) => background(url));
  vi.stubGlobal("fetch", fetcher);
});
afterEach(() => {
  cleanup(); useSessionStore.getState().setToken("");
  vi.unstubAllGlobals(); vi.restoreAllMocks();
});

it("a late setup read fills the redirect without overwriting an edited client ID or secret", async () => {
  const read = deferred<Response>();
  const puts: unknown[] = [];
  fetcher.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/oauth-client")) {
      if (init?.method !== "PUT") return read.promise;
      puts.push(JSON.parse(init.body as string));
      return Response.json({ configured: true });
    }
    return background(url);
  });
  mount();
  await open();
  fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "my-id" } });
  fireEvent.change(screen.getByLabelText("Client secret"), { target: { value: "my-secret" } });
  await act(async () => read.resolve(Response.json({ client_id: "saved-id", redirect_uri: "https://public.test/callback" })));
  expect(screen.getByLabelText("Client ID")).toHaveValue("my-id");
  expect(screen.getByLabelText("Client secret")).toHaveValue("my-secret");
  expect(screen.getByText("https://public.test/callback")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await act(async () => {});
  expect(puts).toEqual([{ client_id: "my-id", client_secret: "my-secret" }]);
});

it.each(["other kind", "same kind", "close reopen", "account ABA"] as const)(
  "a retired GET cannot supply credentials or redirect to a new %s draft",
  async (transition) => {
    const old = deferred<Response>();
    let reads = 0;
    const puts: { url: string; body: unknown }[] = [];
    fetcher.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/oauth-client")) {
        if (init?.method === "PUT") {
          puts.push({ url, body: JSON.parse(init.body as string) });
          return Response.json({ configured: true });
        }
        if (++reads === 1) return old.promise;
        return Response.json({ client_id: "current-id", redirect_uri: "https://current.test/callback" });
      }
      return background(url);
    });
    mount();
    await open();
    if (transition === "account ABA") {
      act(() => {
        useSessionStore.getState().setToken("other-account");
        useSessionStore.getState().setToken("setup-account");
      });
      expect(screen.queryByLabelText("Client ID")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "+ New Connector" }));
    } else if (transition === "close reopen") {
      fireEvent.click(screen.getByRole("button", { name: "Close" }));
      fireEvent.click(screen.getByRole("button", { name: "+ New Connector" }));
    } else fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await open(transition === "other kind" ? "github" : "gmail");
    await act(async () => {});
    fireEvent.change(screen.getByLabelText("Client secret"), { target: { value: "current-secret" } });
    await act(async () => old.resolve(Response.json({ client_id: "old-id", redirect_uri: "https://old.test/callback" })));
    expect(screen.getByLabelText("Client ID")).toHaveValue("current-id");
    expect(screen.getByText("https://current.test/callback")).toBeVisible();
    expect(screen.queryByText("https://old.test/callback")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await act(async () => {});
    expect(puts).toEqual([{
      url: expect.stringContaining(`/connectors/${transition === "other kind" ? "github" : "gmail"}/oauth-client`),
      body: { client_id: "current-id", client_secret: "current-secret" },
    }]);
  },
);

it.each([
  ["success", "same kind"], ["failure", "same kind"],
  ["success", "other kind"], ["failure", "other kind"],
  ["success", "close reopen"], ["failure", "account ABA"],
] as const)("an old save %s cannot close or finish a new %s draft", async (outcome, transition) => {
  const old = deferred<Response>();
  const current = deferred<Response>();
  const puts: { url: string; body: unknown }[] = [];
  fetcher.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/oauth-client")) {
      if (init?.method === "PUT") {
        puts.push({ url, body: JSON.parse(init.body as string) });
        return puts.length === 1 ? old.promise : current.promise;
      }
      return Response.json({ client_id: "saved-id", redirect_uri: `https://current.test/callback` });
    }
    return background(url);
  });
  mount();
  await open();
  fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "gmail-id" } });
  fireEvent.change(screen.getByLabelText("Client secret"), { target: { value: "gmail-secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  if (transition === "account ABA") {
    act(() => {
      useSessionStore.getState().setToken("other-account");
      useSessionStore.getState().setToken("setup-account");
    });
    fireEvent.click(screen.getByRole("button", { name: "+ New Connector" }));
  } else if (transition === "close reopen") {
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "+ New Connector" }));
  } else fireEvent.click(screen.getByRole("button", { name: "Back" }));
  const kind = transition === "other kind" ? "github" : "gmail";
  await open(kind);
  fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "github-id" } });
  fireEvent.change(screen.getByLabelText("Client secret"), { target: { value: "github-secret" } });
  expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await act(async () => old.resolve(outcome === "success"
    ? Response.json({ configured: true }) : Response.json({ detail: "Old save refused" }, { status: 503 })));
  expect(screen.getByLabelText("Client ID")).toHaveValue("github-id");
  expect(screen.getByLabelText("Client secret")).toHaveValue("github-secret");
  expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
  expect(screen.queryByText("Old save refused")).toBeNull();
  expect(puts).toEqual([
    { url: expect.stringContaining("/gmail/oauth-client"), body: { client_id: "gmail-id", client_secret: "gmail-secret" } },
    { url: expect.stringContaining(`/${kind}/oauth-client`), body: { client_id: "github-id", client_secret: "github-secret" } },
  ]);
  await act(async () => current.resolve(Response.json({ configured: true })));
  expect(screen.queryByLabelText("Client ID")).toBeNull();
});

it("a custom save cannot retire a setup draft opened while its receipt is pending", async () => {
  const custom = deferred<Response>();
  fetcher.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/custom") && init?.method === "POST") return custom.promise;
    if (url.endsWith("/oauth-client")) return Response.json({ client_id: "setup-id" });
    return background(url);
  });
  mount();
  fireEvent.click(screen.getByRole("button", { name: /Add custom connector/ }));
  for (const [label, value] of [
    ["Slug", "custom"], ["Authorize URL", "https://local.test/auth"],
    ["Token URL", "https://local.test/token"], ["API base URL", "https://local.test/api"],
    ["Client ID", "custom-id"], ["Client secret", "custom-secret"],
  ]) fireEvent.change(screen.getByLabelText(label), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  await open("github");
  fireEvent.change(screen.getByLabelText("Client secret"), { target: { value: "setup-secret" } });
  await act(async () => custom.resolve(Response.json({ kind: "custom" })));
  expect(screen.getByLabelText("Client ID")).toHaveValue("setup-id");
  expect(screen.getByLabelText("Client secret")).toHaveValue("setup-secret");
  expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
});

it("clearing a setup ID counts as an edit when the initial read arrives", async () => {
  const read = deferred<Response>();
  fetcher.mockImplementation(async (url: string) => url.endsWith("/oauth-client") ? read.promise : background(url));
  mount();
  await open();
  fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "temporary" } });
  fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "" } });
  await act(async () => read.resolve(Response.json({ client_id: "saved-id" })));
  expect(screen.getByLabelText("Client ID")).toHaveValue("");
});

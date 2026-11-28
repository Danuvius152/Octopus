import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { FileViewerDialog } from "./FileViewerDialog";
import { mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";

const a = { id: "view-a", name: "A", status: "idle", backend: "codex", working_dir: "/tmp", created_at: "now" } as SessionInfo;
const b = { ...a, id: "view-b", name: "B" };
const reads: ReturnType<typeof deferred<Response>>[] = [];
beforeEach(() => {
  mockChatViewport();
  useSessionStore.getState().setToken("");
  useSessionStore.getState().setToken("view-token");
  useSessionStore.setState({ mainView: "chat", activeSessionId: a.id, sessions: [a, b], archivedSessions: [], agents: [], messages: {},
    pendingQuestions: {}, streamingText: {}, dispatchStates: {}, dispatchUnavailable: {}, pendingForks: {}, credentials: [], viewer: null });
  reads.length = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    if (String(url).endsWith("/showme/resolve") || init?.method === "PATCH") {
      const reply = deferred<Response>(); reads.push(reply); return reply.promise;
    }
    const location = new URL(String(url), window.location.origin);
    if (location.pathname.endsWith("/files/meta")) {
      const path = location.searchParams.get("path")!;
      return Response.json(markdown(path));
    }
    if (location.pathname.endsWith("/files")) return new Response("# Opened document");
    return Response.json(String(url).endsWith("/submissions") ? { items: [], next_cursor: null } : []);
  }));
});
afterEach(() => { cleanup(); useSessionStore.getState().setToken(""); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function markdown(path: string) {
  return { path, absolute_path: `/tmp/${path}`, source: "/tmp", canonical_path_id: path,
    canonical_directory: { path: ".", path_id: "directory" }, kind: "markdown", size: 17 };
}
async function mount(strict = false, interrupt = vi.fn()) {
  let view!: ReturnType<typeof render>;
  const chat = <VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={interrupt} answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
    <FileViewerDialog />
  </VirtuosoMockContext.Provider>;
  await act(async () => { view = render(strict ? <StrictMode>{chat}</StrictMode> : chat); });
  return view;
}
async function command(text: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value: text } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Send message" })); });
}

it("closing a file with Escape returns focus without interrupting a running chat", async () => {
  useSessionStore.setState({
    sessions: [{ ...a, status: "running" }, b],
  });
  const interrupt = vi.fn();
  await mount(false, interrupt);
  const input = screen.getByRole("textbox");
  input.focus();
  fireEvent.change(input, { target: { value: "/showme report.md" } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: /Queue message|Send to running turn/ })); });
  await act(async () => reads[0].resolve(Response.json({ path: "report.md", metadata: markdown("report.md") })));
  const close = await screen.findByRole("button", { name: "Close" });
  await screen.findByRole("heading", { name: "Opened document" });
  fireEvent.keyDown(close, { key: "Escape" });
  await act(async () => {});
  expect(screen.queryByRole("dialog")).toBeNull();
  await vi.waitFor(() => expect(input).toHaveFocus());
  expect(interrupt).not.toHaveBeenCalled();
});

it("searches an ambiguous filename only on request and lets the user clarify once per intent", async () => {
  await mount(true);
  await command("/showme 请打开 plan.md");
  await screen.findByRole("status");
  await act(async () => reads[0].resolve(Response.json({ detail: "File not found.",
    target: { path: "请打开 plan.md" }, can_search_description: true }, { status: 404 })));
  expect(reads).toHaveLength(1);
  fireEvent.click(await screen.findByRole("button", { name: "Search by description" }));
  await screen.findByText("Searching the conversation…");
  await act(async () => reads[1].resolve(Response.json({ description: "请打开 plan.md", path: null, message: "Which plan?" })));
  expect(screen.getByText("Which plan?")).toBeVisible();
  const field = screen.getByRole("textbox", { name: "File description" });
  expect(field).toHaveValue("请打开 plan.md");
  fireEvent.change(field, { target: { value: "the implementation plan.md" } });
  fireEvent.click(screen.getByRole("button", { name: "Search conversation" }));
  await screen.findByText("Searching the conversation…");
  await act(async () => reads[2].resolve(Response.json({ description: "the implementation plan.md", path: "plan.md", metadata: markdown("plan.md") })));
  await screen.findByRole("heading", { name: "Opened document" });
  const resolutionBodies = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/showme/resolve"))
    .map(([, init]) => JSON.parse(String(init?.body)));
  expect(resolutionBodies).toEqual([
    { text: "请打开 plan.md" },
    { text: "请打开 plan.md", mode: "description" },
    { text: "the implementation plan.md", mode: "description" },
  ]);
  expect(Object.values(useSessionStore.getState().messages).flat()).toEqual([]);
});

it.each(["plan.md", "the original report"])("keeps one lookup for a slow unresolved /showme %s", async (input) => {
  await mount(true);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  try {
    await command(`/showme ${input}`);
    expect(reads).toHaveLength(1);
    await act(async () => vi.advanceTimersByTime(5000));
    expect(screen.getByText("The file has not finished loading.")).toBeVisible();
    expect(screen.queryByRole("button", { name: /^Retry$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reload file" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close" })).toBeVisible();

    await act(async () => reads[0].resolve(Response.json({
      path: "plan.md", metadata: markdown("plan.md"), ...(input === "plan.md" ? {} : { description: input }),
    })));
    expect(screen.getByRole("heading", { name: "Opened document" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Reload file" })).toBeVisible();
    expect(reads).toHaveLength(1);
  } finally {
    vi.useRealTimers();
  }
});

it.each(["resolving", "reading"])("keeps a background /showme %s retry tied to a confirmed file", async (stage) => {
  const fallback = vi.mocked(fetch).getMockImplementation()!;
  const candidate = { ...markdown("plan.md"), source: "/repo", absolute_path: "/repo/plan.md" };
  const stalled = deferred<Response>();
  const readsOfPlan: RequestInit[] = [];
  const confirmedRetry: URLSearchParams[] = [];
  vi.mocked(fetch).mockImplementation((input, init) => {
    const url = new URL(String(input), window.location.origin);
    if (url.pathname.endsWith("/sources")) return Promise.resolve(Response.json({ cwd: "/tmp", roots: ["/tmp"] }));
    if (url.pathname.endsWith("/browse")) return Promise.resolve(Response.json({
      entries: [{ path: "other.md", name: "other.md", kind: "file" }], skipped: false, truncated: false,
    }));
    if (url.pathname.endsWith("/files/meta") && url.searchParams.get("path") === candidate.absolute_path) {
      confirmedRetry.push(url.searchParams);
      return Promise.resolve(Response.json(candidate));
    }
    if (url.pathname.endsWith("/files") && url.searchParams.get("path") === "plan.md") {
      readsOfPlan.push(init!);
      return readsOfPlan.length === 1 ? stalled.promise : Promise.resolve(new Response("# Plan\n\nConfirmed line"));
    }
    return fallback(input, init);
  });
  await mount();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  try {
    await command("/showme the original report");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file picker" })));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "other.md" })));
    expect(screen.getByRole("tab", { name: "other.md", selected: true })).toBeVisible();

    if (stage === "reading") {
      await act(async () => reads[0].resolve(Response.json({
        description: "the original report", path: candidate.absolute_path, metadata: candidate,
        target: { path: candidate.absolute_path, position: { kind: "lines", start: 3 } },
      })));
    }
    await act(async () => vi.advanceTimersByTime(5000));
    if (stage === "resolving") {
      expect(screen.getByText("the original report is taking longer to load")).toBeVisible();
      expect(screen.queryByRole("button", { name: /Retry opening/ })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Cancel opening the original report" })).toBeVisible();
    } else {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening plan.md" })));
      expect(confirmedRetry).toHaveLength(1);
      expect(confirmedRetry[0].get("source")).toBe(candidate.source);
      expect(readsOfPlan[0].signal?.aborted).toBe(true);
      await act(async () => stalled.resolve(new Response("# Retired plan")));
      expect(screen.getByText("Confirmed line")).toHaveAttribute("data-located", "true");
      expect(screen.queryByText("Retired plan")).not.toBeInTheDocument();
    }
    expect(reads).toHaveLength(1);
  } finally {
    vi.useRealTimers();
  }
});

it.each([
  { scenario: "slow read after its origin closes", slowRetry: true },
  { scenario: "failed read after its origin closes" },
  { scenario: "failed read after its alias is replaced", replaceAlias: true },
  { scenario: "failed read after its alias is replaced and the alias confirms first", replaceAlias: true, aliasConfirmsFirst: true },
  { scenario: "failed read when its alias confirms first and its origin closes", aliasConfirmsFirst: true },
  { scenario: "failed read retried from its link origin", retryFrom: "origin" },
  { scenario: "failed read retried from recovery", retryFrom: "recovery" },
  { scenario: "alias-first read retried from its link origin", aliasConfirmsFirst: true, retryFrom: "origin" },
  { scenario: "alias-first read retried from recovery", aliasConfirmsFirst: true, retryFrom: "recovery" },
  { scenario: "alias-first read retried after the failed link origin closes", aliasConfirmsFirst: true, retryFrom: "after-close" },
  { scenario: "failed read retried through a repointed link", retryFrom: "repointed-origin" },
  { scenario: "failed read retried again after its link origin closes", aliasConfirmsFirst: true, retryFrom: "after-denial" },
  { scenario: "parallel retry succeeds before an older denial", retryFrom: "parallel-success" },
  { scenario: "parallel retry succeeds before an older denial with alias first", aliasConfirmsFirst: true, retryFrom: "parallel-success" },
  { scenario: "late same-file open cannot reopen the closed result", retryFrom: "parallel-closed" },
  { scenario: "parallel retry keeps a delayed independent redirect", aliasConfirmsFirst: true, retryFrom: "parallel-repoint" },
  { scenario: "parallel retry denial survives an older success", retryFrom: "parallel-denial" },
  { scenario: "parallel retry denial survives an older success with alias first", aliasConfirmsFirst: true, retryFrom: "parallel-denial" },
  { scenario: "older authorization succeeds after the newer denial", retryFrom: "parallel-denial-first" },
  { scenario: "older authorization succeeds after the newer denial with alias first", aliasConfirmsFirst: true, retryFrom: "parallel-denial-first" },
  { scenario: "older confirmed file fails after the redirect is ready", retryFrom: "parallel-distinct-ready" },
  { scenario: "older confirmed file fails after the redirect is denied", retryFrom: "parallel-distinct-denied" },
  { scenario: "repointed body denial falls back to the original file", aliasConfirmsFirst: true, retryFrom: "repointed-denial" },
  { scenario: "repointed read fails after its origin closes", retryFrom: "repointed-closed" },
  { scenario: "alias-first repointed read fails after its origin closes", aliasConfirmsFirst: true, retryFrom: "repointed-closed" },
  { scenario: "denied read edited at the parsed path", aliasConfirmsFirst: true, editPosition: true },
  { scenario: "description retried and reloaded after its alias confirms first", aliasConfirmsFirst: true, descriptionSearch: true },
])("recovers a shared command read for a $scenario", async ({
  slowRetry, replaceAlias, aliasConfirmsFirst, retryFrom, editPosition, descriptionSearch,
}) => {
  const fallback = vi.mocked(fetch).getMockImplementation()!;
  const candidate = markdown("initial.md");
  const stalled = deferred<Response>();
  const repointedBody = deferred<Response>();
  const retryRequests: URLSearchParams[] = [];
  let fileReads = 0;
  let aliasReplaced = false;
  let denyAlias = false;
  const retryMetadata: Promise<Response>[] = [];
  const retryBodies: Promise<Response>[] = [];
  vi.mocked(fetch).mockImplementation((input, init) => {
    const url = new URL(String(input), window.location.origin);
    if (url.pathname.endsWith("/sources")) return Promise.resolve(Response.json({ cwd: "/tmp", roots: ["/tmp"] }));
    if (url.pathname.endsWith("/browse")) return Promise.resolve(Response.json({
      entries: [{ path: "other.md", name: "other.md", kind: "file" }], skipped: false, truncated: false,
    }));
    const path = url.searchParams.get("path");
    if (url.pathname.endsWith("/files/meta") && path === "alias.md") {
      if (retryMetadata.length) return retryMetadata.shift()!;
      if (denyAlias) return Promise.resolve(new Response(null, { status: 403 }));
      return Promise.resolve(Response.json(aliasReplaced ? markdown("replacement.md") : candidate));
    }
    if (url.pathname.endsWith("/files/meta") && path === candidate.absolute_path) {
      retryRequests.push(url.searchParams);
      return Promise.resolve(Response.json(candidate));
    }
    if (url.pathname.endsWith("/files") && path === "other.md") {
      return Promise.resolve(new Response("# Other\n\n[Read alias at seven](alias.md#L7)"));
    }
    if (url.pathname.endsWith("/files") && path === candidate.path) {
      const body = "# Plan\n\nOriginal line\n\nMiddle\n\nAlias line";
      return ++fileReads === 1 ? stalled.promise : retryBodies.shift() ?? Promise.resolve(new Response(body));
    }
    if (url.pathname.endsWith("/files") && path === "replacement.md" &&
      (retryFrom === "repointed-denial" || retryFrom === "parallel-distinct-denied")) {
      return Promise.resolve(new Response(null, { status: 403 }));
    }
    if (url.pathname.endsWith("/files") && path === "replacement.md" && retryFrom === "repointed-closed") return repointedBody.promise;
    return fallback(input, init);
  });
  await mount();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  try {
    const description = "the original report";
    await command(descriptionSearch ? `/showme ${description}` : "/showme initial.md#L3");
    const confirmInitial = () => act(async () => reads[0].resolve(Response.json({ metadata: candidate,
      description: descriptionSearch ? description : undefined,
      target: { path: candidate.path, position: descriptionSearch ? undefined : { kind: "lines", start: 3 } },
    })));
    if (!aliasConfirmsFirst) await confirmInitial();
    expect(fileReads).toBe(aliasConfirmsFirst ? 0 : 1);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file picker" })));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "other.md" })));
    const aliasLink = screen.getByRole("button", { name: "Open file: Read alias at seven at line 7" });
    await act(async () => fireEvent.click(aliasLink));
    expect(fileReads).toBe(1);
    if (aliasConfirmsFirst) await confirmInitial();
    expect(fileReads).toBe(1);
    if (replaceAlias) {
      aliasReplaced = true;
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Read alias at seven at line 7" })));
      expect(screen.getByRole("tab", { name: "replacement.md", selected: true })).toBeVisible();
    } else if (!retryFrom) {
      fireEvent.click(screen.getByRole("button", { name: "Close other.md" }));
    }
    if (retryFrom) {
      aliasLink.focus();
      await act(async () => stalled.reject(new TypeError("Interrupted body")));
      expect(screen.getByRole("tab", { name: "other.md", selected: true })).toBeVisible();
      expect(aliasLink).toHaveFocus();
      expect(screen.getByRole("heading", { name: "Other" })).toBeVisible();
      expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open alias.md. Couldn't load the file. Check your connection and retry.");
      expect(screen.getByRole("button", { name: "Retry opening alias.md" })).toBeVisible();
      if (retryFrom === "repointed-closed") {
        aliasReplaced = true;
        fireEvent.click(screen.getByRole("tab", { name: "initial.md" }));
        await act(async () => fireEvent.click(screen.getByRole("button", { name: /^Retry$/ })));
        expect(screen.getByRole("tab", { name: "replacement.md", selected: true })).toBeVisible();
        fireEvent.click(screen.getByRole("button", { name: "Close other.md" }));
        await act(async () => repointedBody.resolve(new Response(null, { status: 403 })));
        expect(screen.getByRole("tab", { name: "initial.md", selected: true })).toHaveAttribute("title", candidate.absolute_path);
        expect(screen.getByRole("alert")).toHaveTextContent("Check your connection and retry.");
        fireEvent.click(screen.getByRole("button", { name: "Edit path" }));
        expect(screen.getByRole("textbox", { name: "File path" })).toHaveValue(candidate.absolute_path);
        expect(screen.getByRole("textbox", { name: "Position (optional)" })).toHaveValue(":3");
        fireEvent.click(screen.getByRole("button", { name: /^Cancel$/ }));
        await act(async () => fireEvent.click(screen.getByRole("button", { name: /^Retry$/ })));
        expect(screen.getByText("Original line")).toHaveAttribute("data-located", "true");
        expect(retryRequests).toHaveLength(1);
        expect(retryRequests[0].get("source")).toBe(candidate.source);
        return;
      }
      const deniesFirst = retryFrom === "parallel-denial-first";
      if (retryFrom.startsWith("parallel-")) {
        const older = deferred<Response>();
        const newer = deferred<Response>();
        retryMetadata.push(older.promise, newer.promise);
        fireEvent.click(screen.getByRole("tab", { name: "initial.md" }));
        await act(async () => fireEvent.click(screen.getByRole("button", { name: /^Retry$/ })));
        fireEvent.click(screen.getByRole("tab", { name: "other.md" }));
        await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening alias.md" })));
        expect(retryMetadata).toHaveLength(0);
        if (retryFrom.startsWith("parallel-distinct-")) {
          const oldBody = deferred<Response>();
          retryBodies.push(oldBody.promise);
          await act(async () => older.resolve(Response.json(candidate)));
          expect(fileReads).toBe(2);
          await act(async () => newer.resolve(Response.json(markdown("replacement.md"))));
          const denied = retryFrom === "parallel-distinct-denied";
          expect(screen.getByRole("tab", { name: denied ? "other.md" : "replacement.md", selected: true })).toBeVisible();
          await act(async () => oldBody.reject(new TypeError("Original body was interrupted")));
          fireEvent.click(screen.getByRole("tab", { name: "other.md" }));
          const alerts = screen.getAllByRole("alert");
          expect(alerts).toHaveLength(denied ? 2 : 1);
          expect(alerts.some((alert) => alert.textContent?.includes("Check your connection and retry."))).toBe(true);
          return;
        }
        if (retryFrom === "parallel-closed") {
          await act(async () => newer.resolve(Response.json(candidate)));
          fireEvent.click(screen.getByRole("button", { name: "Close initial.md" }));
          await act(async () => older.resolve(Response.json(candidate)));
          expect(screen.getAllByRole("tab")).toHaveLength(1);
          expect(screen.getByRole("tab", { name: "other.md", selected: true })).toBeVisible();
          expect(fileReads).toBe(2);
          return;
        }
        if (retryFrom !== "parallel-denial" && !deniesFirst) {
          await act(async () => newer.resolve(Response.json(candidate)));
          await act(async () => older.resolve(retryFrom === "parallel-repoint" ? Response.json(markdown("replacement.md"))
            : new Response(null, { status: 403 })));
          expect(screen.getAllByRole("tab")).toHaveLength(retryFrom === "parallel-repoint" ? 3 : 2);
          expect(screen.getAllByRole("tab", { name: "initial.md" })).toHaveLength(1);
          expect(screen.getByText("Alias line")).toHaveAttribute("data-located", "true");
          if (retryFrom === "parallel-repoint") {
            fireEvent.click(screen.getByRole("tab", { name: "replacement.md", selected: false }));
            expect(screen.getByText("# Opened document")).toBeVisible();
          }
        } else {
          const oldBody = deferred<Response>();
          retryBodies.push(oldBody.promise);
          if (deniesFirst) await act(async () => newer.resolve(new Response(null, { status: 403 })));
          await act(async () => older.resolve(Response.json(candidate)));
          if (!deniesFirst) await act(async () => newer.resolve(new Response(null, { status: 403 })));
          expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
          expect(fileReads).toBe(2);
          await act(async () => oldBody.resolve(new Response("# Readable older snapshot")));
          expect(screen.getByRole("tab", { name: "other.md", selected: true })).toBeVisible();
          expect(screen.getByRole("tab", { name: "initial.md", selected: false })).toBeVisible();
          expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
          expect(screen.getByRole("button", { name: "Retry opening alias.md" })).toBeVisible();
        }
        return;
      }
      if (retryFrom === "origin" || retryFrom === "repointed-origin") {
        aliasReplaced = retryFrom === "repointed-origin";
        await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening alias.md" })));
        if (aliasReplaced) {
          expect(screen.getByRole("tab", { name: "replacement.md", selected: true })).toBeVisible();
          fireEvent.click(screen.getByRole("tab", { name: "initial.md" }));
          expect(screen.getByRole("alert")).toHaveTextContent("Check your connection and retry.");
          await act(async () => fireEvent.click(screen.getByRole("button", { name: /^Retry$/ })));
        }
      } else {
        if (retryFrom === "after-close") fireEvent.click(screen.getByRole("button", { name: "Close other.md" }));
        fireEvent.click(screen.getByRole("tab", { name: "initial.md" }));
        if (retryFrom === "after-denial" || retryFrom === "repointed-denial") {
          denyAlias = retryFrom === "after-denial";
          aliasReplaced = retryFrom === "repointed-denial";
          await act(async () => fireEvent.click(screen.getByRole("button", { name: /^Retry$/ })));
          expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
          fireEvent.click(screen.getByRole("button", { name: "Close other.md" }));
          if (retryFrom === "repointed-denial") {
            expect(screen.getByRole("alert")).toHaveTextContent("Check your connection and retry.");
          }
        }
        await act(async () => fireEvent.click(screen.getByRole("button", {
          name: retryFrom === "after-denial" ? "Reload file" : /^Retry$/,
        })));
      }
      expect(screen.getAllByRole("tab", { name: "initial.md" })).toHaveLength(1);
      expect(screen.getByRole("tab", { name: "initial.md", selected: true })).toBeVisible();
      const keepsOrigin = retryFrom !== "after-close" && retryFrom !== "after-denial" && retryFrom !== "repointed-denial";
      const keepsAlias = keepsOrigin && retryFrom !== "repointed-origin";
      expect(screen.getByText(keepsAlias ? "Alias line" : "Original line")).toHaveAttribute("data-located", "true");
      expect(fileReads).toBe(2);
      expect(reads).toHaveLength(1);
      if (keepsOrigin) {
        fireEvent.click(screen.getByRole("tab", { name: "other.md" }));
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
        expect(screen.queryByRole("button", { name: "Retry opening alias.md" })).not.toBeInTheDocument();
      }
      return;
    }
    if (slowRetry) {
      await act(async () => vi.advanceTimersByTime(5000));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening initial.md" })));
    } else if (editPosition) {
      await act(async () => stalled.resolve(new Response(null, { status: 403 })));
      expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
      fireEvent.click(screen.getByRole("button", { name: "Edit path" }));
      expect(screen.getByRole("textbox", { name: "File path" })).toHaveValue(candidate.path);
      fireEvent.change(screen.getByRole("textbox", { name: "Position (optional)" }), { target: { value: "#L7" } });
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file" })));
      expect(screen.getByText("Alias line")).toHaveAttribute("data-located", "true");
      const editRead = vi.mocked(fetch).mock.calls.map(([url]) => new URL(String(url), window.location.origin))
        .find((url) => url.pathname.endsWith("/files/meta") && url.searchParams.get("path") === candidate.path);
      expect(editRead?.searchParams.get("source")).toBe(candidate.source);
      expect(reads).toHaveLength(1);
      return;
    } else {
      await act(async () => stalled.reject(new TypeError("Interrupted body")));
      if (replaceAlias) {
        expect(screen.getByRole("tab", { name: "replacement.md", selected: true })).toBeVisible();
        fireEvent.click(screen.getByRole("tab", { name: "other.md" }));
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
        fireEvent.click(screen.getByRole("tab", { name: "initial.md" }));
      }
      expect(screen.getByRole("tab", { name: "initial.md", selected: true })).toBeVisible();
      expect(screen.getByRole("alert")).toHaveTextContent("Check your connection and retry.");
      if (descriptionSearch) expect(screen.getByRole("textbox", { name: "File description" })).toHaveValue(description);
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry" })));
    }
    expect(reads).toHaveLength(1);
    expect(retryRequests).toHaveLength(1);
    expect(retryRequests[0].get("source")).toBe(candidate.source);
    if (descriptionSearch) {
      expect(screen.getByRole("heading", { name: "Plan" })).toBeVisible();
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Reload file" })));
      expect(fileReads).toBe(3);
      expect(reads).toHaveLength(1);
      expect(retryRequests.map((query) => query.get("source"))).toEqual([candidate.source, candidate.source]);
      return;
    }
    expect(screen.getByText("Original line")).toHaveAttribute("data-located", "true");
    expect(screen.getByText("Alias line")).not.toHaveAttribute("data-located");
  } finally {
    vi.useRealTimers();
  }
});

it("retries candidate bytes from the confirmed source without repeating inference", async () => {
  const fallback = vi.mocked(fetch).getMockImplementation()!;
  const candidate = { ...markdown("plan.md"), source: "/repo", absolute_path: "/repo/plan.md" };
  const bodyRequests: URLSearchParams[] = [];
  vi.mocked(fetch).mockImplementation((input, init) => {
    const url = new URL(String(input), window.location.origin);
    if (url.pathname.endsWith("/files/meta")) {
      expect(url.searchParams.get("source")).toBe(candidate.source);
      expect(url.searchParams.get("path")).toBe(candidate.absolute_path);
      return Promise.resolve(Response.json(candidate));
    }
    if (url.pathname.endsWith("/files")) {
      bodyRequests.push(url.searchParams);
      if (bodyRequests.length === 1) return Promise.reject(new TypeError("Temporary connection loss"));
    }
    return fallback(input, init);
  });
  await mount();
  await command("/showme the plan from v1.2");
  await act(async () => reads[0].resolve(Response.json({ description: "the plan from v1.2", path: candidate.absolute_path, metadata: candidate })));
  expect(await screen.findByRole("alert")).toHaveTextContent(/connection and retry/i);
  expect(screen.getByRole("textbox", { name: "File description" })).toHaveValue("the plan from v1.2");
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await screen.findByRole("heading", { name: "Opened document" });
  expect(reads).toHaveLength(1);
  expect(bodyRequests.map(query => query.get("source"))).toEqual(["/repo", "/repo"]);
});

it("offers another source when the original working directory is unavailable", async () => {
  await mount();
  await command("/showme the original report");
  await act(async () => reads[0].resolve(Response.json({ description: "the original report",
    code: "working_directory_unavailable", detail: "The session working directory is no longer available." }, { status: 403 })));
  expect(screen.getByRole("alert")).toHaveTextContent("The session working directory is no longer available.");
  expect(screen.getByRole("textbox", { name: "File description" })).toHaveValue("the original report");
  fireEvent.click(screen.getByRole("button", { name: "Choose a file" }));
  expect(screen.getByRole("heading", { name: "Choose a file" })).toBeVisible();
  expect(reads).toHaveLength(1);
});

it("keeps picker responses bound to the current source, directory, search and viewer", async () => {
  const fallback = vi.mocked(fetch).getMockImplementation()!;
  const lists: { params: URLSearchParams; signal: AbortSignal; reply: ReturnType<typeof deferred<Response>> }[] = [];
  vi.mocked(fetch).mockImplementation((input, init) => {
    const url = new URL(String(input), window.location.origin);
    if (url.pathname.endsWith("/sources")) return Promise.resolve(Response.json({ cwd: "/tmp", roots: ["/repo", "/tmp"] }));
    if (url.pathname.endsWith("/browse") || url.pathname.endsWith("/find")) {
      const reply = deferred<Response>();
      lists.push({ params: url.searchParams, signal: init!.signal!, reply });
      return reply.promise;
    }
    return fallback(input, init);
  });
  const entry = (path: string, kind = "file") => ({ path, name: path.split("/").pop(), kind });
  const deliver = (index: number, entries: ReturnType<typeof entry>[]) => act(async () => {
    lists[index].reply.resolve(Response.json({ entries, skipped: false, truncated: false }));
  });

  await mount();
  await command("/showme");
  const source = await screen.findByRole("combobox", { name: "Source" });
  expect(source).toHaveValue("/tmp");
  expect(lists[0].params.get("source")).toBe("/tmp");
  fireEvent.change(source, { target: { value: "/repo" } });
  expect(lists[0].signal.aborted).toBe(true);
  await deliver(1, [entry("nested", "directory")]);
  await deliver(0, [entry("late source.md")]);
  expect(screen.queryByRole("button", { name: /late source/ })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "nested" }));
  expect(lists[2].params.get("directory")).toBe("nested");
  const search = screen.getByRole("searchbox", { name: "Search filenames" });
  fireEvent.change(search, { target: { value: "first" } });
  expect(lists[2].signal.aborted).toBe(true);
  let lateBody!: ReadableStreamDefaultController<Uint8Array>;
  await act(async () => lists[3].reply.resolve(new Response(new ReadableStream({ start(controller) { lateBody = controller; } }))));
  fireEvent.change(search, { target: { value: "second" } });
  expect(lists[3].signal.aborted).toBe(true);
  await deliver(4, [entry("latest.md")]);
  await act(async () => {
    lateBody.enqueue(new TextEncoder().encode(JSON.stringify({ entries: [entry("late keyword.md")] })));
    lateBody.close();
  });
  await deliver(2, [entry("nested/late directory.md")]);
  expect(screen.getByRole("button", { name: /latest.md/ })).toBeVisible();
  expect(screen.queryByRole("button", { name: /late keyword|late directory/ })).toBeNull();

  fireEvent.change(search, { target: { value: "third" } });
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(lists[5].signal.aborted).toBe(true);
  await deliver(5, [entry("late close.md")]);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(reads).toHaveLength(0);
  expect(Object.values(useSessionStore.getState().messages).flat()).toEqual([]);
});

it.each(["success", "failure"])("retires a viewing intent permanently through a same-batch session round trip (%s)", async outcome => {
  await mount();
  await command("/showme first.md");
  act(() => { useSessionStore.getState().setActiveSessionId(b.id); useSessionStore.getState().setActiveSessionId(a.id); });
  await act(async () => reads[0].resolve(outcome === "success" ? Response.json({ path: "/obsolete/report.md" }) : Response.json({ detail: "Obsolete view failure" }, { status: 400 })));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining("/files/meta"), expect.anything());
  expect(useSessionStore.getState().messages[a.id] ?? []).toEqual([]);
});

it("does not restore the opening focus after closing and leaving the conversation", async () => {
  await mount();
  const input = screen.getByRole("textbox");
  input.focus();
  await command("/showme first.md");
  act(() => {
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    useSessionStore.getState().setActiveSessionId(b.id);
    useSessionStore.getState().setActiveSessionId(a.id);
  });
  await act(async () => new Promise<void>((resolve) => setTimeout(resolve, 0)));
  expect(input).not.toHaveFocus();
});

it.each(["selection", "page", "page round trip", "close viewer", "explicit old file", "account round trip"].flatMap(
  transition => ["success", "failure"].map(outcome => ({ transition, outcome })),
))("retires viewing on $transition without adopting late $outcome", async ({ transition, outcome }) => {
  await mount();
  await command("/showme the first report");
  if (transition === "close viewer") fireEvent.click(screen.getByRole("button", { name: "Close" }));
  act(() => {
    if (transition === "selection") useSessionStore.getState().setActiveSessionId(b.id);
    if (transition.startsWith("page")) {
      useSessionStore.getState().openManage("schedules");
      if (transition === "page round trip") useSessionStore.getState().showChat();
    }
    if (transition === "explicit old file") useSessionStore.getState().openViewer(b.id, "older.md");
    if (transition === "account round trip") {
      useSessionStore.getState().setToken("another-account");
      useSessionStore.getState().setToken("view-token");
      useSessionStore.setState({ mainView: "chat", activeSessionId: a.id, sessions: [a, b] });
    }
  });
  await act(async () => {
    if (outcome === "success") reads[0].resolve(Response.json({ description: "the first report",
      path: "/obsolete/report.md", metadata: markdown("obsolete.md") }));
    else reads[0].reject(new TypeError("Retired network failure"));
  });
  if (transition === "explicit old file") {
    await screen.findByRole("dialog", { name: "older.md" });
    await screen.findByRole("heading", { name: "Opened document" });
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining(`/${b.id}/files?`), expect.anything());
  } else {
    expect(screen.queryByRole("dialog")).toBeNull();
  }
  expect(Object.values(useSessionStore.getState().messages).flat()).toEqual([]);
});

it.each(["success", "failure"])("keeps a newer viewing intent through an old %s and cleanup", async outcome => {
  await mount();
  await command("/showme first.md");
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  await command("/showme second.md");
  await act(async () => reads[0].resolve(outcome === "success" ? Response.json({ path: "/old.md" }) : Response.json({ detail: "Old failure" }, { status: 409 })));
  expect(screen.getByRole("status")).toHaveTextContent("Loading");
  await act(async () => reads[1].resolve(Response.json({ path: "new.md", metadata: markdown("new.md") })));
  await screen.findByRole("dialog", { name: "new.md" });
  expect(useSessionStore.getState().messages[a.id] ?? []).toEqual([]);
});

it("keeps the newer viewer when its response finishes before the old viewing", async () => {
  await mount();
  await command("/showme first.md");
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  await command("/showme second.md");
  await act(async () => reads[1].resolve(Response.json({ path: "new.md", metadata: markdown("new.md") })));
  await act(async () => reads[0].resolve(Response.json({ path: "/old.md" })));
  expect(screen.getByRole("dialog", { name: "new.md" })).toBeVisible();
});

it.each([200, 400])("ignores a body delivered after account retirement (HTTP %s)", async status => {
  await mount();
  await command("/showme report");
  let body!: ReadableStreamDefaultController<Uint8Array>;
  await act(async () => reads[0].resolve(new Response(new ReadableStream<Uint8Array>({ start(controller) { body = controller; } }), { status })));
  act(() => {
    useSessionStore.getState().setToken("another-account");
    useSessionStore.setState({ activeSessionId: a.id, sessions: [a, b] });
  });
  await act(async () => { body.enqueue(new TextEncoder().encode(JSON.stringify(status === 200
    ? { description: "report", path: "/old.md", metadata: markdown("old.md") }
    : { description: "report", detail: "Old error body" }))); body.close(); });
  expect(useSessionStore.getState().viewer).toBeNull();
  expect(useSessionStore.getState().messages[a.id] ?? []).toEqual([]);
});

it.each([
  [404, "File not found.", "Edit path"],
  [403, "You don't have access to this file.", "Edit path"],
  [409, "The file path changed.", "Retry"],
  [413, "This file exceeds the preview limit.", "Edit path"],
  [415, "This file format can't be previewed.", "Edit path"],
  ["network", "Check your connection and retry.", "Retry"],
  [200, "No matching report", "Edit path"],
] as const)("shows the current %s failure in the viewer", async (status, message, action) => {
  await mount();
  await command("/showme the report");
  expect(screen.getByRole("status")).toHaveTextContent("Loading");
  await act(async () => {
    if (status === "network") reads[0].reject(new TypeError("offline"));
    else reads[0].resolve(Response.json(status === 200 ? { message: "No matching report" } :
      status === 403 ? { detail: "/undisclosed/internal.md" } : {}, { status }));
  });
  expect(screen.getByRole("alert")).toHaveTextContent(message);
  expect(screen.getByRole("button", { name: action })).toBeVisible();
  expect(screen.queryByText("/undisclosed/internal.md")).toBeNull();
  expect(useSessionStore.getState().messages[a.id] ?? []).toEqual([]);
});

it("routes the old /model syntax to settings without sending a write", async () => {
  await mount();
  await command("/model next-model");
  expect(screen.getByRole("dialog", { name: "Conversation settings" })).toBeVisible();
  expect(reads).toHaveLength(0);
  expect(useSessionStore.getState().sessions[0].model).toBe(a.model);
});

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
function confirmedFile(path: string, metadata = markdown(path)) {
  return { metadata, target: { path } };
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
  await act(async () => reads[0].resolve(Response.json(confirmedFile("report.md"))));
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
  await act(async () => reads[1].resolve(Response.json({ description: "请打开 plan.md", message: "Which plan?" })));
  expect(screen.getByText("Which plan?")).toBeVisible();
  const field = screen.getByRole("textbox", { name: "File description" });
  expect(field).toHaveValue("请打开 plan.md");
  fireEvent.change(field, { target: { value: "the implementation plan.md" } });
  fireEvent.click(screen.getByRole("button", { name: "Search conversation" }));
  await screen.findByText("Searching the conversation…");
  await act(async () => reads[2].resolve(Response.json({ ...confirmedFile("plan.md"), description: "the implementation plan.md" })));
  await screen.findByRole("heading", { name: "Opened document" });
  const resolutionBodies = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/showme/resolve"))
    .map(([, init]) => JSON.parse(String(init?.body)));
  expect(resolutionBodies).toEqual([
    { text: "请打开 plan.md" },
    { text: "请打开 plan.md", mode: "description" },
    { text: "the implementation plan.md", mode: "description" },
  ]);
  const fileReads = vi.mocked(fetch).mock.calls.map(([url]) => new URL(String(url), window.location.origin).pathname);
  expect(fileReads.filter((url) => url.includes("/files"))).toEqual([`/api/sessions/${a.id}/files`]);
  expect(Object.values(useSessionStore.getState().messages).flat()).toEqual([]);
});

it.each([
  ["plan.md", "wait"], ["plan.md", "cancel"],
  ["the original report", "wait"], ["the original report", "cancel"],
] as const)("keeps one lookup for slow unresolved /showme %s when the reader chooses to %s", async (input, choice) => {
  await mount(true);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  try {
    if (input === "plan.md") await command(`/showme ${input}`);
    else await act(async () => useSessionStore.getState().openViewer(a.id, input, { mode: "description" }));
    expect(reads).toHaveLength(1);
    await act(async () => vi.advanceTimersByTime(5000));
    expect(screen.getByText("The file has not finished loading.")).toBeVisible();
    expect(screen.queryByRole("button", { name: /^Retry$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reload file" })).not.toBeInTheDocument();
    const close = screen.getByRole("button", { name: "Close" });
    expect(close).toBeVisible();
    if (choice === "cancel") fireEvent.click(close);

    await act(async () => reads[0].resolve(Response.json({
      ...confirmedFile("plan.md"), ...(input === "plan.md" ? {} : { description: input }),
    })));
    if (choice === "wait") {
      expect(screen.getByRole("heading", { name: "Opened document" })).toBeVisible();
      expect(screen.getByRole("button", { name: "Reload file" })).toBeVisible();
    } else {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    }
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
  const bodyBindings: URLSearchParams[] = [];
  vi.mocked(fetch).mockImplementation((input, init) => {
    const url = new URL(String(input), window.location.origin);
    if (url.pathname.endsWith("/sources")) return Promise.resolve(Response.json({ cwd: "/tmp", roots: ["/tmp"] }));
    if (url.pathname.endsWith("/browse")) return Promise.resolve(Response.json({
      entries: [{ path: "other.md", name: "other.md", kind: "file" }], skipped: false, truncated: false,
    }));
    if (url.pathname.endsWith("/files") && url.searchParams.get("path") === "plan.md") {
      readsOfPlan.push(init!);
      bodyBindings.push(url.searchParams);
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
        description: "the original report", metadata: candidate,
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
      expect(bodyBindings.map((binding) => binding.get("source"))).toEqual([candidate.source, candidate.source]);
      expect(bodyBindings.map((binding) => binding.get("canonical_path_id"))).toEqual(["plan.md", "plan.md"]);
      expect(readsOfPlan[0].signal?.aborted).toBe(true);
      await act(async () => stalled.resolve(new Response("# Retired plan")));
      expect(screen.getByRole("tab", { name: "other.md", selected: true })).toBeVisible();
      fireEvent.click(screen.getByRole("tab", { name: "plan.md" }));
      expect(screen.getByText("Confirmed line")).toBeVisible();
      expect(screen.getByText("Confirmed line")).not.toHaveAttribute("data-located");
      expect(screen.queryByText("Retired plan")).not.toBeInTheDocument();
    }
    expect(reads).toHaveLength(1);
  } finally {
    vi.useRealTimers();
  }
});

it.each(["command", "description"] as const)("retries and reloads a %s result without repeating inference", async (mode) => {
  const fallback = vi.mocked(fetch).getMockImplementation()!;
  const candidate = { ...markdown("plan.md"), source: "/repo", absolute_path: "/repo/plan.md" };
  const bodyRequests: URLSearchParams[] = [];
  const metadataRequests: URLSearchParams[] = [];
  vi.mocked(fetch).mockImplementation((input, init) => {
    const url = new URL(String(input), window.location.origin);
    if (url.pathname.endsWith("/files/meta")) {
      metadataRequests.push(url.searchParams);
      return Promise.resolve(Response.json({ ...candidate, canonical_path_id: "reloaded-binding" }));
    }
    if (url.pathname.endsWith("/files")) {
      bodyRequests.push(url.searchParams);
      if (bodyRequests.length === 1) return Promise.reject(new TypeError("Temporary connection loss"));
      if (bodyRequests.length === 3) return Promise.resolve(new Response("# Reloaded description result"));
    }
    return fallback(input, init);
  });
  await mount();
  const description = "the plan from v1.2";
  if (mode === "command") await command(`/showme ${description}`);
  else await act(async () => useSessionStore.getState().openViewer(a.id, description, { mode }));
  await act(async () => reads[0].resolve(Response.json({
    ...confirmedFile(candidate.absolute_path, candidate), description: "the plan from v1.2",
  })));
  expect(await screen.findByRole("alert")).toHaveTextContent(/connection and retry/i);
  expect(screen.getByRole("textbox", { name: "File description" })).toHaveValue("the plan from v1.2");
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await screen.findByRole("heading", { name: "Opened document" });
  expect(reads).toHaveLength(1);
  expect(metadataRequests).toEqual([]);
  expect(bodyRequests.map(query => query.get("source"))).toEqual(["/repo", "/repo"]);
  expect(bodyRequests.map(query => query.get("canonical_path_id"))).toEqual(["plan.md", "plan.md"]);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Reload file" })));
  expect(reads).toHaveLength(1);
  expect(metadataRequests).toHaveLength(1);
  expect(metadataRequests[0].get("path")).toBe(candidate.absolute_path);
  expect(metadataRequests[0].get("source")).toBe(candidate.source);
  expect(bodyRequests[2].get("canonical_path_id")).toBe("reloaded-binding");
  expect(screen.getByRole("heading", { name: "Reloaded description result" })).toBeVisible();
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
  await act(async () => reads[0].resolve(outcome === "success" ? Response.json(confirmedFile("obsolete.md")) : Response.json({ detail: "Obsolete view failure" }, { status: 400 })));
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
    if (outcome === "success") reads[0].resolve(Response.json({ ...confirmedFile("obsolete.md"), description: "the first report" }));
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
  await act(async () => reads[0].resolve(outcome === "success" ? Response.json(confirmedFile("old.md")) : Response.json({ detail: "Old failure" }, { status: 409 })));
  expect(screen.getByRole("status")).toHaveTextContent("Loading");
  await act(async () => reads[1].resolve(Response.json(confirmedFile("new.md"))));
  await screen.findByRole("dialog", { name: "new.md" });
  expect(useSessionStore.getState().messages[a.id] ?? []).toEqual([]);
});

it("keeps the newer viewer when its response finishes before the old viewing", async () => {
  await mount();
  await command("/showme first.md");
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  await command("/showme second.md");
  await act(async () => reads[1].resolve(Response.json(confirmedFile("new.md"))));
  await act(async () => reads[0].resolve(Response.json(confirmedFile("old.md"))));
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
    ? { ...confirmedFile("old.md"), description: "report" }
    : { description: "report", detail: "Old error body" }))); body.close(); });
  expect(useSessionStore.getState().viewer).toBeNull();
  expect(useSessionStore.getState().messages[a.id] ?? []).toEqual([]);
});

it.each([
  [404, "File not found.", "Edit path"],
  [403, "You don't have access to this file.", "Edit path"],
  [409, "The target file changed.", "Retry"],
  [413, "This file exceeds the 2 MiB limit for preview and download.", "Edit path"],
  [415, "This file format can't be previewed.", "Edit path"],
  ["network", "Check your connection and retry.", "Retry"],
] as const)("shows the current %s failure in the viewer", async (status, message, action) => {
  await mount();
  await command("/showme the report");
  expect(screen.getByRole("status")).toHaveTextContent("Loading");
  await act(async () => {
    if (status === "network") reads[0].reject(new TypeError("offline"));
    else reads[0].resolve(Response.json(status === 403 ? { detail: "/undisclosed/internal.md" } : {}, { status }));
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

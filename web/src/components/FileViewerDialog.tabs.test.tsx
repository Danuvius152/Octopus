import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";
import { FileViewerDialog } from "./FileViewerDialog";

const root = "/workspace";

const pdfRenderer = vi.hoisted(() => ({
  openPdf: vi.fn(() => ({ promise: Promise.resolve({ numPages: 1 }), destroy: async () => {} })),
  paintPdfPage: vi.fn(() => ({ promise: Promise.resolve(), cancel: () => {} })),
  viewerStyles: "",
}));
vi.mock("./pdfRenderer", () => pdfRenderer);
const renderSource = vi.hoisted(() => vi.fn());
vi.mock("./FileSourceView", async (importOriginal) => {
  const source = await importOriginal<typeof import("./FileSourceView")>();
  return {
    ...source,
    FileSourceView: function TrackedSourceView(props: ComponentProps<typeof source.FileSourceView>) {
      renderSource(props.raw);
      return <source.FileSourceView {...props} />;
    },
  };
});

function metadata(path: string, source = root, absolutePath = `${source}/${path}`, id = absolutePath,
  directory = { path: ".", path_id: source }, kind = "markdown") {
  return Response.json({
    path, absolute_path: absolutePath, source, canonical_path_id: id,
    canonical_directory: directory, kind, size: 120,
  });
}

function documentRead(documents: Record<string, string>) {
  const bodies = new Map<string, Array<Promise<Response>>>();
  const aliases = new Map<string, Promise<Response> | ((url: URL) => Response)>();
  const read = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = new URL(String(input));
    const path = url.searchParams.get("path") ?? "";
    const source = url.searchParams.get("source") ?? root;
    if (url.pathname.endsWith("/sources")) return Response.json({ cwd: root, roots: [root, `${root}/shared`] });
    if (url.pathname.endsWith("/browse")) return Response.json({
      entries: ["a.md", "picked.md"].map((name) => ({ path: name, name, kind: "file" })), skipped: false, truncated: false,
    });
    if (url.pathname.endsWith("/meta")) {
      const result = aliases.get(path);
      return typeof result === "function" ? result(url) : result ?? metadata(path, source);
    }
    return bodies.get(path)?.shift() ?? new Response(documents[path] ?? `# ${path} body`);
  });
  vi.stubGlobal("fetch", read);
  const fileRequests = (path: string) => read.mock.calls.filter(([input]) =>
    new URL(String(input)).searchParams.get("path") === path);
  return {
    fileRequests,
    meta(path: string, result: Promise<Response> | ((url: URL) => Response)) { aliases.set(path, result); },
    body(path: string) {
      const response = deferred<Response>();
      bodies.set(path, [...(bodies.get(path) ?? []), response.promise]);
      return response;
    },
  };
}

const openLink = async (name: string) => {
  const label = `Open file: ${name}`;
  fireEvent.click(await screen.findByRole("button", { name: (value) => value === label || value.startsWith(`${label} at line `) }));
};
const tab = (name: string) => screen.getByRole("tab", { name });
const selected = () => screen.getByRole("tab", { selected: true });
const closeTab = (name: string) => fireEvent.click(screen.getByRole("button", { name: `Close ${name}` }));
const cancel = (name: string) => fireEvent.click(screen.getByRole("button", { name: `Cancel opening ${name}` }));

function openReader(path = "a.md", position?: { kind: "heading"; slug: string }) {
  useSessionStore.setState({ token: "tok", viewer: null, activeSessionId: "foreground", mainView: "chat" });
  render(<FileViewerDialog />);
  act(() => useSessionStore.getState().openViewer("original", path, { position }));
}

function imageRead(documents: Record<string, string>) {
  const files = documentRead(documents);
  files.meta("diagram.png", () => metadata("diagram.png", root, undefined, undefined, undefined, "image"));
  let nextUrl = 0;
  const revoke = vi.fn();
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL = vi.fn(() => `blob:diagram-${++nextUrl}`);
    static revokeObjectURL = revoke;
  });
  return {
    ...files,
    revoke,
    imageBody() {
      const bytes = files.body("diagram.png");
      return () => act(async () => bytes.resolve(new Response(new Blob(["image"], { type: "image/png" }))));
    },
  };
}

async function startImageDecode(image: HTMLElement) {
  const decoded = deferred<void>();
  const decode = vi.fn(() => decoded.promise);
  Object.defineProperty(image, "decode", { value: decode });
  await act(async () => fireEvent.load(image));
  expect(decode).toHaveBeenCalledOnce();
  return decoded;
}

afterEach(() => {
  cleanup();
  useSessionStore.setState({ viewer: null });
  vi.unstubAllGlobals();
  pdfRenderer.openPdf.mockClear();
  pdfRenderer.paintPdfPage.mockClear();
  renderSource.mockClear();
});

describe("text file tabs", () => {
  it("expires only the current link notice and ignores a superseded failure", async () => {
    const files = documentRead({ "a.md": "# Original\n\n[Read B](b.md)\n\n[Read C](c.md)\n\n[Read D](d.md)" });
    const b = files.body("b.md");
    const c = files.body("c.md");
    const d = files.body("d.md");
    openReader();
    await screen.findByRole("heading", { name: "Original" });
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    try {
      const start = async (name: string) => {
        await act(async () => fireEvent.click(screen.getByRole("button", { name: `Open file: ${name}` })));
      };
      const fail = (response: typeof b) => act(async () => response.resolve(new Response(null, { status: 404 })));
      await start("Read B");
      await fail(b);
      expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open b.md.");
      await act(async () => vi.advanceTimersByTime(6000));
      await start("Read C");
      await start("Read D");
      await fail(d);
      await act(async () => vi.advanceTimersByTime(2500));
      expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open d.md.");
      await act(async () => vi.advanceTimersByTime(5500));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      await fail(c);
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "Original" })).toBeVisible();
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["dismiss", "expire"])("keeps a bound path draft after its notice is set to %s", async (outcome) => {
    const directory = { path: "docs", path_id: "guide-parent" };
    const files = documentRead({
      "docs/a.md": "# Original\n\n[Read missing](missing.md#L3)",
      "docs/fixed.md": "# Fixed\n\nTarget line",
    });
    files.meta("docs/a.md", () => metadata("docs/a.md", root, undefined, undefined, directory));
    files.meta("missing.md", Promise.resolve(new Response(null, { status: 404 })));
    files.meta("fixed.md", () => metadata("docs/fixed.md", root, undefined, undefined, directory));
    openReader("docs/a.md");
    const link = await screen.findByRole("button", { name: /Open file: Read missing/ });
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(link));
      const dismiss = screen.getByRole("button", { name: "Dismiss file opening notice" });
      const edit = screen.getByRole("button", { name: "Edit path for missing.md" });
      if (outcome === "expire") {
        const notice = dismiss.parentElement!;
        await act(async () => vi.advanceTimersByTime(3000));
        fireEvent.mouseEnter(notice);
        await act(async () => vi.advanceTimersByTime(8000));
        expect(screen.getByRole("alert")).toBeVisible();
        act(() => edit.focus());
        fireEvent.mouseLeave(notice);
        await act(async () => vi.advanceTimersByTime(8000));
        expect(screen.getByRole("alert")).toBeVisible();
      }
      fireEvent.click(edit);
      const field = screen.getByRole("textbox", { name: "File path" });
      const position = screen.getByRole("textbox", { name: "Position (optional)" });
      expect(field).toHaveValue("missing.md");
      expect(position).toHaveValue(":3");
      if (outcome === "dismiss") fireEvent.click(dismiss);
      else await act(async () => vi.advanceTimersByTime(5000));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      fireEvent.change(field, { target: { value: "fixed.md" } });
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file" })));
      const metadata = new URL(String(files.fileRequests("fixed.md")[0][0]));
      expect(metadata.searchParams.get("source")).toBe(root);
      expect(metadata.searchParams.get("source_directory")).toBe(directory.path);
      expect(metadata.searchParams.get("source_directory_id")).toBe(directory.path_id);
      expect(screen.getByText("Target line")).toHaveAttribute("data-located", "true");
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["opening", "reloading"])("keeps recovery available when %s leaves no readable body", async (operation) => {
    const files = documentRead({ "a.md": "# Loaded document" });
    const pending = operation === "opening" ? files.body("a.md") : undefined;
    openReader();
    if (!pending) await screen.findByRole("heading", { name: "Loaded document" });
    const failure = pending ?? files.body("a.md");
    if (!pending) fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(pending ? 2 : 4));
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    try {
      await act(async () => failure.reject(new TypeError("Network disconnected")));
      await act(async () => vi.advanceTimersByTime(16000));
      expect(screen.getByRole("alert")).toHaveTextContent("Check your connection and retry.");
      expect(screen.queryByRole("button", { name: "Dismiss file opening notice" })).not.toBeInTheDocument();
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry" })));
      expect(screen.getByRole("heading", { name: "Loaded document" })).toBeVisible();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps reading at an invalid line link and omits that location after the notice expires", async () => {
    documentRead({ "a.md": "# Original\n\n[Read outside](#L450)" });
    const writeText = vi.fn().mockResolvedValue(undefined);
    const clipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    openReader("a.md", { kind: "heading", slug: "original" });
    const heading = await screen.findByRole("heading", { name: "Original" });
    const scroller = heading.closest(".file-viewer-body")!;
    scroller.scrollTop = 100;
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    try {
      fireEvent.click(screen.getByRole("link", { name: "Read outside" }));
      expect(screen.getByRole("note")).toHaveTextContent("The requested lines are outside this file.");
      const dismiss = screen.getByRole("button", { name: "Dismiss position notice" });
      act(() => dismiss.focus());
      await act(async () => vi.advanceTimersByTime(8000));
      expect(dismiss).toBeVisible();
      act(() => tab("a.md").focus());
      await act(async () => vi.advanceTimersByTime(8000));
      expect(screen.queryByRole("note")).not.toBeInTheDocument();
      expect(heading).toBeVisible();
      expect(scroller.scrollTop).toBe(100);
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Copy /showme command" })));
      expect(writeText).toHaveBeenCalledWith('/showme "/workspace/a.md" --source "/workspace"');
    } finally {
      vi.useRealTimers();
      if (clipboard) Object.defineProperty(navigator, "clipboard", clipboard);
      else Reflect.deleteProperty(navigator, "clipboard");
    }
  });

  it("validates a reused tab's location before changing its reading mode or position", async () => {
    const files = documentRead({
      "a.md": "# Guide\n\n[Open B](b.md)\n\n[Read bad line](b.md#L99)\n\n[Read missing heading](b.md#missing)\n\n[Read existing heading](b.md#existing-document)",
      "b.md": "# Existing document\n\n[Jump to source](#L1)",
    });
    openReader();
    await openLink("Open B");
    const heading = await screen.findByRole("heading", { name: "Existing document" });
    const scroller = heading.closest(".file-viewer-body")!;
    scroller.scrollTop = 100;
    fireEvent.click(tab("a.md"));
    await openLink("Read bad line");
    expect(await screen.findByRole("note")).toHaveTextContent("The requested lines are outside this file.");
    expect(heading).toBeVisible();
    expect(scroller.scrollTop).toBe(100);
    fireEvent.click(screen.getByRole("link", { name: "Jump to source" }));
    expect(screen.getByText("# Existing document")).toHaveAttribute("data-located", "true");
    scroller.scrollTop = 120;
    fireEvent.click(tab("a.md"));
    fireEvent.click(screen.getByRole("button", { name: /^Open file: Read missing heading/ }));
    expect(await screen.findByRole("note")).toHaveTextContent("The requested heading wasn't found.");
    expect(screen.getByRole("button", { name: "Source", pressed: true })).toBeVisible();
    expect(screen.getByText("# Existing document")).toHaveAttribute("data-located", "true");
    expect(scroller.scrollTop).toBe(120);
    fireEvent.click(tab("a.md"));
    fireEvent.click(screen.getByRole("button", { name: /^Open file: Read existing heading/ }));
    expect(await screen.findByRole("heading", { name: "Existing document" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Reading", pressed: true })).toBeVisible();
    expect(files.fileRequests("b.md").filter(([input]) => new URL(String(input)).pathname.endsWith("/files"))).toHaveLength(1);
  });

  it("lets a draft from the retained source survive that source's Reload", async () => {
    const source = "/other";
    const target = `${source}/report.md`;
    const files = documentRead({ "a.md": `# Original\n\n[Read report](${target})` });
    files.meta(target, () => metadata("report.md", source));
    const failed = files.body("report.md");
    openReader();
    await screen.findByRole("heading", { name: "Original" });
    const reload = files.body("a.md");
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(4));
    await openLink("Read report");
    await waitFor(() => expect(files.fileRequests("report.md")).toHaveLength(1));
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    try {
      await act(async () => failed.resolve(new Response(null, { status: 404 })));
      fireEvent.click(screen.getByRole("button", { name: `Edit path for ${target}` }));
      fireEvent.change(screen.getByRole("textbox", { name: "File path" }), { target: { value: "fixed.md" } });
      await act(async () => vi.advanceTimersByTime(6000));
      await act(async () => reload.resolve(new Response("# Refreshed original")));
      expect(screen.getByRole("heading", { name: "Refreshed original" })).toBeVisible();
      expect(screen.getByRole("alert")).toBeVisible();
      await act(async () => vi.advanceTimersByTime(2000));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.getByRole("textbox", { name: "File path" })).toHaveValue("fixed.md");
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file" })));
      const request = new URL(String(files.fileRequests("fixed.md")[0][0]));
      expect(request.searchParams.get("source")).toBe(source);
      expect(screen.getByRole("heading", { name: "fixed.md body" })).toBeVisible();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reports a previously valid heading that is missing from the reloaded body", async () => {
    const files = documentRead({ "a.md": "# Original" });
    openReader("a.md", { kind: "heading", slug: "original" });
    await screen.findByRole("heading", { name: "Original" });
    const reload = files.body("a.md");
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(4));
    await act(async () => reload.resolve(new Response("# Revised document")));
    expect(screen.getByRole("heading", { name: "Revised document" })).toBeVisible();
    expect(screen.getByRole("note")).toHaveTextContent("The requested heading wasn't found.");
  });

  it.each(["expire", "dismiss"] as const)("lets the retained position notice %s independently of Reload", async (outcome) => {
    const files = documentRead({ "a.md": "# Original\n\n[Outside](#L99)" });
    openReader("a.md", { kind: "heading", slug: "original" });
    await screen.findByRole("heading", { name: "Original" });
    const reload = files.body("a.md");
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(4));
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    try {
      fireEvent.click(screen.getByRole("link", { name: "Outside" }));
      expect(screen.getByRole("note")).toHaveTextContent("The requested lines are outside this file.");
      await act(async () => vi.advanceTimersByTime(6000));
      if (outcome === "dismiss") fireEvent.click(screen.getByRole("button", { name: "Dismiss position notice" }));
      const next = outcome === "dismiss" ? "# Revised document" : "# Original\n\nNew snapshot";
      await act(async () => reload.resolve(new Response(next)));
      if (outcome === "dismiss") {
        expect(screen.getByRole("heading", { name: "Revised document" })).toBeVisible();
        expect(screen.getByRole("note")).toHaveTextContent("The requested heading wasn't found.");
      } else {
        expect(screen.getByText("New snapshot")).toBeVisible();
        expect(screen.getByRole("note")).toHaveTextContent("The requested lines are outside this file.");
        await act(async () => vi.advanceTimersByTime(2000));
        expect(screen.queryByRole("note")).not.toBeInTheDocument();
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it("opens the explicit path draft while the first file is still reading", async () => {
    const files = documentRead({ "a.md": "# Original A", "picked.md": "# Recovered selection" });
    const first = files.body("a.md");
    openReader();
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(2));
    files.meta("picked.md", Promise.resolve(new Response(null, { status: 404 })));
    fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
    fireEvent.click(within(await screen.findByRole("list")).getByRole("button", { name: "picked.md" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't open picked.md.");
    fireEvent.click(screen.getByRole("button", { name: "Edit path for picked.md" }));
    expect(screen.getByRole("textbox", { name: "File path" })).toHaveValue("picked.md");
    files.meta("picked.md", () => metadata("picked.md"));
    fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    expect(await screen.findByRole("heading", { name: "Recovered selection" })).toBeVisible();
    await act(async () => first.resolve(new Response("# Original A")));
    expect(selected()).toBe(tab("picked.md"));
    fireEvent.click(tab("a.md"));
    expect(screen.getByRole("heading", { name: "Original A" })).toBeVisible();
  });

  it.each(["opening", "Reload"] as const)("uses the confirmed source for a relative edit after %s fails", async (operation) => {
    const source = "/other";
    const alias = `${source}/alias.md`;
    const files = documentRead({ "report.md": "# Original report", "fixed.md": "# Correct destination" });
    files.meta(alias, () => metadata("report.md", source));
    files.meta("fixed.md", (url) => url.searchParams.get("source") === source
      ? metadata("fixed.md", source) : metadata("wrong.md"));
    const initialFailure = operation === "opening" ? files.body("report.md") : undefined;
    openReader(alias);
    if (!initialFailure) await screen.findByRole("heading", { name: "Original report" });
    const failure = initialFailure ?? files.body("report.md");
    if (!initialFailure) fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await waitFor(() => expect(files.fileRequests("report.md")).toHaveLength(initialFailure ? 1 : 2));
    await act(async () => failure.resolve(new Response(null, { status: 403 })));
    fireEvent.click(screen.getByRole("button", { name: "Edit path" }));
    fireEvent.change(screen.getByRole("textbox", { name: "File path" }), { target: { value: "fixed.md" } });
    fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    expect(await screen.findByRole("heading", { name: "Correct destination" })).toBeVisible();
    expect(new URL(String(files.fileRequests("fixed.md")[0][0])).searchParams.get("source")).toBe(source);
  });

  it("reconfirms an implicit command alias from the original cwd and rechecks the prior source", async () => {
    const source = "/shared";
    const read = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/showme/resolve")) {
        expect(JSON.parse(String(init?.body))).toEqual({ text: "alias.md" });
        expect(url.searchParams.get("source")).toBeNull();
        const prior = url.searchParams.get("reload_source");
        expect(prior).toBe(read.mock.calls.length === 1 ? null : source);
        const file = prior ? "replacement.md" : "report.md";
        return Response.json({ target: { path: "alias.md" }, metadata: await metadata(file, source).json() });
      }
      return url.searchParams.get("path") === "report.md"
        ? Response.json({ detail: "The file path changed.", code: "target_changed" }, { status: 409 })
        : new Response("# Reconfirmed alias");
    });
    vi.stubGlobal("fetch", read);
    useSessionStore.setState({ token: "tok", viewer: null, activeSessionId: "foreground", mainView: "chat" });
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("original", "alias.md", { mode: "command" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The target file changed.");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { name: "Reconfirmed alias" })).toBeVisible();
    expect(read).toHaveBeenCalledTimes(4);
  });

  it.each(["relative path after reload", "position after reload", "position after byte denial", "position after retry"] as const)(
    "keeps an implicit alias's confirmed source when editing %s",
    async (operation) => {
      const source = "/shared";
      const editingAfterReload = operation.endsWith("after reload");
      const editingAfterRetry = operation === "position after retry";
      const changingPath = operation === "relative path after reload";
      const expectedPath = changingPath ? "fixed.md" : `${source}/report.md`;
      let aliasTarget = "report.md";
      let requestedEdit: URL | undefined;
      let metadataRequests = 0;
      let resolutions = 0;
      vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input));
        if (url.pathname.endsWith("/showme/resolve")) {
          expect(JSON.parse(String(init?.body))).toEqual({ text: "alias.md" });
          expect(url.searchParams.get("source")).toBeNull();
          if (++resolutions > 1) {
            expect(url.searchParams.get("reload_source")).toBe(source);
            if (aliasTarget === "report.md") return Response.json({ detail: "File not found." }, { status: 404 });
          }
          return Response.json({ target: { path: "alias.md" }, metadata: await metadata(aliasTarget, source).json() });
        }
        if (url.pathname.endsWith("/meta")) {
          metadataRequests++;
          requestedEdit = url;
          const correct = url.searchParams.get("source") === source && url.searchParams.get("path") === expectedPath;
          return correct ? metadata(changingPath ? "fixed.md" : "report.md", source) : metadata("other.md");
        }
        if (!requestedEdit && !editingAfterReload) {
          return editingAfterRetry
            ? Response.json({ detail: "The file path changed.", code: "target_changed" }, { status: 409 })
            : new Response(null, { status: 403 });
        }
        const file = url.searchParams.get("path");
        if (!changingPath && metadataRequests === 1 && file === "report.md") {
          return Response.json({ detail: "The file path changed.", code: "target_changed" }, { status: 409 });
        }
        const heading = file === "other.md" ? "Wrong file" : file === "new.md" ? "Repointed file" : "Original file";
        return new Response(`# ${heading}`);
      }));
      useSessionStore.setState({ token: "tok", viewer: null, activeSessionId: "foreground", mainView: "chat" });
      render(<FileViewerDialog />);
      act(() => useSessionStore.getState().openViewer("original", "alias.md", { mode: "command" }));
      if (editingAfterReload) {
        await screen.findByRole("heading", { name: "Original file" });
        fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
      } else if (editingAfterRetry) {
        fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
      }
      const expectedError = editingAfterReload || editingAfterRetry ? "not found" : "don't have access";
      await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(expectedError));
      fireEvent.click(screen.getByRole("button", { name: "Edit path" }));
      if (changingPath) {
        fireEvent.change(screen.getByRole("textbox", { name: "File path" }), { target: { value: "fixed.md" } });
      } else {
        fireEvent.change(screen.getByRole("textbox", { name: "Position (optional)" }), { target: { value: "#original-file" } });
      }
      fireEvent.click(screen.getByRole("button", { name: "Open file" }));
      const confirmedCommands = resolutions;
      aliasTarget = "new.md";
      if (!changingPath) {
        expect(await screen.findByRole("alert")).toHaveTextContent("The target file changed.");
        fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      }
      expect(await screen.findByRole("heading", { name: "Original file" })).toBeVisible();
      expect(requestedEdit?.searchParams.get("path")).toBe(expectedPath);
      expect(requestedEdit?.searchParams.get("source")).toBe(source);
      expect(resolutions).toBe(confirmedCommands);
      if (!changingPath) {
        expect(metadataRequests).toBe(2);
        expect(requestedEdit?.searchParams.get("reload_source")).toBe(source);
      }

      fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
      if (changingPath) {
        await waitFor(() => expect(metadataRequests).toBe(2));
        expect(requestedEdit?.searchParams.get("path")).toBe("fixed.md");
        expect(resolutions).toBe(confirmedCommands);
      } else {
        expect(await screen.findByRole("heading", { name: "Repointed file" })).toBeVisible();
        expect(resolutions).toBe(confirmedCommands + 1);
        expect(metadataRequests).toBe(2);
      }
    },
  );

  it.each([false, true])("keeps a failed first file available after a different file is ready (initial picker: %s)", async (picker) => {
    const files = documentRead({ "a.md": "# First file recovered", "picked.md": "# Later file" });
    const firstBody = files.body("a.md");
    if (picker) {
      useSessionStore.setState({ token: "tok", viewer: null, activeSessionId: "foreground", mainView: "chat" });
      render(<FileViewerDialog />);
      act(() => useSessionStore.getState().openViewer("original", "", { mode: "picker" }));
      fireEvent.click(await screen.findByRole("button", { name: "a.md" }));
    } else {
      openReader();
    }
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
    fireEvent.click(await screen.findByRole("button", { name: "picked.md" }));
    await screen.findByRole("heading", { name: "Later file" });
    await act(async () => firstBody.reject(new TypeError("Network disconnected")));
    expect(selected()).toBe(tab("picked.md"));
    fireEvent.click(tab("a.md"));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { name: "First file recovered" })).toBeVisible();
    expect(files.fileRequests("a.md")).toHaveLength(3);
  });

  it("focuses the next ready tab when closing a file beside a failed reload", async () => {
    const files = documentRead({ "a.md": "# A\n\n[Read B](b.md)\n\n[Read C](c.md)" });
    openReader();
    await openLink("Read B");
    await screen.findByRole("heading", { name: "b.md body" });
    const reloaded = files.body("b.md");
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await act(async () => reloaded.resolve(new Response(null, { status: 404 })));
    expect(await screen.findByRole("alert")).toHaveTextContent("File not found");
    fireEvent.click(tab("a.md"));
    await openLink("Read C");
    await screen.findByRole("heading", { name: "c.md body" });
    fireEvent.click(tab("a.md"));
    tab("a.md").focus();
    fireEvent.keyDown(tab("a.md"), { key: "Delete" });
    expect(selected()).toBe(tab("c.md"));
    expect(tab("c.md")).toHaveFocus();
    expect(tab("b.md")).toBeVisible();
  });

  it("assigns distinct cancel controls after aliases confirm colliding filenames", async () => {
    const files = documentRead({ "a.md": "[B](b.md)\n\n[C](c.md)\n\n[D](d.md)" });
    const targets = ["one/file.md", "two/file.md", "three/b.md"];
    for (const [index, alias] of ["b.md", "c.md", "d.md"].entries()) {
      const target = targets[index];
      files.meta(alias, () => metadata(target));
      files.body(target);
    }
    openReader();
    for (const name of ["B", "C", "D"]) await openLink(name);
    await waitFor(() => expect(targets.map((target) => files.fileRequests(target).length)).toEqual([1, 1, 1]));
    const controls = screen.getAllByRole("button", { name: /^Cancel opening / });
    const labels = controls.map((control) => control.getAttribute("aria-label")!);
    expect(new Set(labels).size).toBe(3);
    fireEvent.click(screen.getByRole("button", { name: labels[0] }));
    expect(targets.map((target) => files.fileRequests(target)[0][1]?.signal?.aborted)).toEqual([true, false, false]);
  });

  it("distinguishes Retry for a slow read from Retry for the latest failure", async () => {
    const files = documentRead({ "a.md": "[Read target](target.md)" });
    const pending = files.body("target.md");
    const failed = files.body("target.md");
    openReader();
    const link = await screen.findByRole("button", { name: "Open file: Read target" });
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(link));
      await act(async () => fireEvent.click(link));
      await act(async () => failed.resolve(new Response(null, { status: 503 })));
      await act(async () => vi.advanceTimersByTime(5000));
      const [pendingRetry, failedRetry] = screen.getAllByRole("button", { name: /^Retry opening / });
      const pendingName = pendingRetry.getAttribute("aria-label")!;
      expect(failedRetry).not.toHaveAccessibleName(pendingName);
      await act(async () => fireEvent.click(screen.getByRole("button", { name: pendingName })));
      const bytes = files.fileRequests("target.md").filter(([input]) => !new URL(String(input)).pathname.endsWith("/meta"));
      expect(bytes[0][1]?.signal?.aborted).toBe(true);
      expect(bytes).toHaveLength(3);
      await act(async () => pending.resolve(new Response("# Retired read")));
      expect(screen.queryByText("Retired read")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("lets the picker reopen a file while its initial byte request is still pending", async () => {
    const files = documentRead({ "a.md": "# Recovered document" });
    const initial = files.body("a.md");
    openReader();
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
    fireEvent.click(await screen.findByRole("button", { name: "a.md" }));
    expect(await screen.findByRole("heading", { name: "Recovered document" })).toBeVisible();
    await act(async () => initial.resolve(new Response(null, { status: 403 })));
    expect(selected()).toBe(tab("a.md"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(files.fileRequests("a.md")).toHaveLength(4);
  });

  it("keeps the failed read's source when editing only the position", async () => {
    const source = "/shared/docs";
    const alias = `${source}/alias.md`;
    const files = documentRead({ "a.md": `# Original document\n\n[Read alias](${alias})` });
    const stalled = files.body("plan.md");
    const denied = files.body("plan.md");
    files.meta(alias, () => metadata("plan.md", source));
    openReader();
    const link = await screen.findByRole("button", { name: "Open file: Read alias" });
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(link));
      expect(files.fileRequests("plan.md")).toHaveLength(1);
      files.meta(alias, (url) => url.searchParams.get("source") === source
        ? new Response(null, { status: 403 }) : metadata("docs/plan.md", "/shared"));
      await act(async () => vi.advanceTimersByTime(5000));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening plan.md" })));
      await act(async () => denied.resolve(new Response(null, { status: 403 })));
      expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
      fireEvent.click(screen.getByRole("button", { name: `Edit path for ${alias}` }));
      expect(screen.getByRole("textbox", { name: "File path" })).toHaveValue(alias);
      fireEvent.change(screen.getByRole("textbox", { name: "Position (optional)" }), { target: { value: "#L3" } });
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file" })));
      expect(new URL(String(files.fileRequests(alias)[1][0])).searchParams.get("source")).toBe(source);
      expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
      expect(screen.getByRole("heading", { name: "Original document" })).toBeVisible();
      await act(async () => stalled.resolve(new Response("# Retired body")));
    } finally {
      vi.useRealTimers();
    }
  });

  it("honors a cleared position when a failed link is edited and reuses an open tab", async () => {
    const files = documentRead({
      "a.md": "# Guide\n\n[Direct link](b.md)\n\n[Alias at line three](alias.md#L3)\n\n[Another link](another.md)",
      "b.md": "# Report\n\nThe third line\n\nThe fifth line\n\n[Jump to five](#L5)",
    });
    files.meta("b.md", Promise.resolve(new Response(null, { status: 404 })));
    files.meta("alias.md", () => metadata("b.md"));
    openReader();
    await openLink("Alias at line three");
    expect(await screen.findByText("The third line")).toHaveAttribute("data-located", "true");
    fireEvent.click(tab("a.md"));
    await openLink("Direct link");
    await screen.findByRole("alert");
    files.meta("b.md", () => metadata("b.md"));
    fireEvent.click(screen.getByRole("button", { name: "Edit path for b.md" }));
    expect(screen.getByRole("textbox", { name: "Position (optional)" })).toHaveValue("");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file" })));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.getByRole("button", { name: "Reading", pressed: true })).toBeVisible();

    fireEvent.click(screen.getByRole("link", { name: "Jump to five" }));
    expect(screen.getByText("The fifth line")).toHaveAttribute("data-located", "true");
    fireEvent.click(tab("a.md"));
    files.meta("another.md", Promise.resolve(new Response(null, { status: 404 })));
    await openLink("Another link");
    await screen.findByRole("alert");
    files.meta("another.md", () => metadata("b.md"));
    fireEvent.click(screen.getByRole("button", { name: "Edit path for another.md" }));
    expect(screen.getByRole("textbox", { name: "Position (optional)" })).toHaveValue("");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file" })));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.getByRole("button", { name: "Reading", pressed: true })).toBeVisible();
    expect(files.fileRequests("b.md").filter(([input]) => new URL(String(input)).pathname.endsWith("/files"))).toHaveLength(1);
  });

  it.each(["failure after cancel", "failure before cancel", "success after cancel"] as const)(
    "preserves the older candidate's outcome without activating it: %s", async (outcome) => {
      const files = documentRead({ "a.md": "# Original document\n\n[Read alias](alias.md)" });
      const older = deferred<Response>();
      const newer = deferred<Response>();
      files.meta("alias.md", older.promise);
      openReader();
      await openLink("Read alias");
      await waitFor(() => expect(files.fileRequests("alias.md")).toHaveLength(1));
      files.meta("alias.md", newer.promise);
      await openLink("Read alias");
      await waitFor(() => expect(files.fileRequests("alias.md")).toHaveLength(2));
      const cancelOpening = screen.getByRole("button", { name: /^Cancel opening alias\.md .*attempt 2$/ });
      const fails = () => older.resolve(new Response(null, { status: 404 }));
      if (outcome === "failure before cancel") await act(async () => fails());
      fireEvent.click(cancelOpening);
      if (outcome === "failure after cancel") await act(async () => fails());
      if (outcome === "success after cancel") await act(async () => older.resolve(metadata("b.md")));

      expect(files.fileRequests("alias.md")[1][1]?.signal?.aborted).toBe(true);
      expect(selected()).toBe(tab("a.md"));
      if (outcome === "success after cancel") {
        fireEvent.click(await screen.findByRole("tab", { name: "b.md" }));
        expect(screen.getByRole("heading", { name: "b.md body" })).toBeVisible();
      } else {
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      }
    },
  );

  it("re-resolves an alias after it is repointed, across source boundaries", async () => {
    const files = documentRead({ "a.md": "# A\n\n[Follow alias](/shared/alias.md)" });
    files.meta("/shared/alias.md", Promise.resolve(metadata("b.md", "/shared")));
    openReader();
    await openLink("Follow alias");
    expect(await screen.findByRole("heading", { name: "b.md body" })).toBeVisible();
    fireEvent.click(tab("a.md"));
    files.meta("/shared/alias.md", Promise.resolve(metadata("c.md", "/shared/new")));
    await openLink("Follow alias");
    expect(await screen.findByRole("heading", { name: "c.md body" })).toBeVisible();
    expect(files.fileRequests("/shared/alias.md")).toHaveLength(2);
    expect(files.fileRequests("b.md")).toHaveLength(1);
  });

  it.each([
    [0, "Check your connection", "retry"], [403, "don't have access", "edit"], [404, "File not found", "edit"],
    [409, "target file changed", "reconfirm"], [413, "2 MiB limit", "edit"], [415, "format can't be previewed", "edit"],
  ])("reports failure %i on the originating tab with the recovery for its cause", async (status, message, recovery) => {
    const files = documentRead({ "a.md": "# Original\n\n[Read B](b.md)", "b.md": "# Retried document" });
    const failed = files.body("b.md");
    openReader("a.md", { kind: "heading", slug: "original" });
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(2));
    const scroller = screen.getByRole("heading", { name: "Original" }).closest(".file-viewer-body")!;
    scroller.scrollTop = 135;
    await act(async () => {
      if (status === 409) failed.resolve(Response.json({ detail: "File path changed.", code: "target_changed" }, { status }));
      else if (status) failed.resolve(new Response(null, { status }));
      else failed.reject(new TypeError("Network disconnected"));
    });
    expect(screen.getByRole("alert")).toHaveTextContent(`Couldn't open b.md.`);
    expect(screen.getByRole("alert")).toHaveTextContent(message);
    expect(selected()).toBe(tab("a.md"));
    expect(scroller.scrollTop).toBe(135);
    if (recovery === "edit") {
      expect(screen.queryByRole("button", { name: "Retry opening b.md" })).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Edit path for b.md" }));
      expect(screen.getByRole("textbox", { name: "File path" })).toHaveValue("b.md");
      fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    } else {
      expect(screen.queryByRole("button", { name: "Edit path for b.md" })).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Retry opening b.md" }));
    }
    expect(await screen.findByRole("heading", { name: "Retried document" })).toBeVisible();
    const requests = files.fileRequests("b.md");
    expect(requests).toHaveLength(recovery === "retry" ? 3 : 4);
    if (recovery === "reconfirm") {
      const metadata = new URL(String(requests[2][0]));
      expect(metadata.pathname).toMatch(/\/files\/meta$/);
      expect(metadata.searchParams.get("source")).toBe(root);
      expect(metadata.searchParams.get("source_directory")).toBe(".");
      expect(metadata.searchParams.get("source_directory_id")).toBe(root);
      expect(metadata.searchParams.get("reload_source")).toBe(root);
    }
  });

  it("cancels the newer request without restoring the earlier request's activation", async () => {
    const files = documentRead({ "a.md": "# A\n\n[Read B](b.md)\n\n[Read C](c.md)" });
    const b = files.body("b.md");
    const c = files.body("c.md");
    openReader();
    await openLink("Read B");
    await openLink("Read C");
    await waitFor(() => expect(files.fileRequests("c.md")).toHaveLength(2));
    cancel("c.md");
    expect(files.fileRequests("c.md")[1][1]?.signal?.aborted).toBe(true);
    await act(async () => {
      c.resolve(new Response("# C leaked"));
      b.resolve(new Response("# B ready"));
    });
    expect(selected()).toBe(tab("a.md"));
    expect(screen.queryByRole("tab", { name: "c.md" })).not.toBeInTheDocument();
    fireEvent.click(tab("b.md"));
    expect(screen.getByRole("heading", { name: "B ready" })).toBeVisible();
  });

  it.each(["resolve", "reject"] as const)("ignores a closed entry's late %s after reopening the same file", async (outcome) => {
    const files = documentRead({ "a.md": "# A\n\n[Read B](b.md)" });
    const former = files.body("b.md");
    const current = files.body("b.md");
    openReader();
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(2));
    cancel("b.md");
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(4));
    await act(async () => current.resolve(new Response("# Current attempt")));
    await act(async () => {
      if (outcome === "resolve") former.resolve(new Response("# Retired text"));
      else former.reject(new Error("Retired error"));
    });
    expect(screen.getByRole("heading", { name: "Current attempt" })).toBeVisible();
    expect(screen.queryByText("Retired text")).not.toBeInTheDocument();
    fireEvent.click(tab("a.md"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps a reloading tab selectable and updates only that tab after switching", async () => {
    const files = documentRead({ "a.md": "# Original A\n\n[Read B](b.md)", "b.md": "# Reading B" });
    openReader();
    await openLink("Read B");
    await screen.findByRole("heading", { name: "Reading B" });
    const bScroll = screen.getByRole("tabpanel").querySelector<HTMLElement>(".file-viewer-body")!;
    bScroll.scrollTop = 95;
    fireEvent.click(tab("a.md"));
    const original = screen.getByRole("heading", { name: "Original A" });
    const reload = files.body("a.md");
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(4));
    expect(original).toBeVisible();
    expect(screen.queryByRole("button", { name: "Edit path" })).not.toBeInTheDocument();
    fireEvent.click(tab("b.md"));
    expect(tab("a.md")).toBeVisible();
    await act(async () => reload.resolve(new Response("# Updated A")));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.getByRole("heading", { name: "Reading B" })).toBeVisible();
    expect(bScroll.scrollTop).toBe(95);
    fireEvent.click(tab("a.md"));
    expect(await screen.findByRole("heading", { name: "Updated A" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Edit path" }));
    expect(screen.getByRole("textbox", { name: "File path" })).toBeVisible();
    expect(screen.queryByText("Original A")).not.toBeInTheDocument();
    expect(files.fileRequests("b.md")).toHaveLength(2);
  });

  it.each(["another tab", "the retained snapshot"] as const)("preserves a newer location from %s when reloading", async (origin) => {
    const files = documentRead({
      "a.md": "# Original A\n\n[Read B](b.md)\n\n[Go to line](#L7)\n\nOld target line",
      "b.md": "# B\n\n[Find line in A](a.md#L7)",
    });
    openReader();
    if (origin === "another tab") {
      await openLink("Read B");
      await screen.findByRole("heading", { name: "B" });
      fireEvent.click(tab("a.md"));
    } else {
      await screen.findByRole("heading", { name: "Original A" });
    }
    const firstReload = files.body("a.md");
    const retry = files.body("a.md");
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Reload file" })));
      expect(files.fileRequests("a.md")).toHaveLength(4);
      if (origin === "another tab") {
        fireEvent.click(tab("b.md"));
        await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Find line in A at line 7" })));
      } else {
        fireEvent.click(screen.getByRole("link", { name: "Go to line" }));
      }
      expect(screen.getByText("Old target line", { selector: "[data-line]" })).toHaveAttribute("data-located", "true");
      await act(async () => vi.advanceTimersByTime(5000));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry reloading a.md" })));
      await act(async () => retry.resolve(new Response("# Reloaded A\n\nLine three\n\nLine five\n\nNew target line")));
      await act(async () => firstReload.resolve(new Response("# Stale reload")));
      expect(screen.getByText("New target line", { selector: "[data-line]" })).toHaveAttribute("data-located", "true");
      expect(screen.queryByText("Stale reload")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    [0, "Check your connection"], [404, "File not found"], [403, "don't have access"], [413, "2 MiB limit"],
  ])("withdraws the original snapshot and offers the proper recovery for a reload failure (%s)", async (status, message) => {
    const files = documentRead({ "a.md": "# Original snapshot" });
    openReader();
    await screen.findByRole("heading", { name: "Original snapshot" });
    const original = tab("a.md").getAttribute("aria-controls");
    const reload = files.body("a.md");
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(4));
    await act(async () => {
      if (status) reload.resolve(new Response(null, { status }));
      else reload.reject(new TypeError("Network unavailable"));
    });
    expect(screen.queryByText("Original snapshot")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByRole("link", { name: "Download file" })).not.toBeInTheDocument();
    const retry = files.body("a.md");
    if (status) {
      expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Edit path" }));
      expect(screen.getByRole("textbox", { name: "File path" })).toHaveValue("a.md");
      fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    } else {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    }
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(status ? 6 : 5));
    await act(async () => retry.resolve(new Response("# Recovered snapshot")));
    expect(await screen.findByRole("heading", { name: "Recovered snapshot" })).toBeVisible();
    expect(tab("a.md")).toHaveAttribute("aria-controls", original);
  });

  it("keeps a newer reload and discards a reload response after the tab closes", async () => {
    const files = documentRead({ "a.md": "# Original A\n\n[Read B](b.md)" });
    openReader();
    await openLink("Read B");
    await screen.findByRole("heading", { name: "b.md body" });
    fireEvent.click(tab("a.md"));
    const former = files.body("a.md");
    const current = files.body("a.md");
    const reload = screen.getByRole("button", { name: "Reload file" });
    fireEvent.click(reload);
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(4));
    fireEvent.click(reload);
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(6));
    await act(async () => current.resolve(new Response("# Current A")));
    await act(async () => former.resolve(new Response(null, { status: 403 })));
    expect(await screen.findByRole("heading", { name: "Current A" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(files.fileRequests("a.md")[3][1]?.signal?.aborted).toBe(true);
    const retiring = files.body("a.md");
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(8));
    closeTab("a.md");
    await act(async () => retiring.resolve(new Response("# Retired A")));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.queryByText("Retired A")).not.toBeInTheDocument();
    expect(files.fileRequests("a.md")[7][1]?.signal?.aborted).toBe(true);
  });

  it.each(["duplicate is still open", "duplicate was closed", "reload bytes denied"] as const)(
    "settles an alias reload in its own slot when the %s", async (outcome) => {
      const files = documentRead({ "a.md": "# Original A\n\n[Read B](b.md)" });
      openReader();
      await openLink("Read B");
      await screen.findByRole("heading", { name: "b.md body" });
      const foreground = screen.getByRole("tabpanel").querySelector<HTMLElement>(".file-viewer-body")!;
      foreground.scrollTop = 95;
      fireEvent.click(tab("a.md"));
      files.meta("a.md", Promise.resolve(metadata("b.md")));
      const pending = files.body("b.md");
      fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
      await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(3));
      fireEvent.click(tab("b.md"));
      if (outcome === "duplicate was closed") closeTab("b.md");
      await act(async () => pending.resolve(outcome === "reload bytes denied"
        ? new Response(null, { status: 403 }) : new Response("# Read from the current alias")));
      if (outcome === "duplicate was closed") {
        expect(await screen.findByRole("heading", { name: "Read from the current alias" })).toBeVisible();
        expect(screen.getByRole("tab")).toHaveTextContent("b.md");
      } else {
        expect(selected()).toBe(tab("b.md"));
        expect(screen.getByRole("heading", { name: "b.md body" })).toBeVisible();
        expect(foreground.scrollTop).toBe(95);
        fireEvent.click(tab("a.md"));
        expect(screen.getByRole("alert")).toHaveTextContent(outcome === "reload bytes denied"
          ? "don't have access" : "already open in another tab");
        expect(screen.getByRole("button", { name: "Edit path" })).toBeVisible();
        expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
      }
      expect(screen.queryByText("Original A")).not.toBeInTheDocument();
    },
  );

  it("retains the origin after a path edit fails to reauthorize a known file", async () => {
    const files = documentRead({
      "a.md": "[Read B](b.md)\n\n[Read C](c.md)",
      "b.md": "# Saved B",
      "c.md": "# Save my place",
    });
    openReader();
    await openLink("Read B");
    await screen.findByRole("heading", { name: "Saved B" });
    fireEvent.click(tab("a.md"));
    await openLink("Read C");
    await screen.findByRole("heading", { name: "Save my place" });
    files.meta("b.md", Promise.resolve(new Response(null, { status: 403 })));
    fireEvent.click(screen.getByRole("button", { name: "Edit path" }));
    fireEvent.change(screen.getByRole("textbox", { name: "File path" }), { target: { value: "b.md" } });
    fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("don't have access");
    expect(selected()).toBe(tab("c.md"));
    expect(screen.getByRole("heading", { name: "Save my place" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Edit path for b.md" }));
    const pending = deferred<Response>();
    files.meta("b.md", pending.promise);
    fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(4));
    closeTab("c.md");
    expect(files.fileRequests("b.md")[3][1]?.signal?.aborted).toBe(false);
    await act(async () => pending.resolve(metadata("b.md")));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.getByRole("heading", { name: "Saved B" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(files.fileRequests("b.md")[3][1]?.signal?.aborted).toBe(true);
  });

  it("ignores superseded responses when the same tab starts a newer attempt", async () => {
    const files = documentRead({ "a.md": "# Current attempt" });
    const former = files.body("a.md");
    openReader();
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(2));
    const slot = tab("a.md").getAttribute("aria-controls");
    fireEvent.click(screen.getByRole("button", { name: "Retry file" }));
    expect(await screen.findByRole("heading", { name: "Current attempt" })).toBeVisible();
    await act(async () => former.resolve(new Response(null, { status: 403 })));
    expect(tab("a.md")).toHaveAttribute("aria-controls", slot);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(files.fileRequests("a.md")).toHaveLength(3);
  });

  it.each(["automatic", "select C", "locate in C", "page source in C"] as const)("keeps B's activation consistent after closing A and %s", async (next) => {
    const files = documentRead({
      "left.md": "# Left\n\n[Read A](a.md)", "a.md": "# A\n\n[Read C](c.md)\n\n[Read B](b.md)",
      "c.md": next === "page source in C" ? `# In C\n${"source line\n".repeat(2001)}` : "# In C\n\n[Locate](#in-c)",
    });
    const b = files.body("b.md");
    openReader("left.md");
    await openLink("Read A");
    await openLink("Read C");
    await screen.findByRole("heading", { name: "In C" });
    fireEvent.click(tab("a.md"));
    await openLink("Read B");
    closeTab("a.md");
    expect(selected()).toBe(tab("c.md"));
    if (next === "select C") fireEvent.click(tab("c.md"));
    if (next === "locate in C") fireEvent.click(screen.getByRole("link", { name: "Locate" }));
    if (next === "page source in C") {
      fireEvent.click(screen.getByRole("button", { name: "Source" }));
      fireEvent.click(screen.getByRole("button", { name: "Next lines" }));
    }
    await act(async () => b.resolve(new Response("# B ready")));
    expect(selected()).toBe(tab(next === "automatic" ? "b.md" : "c.md"));
    if (next === "automatic") closeTab("b.md");
    closeTab("c.md");
    expect(selected()).toBe(tab(next === "automatic" ? "left.md" : "b.md"));
  });

  it("discards a candidate's failure when its originating tab is closed", async () => {
    const files = documentRead({ "a.md": "# A\n\n[Read B](b.md)\n\n[Read C](c.md)" });
    const b = files.body("b.md");
    openReader();
    await openLink("Read C");
    await screen.findByRole("heading", { name: "c.md body" });
    fireEvent.click(tab("a.md"));
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(2));
    closeTab("a.md");
    await act(async () => b.resolve(new Response(null, { status: 404 })));
    expect(screen.queryByText("b.md loading…")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(selected()).toBe(tab("c.md"));
  });

  it.each([true, false])("keeps the empty viewer's selection failure after the old read fails first: %s", async (oldFirst) => {
    const files = documentRead({ "start.md": "[Read A](a.md)", "a.md": "# Selected file after retry" });
    const former = files.body("a.md");
    openReader("start.md");
    await openLink("Read A");
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(2));
    closeTab("start.md");
    fireEvent.click(screen.getByRole("button", { name: "Choose a file" }));
    const confirmation = deferred<Response>();
    files.meta("a.md", confirmation.promise);
    fireEvent.click(within(await screen.findByRole("list")).getByRole("button", { name: "a.md" }));
    await waitFor(() => expect(files.fileRequests("a.md")).toHaveLength(3));
    const failFormer = () => former.resolve(new Response(null, { status: 404 }));
    if (oldFirst) await act(async () => failFormer());
    await act(async () => confirmation.resolve(new Response(null, { status: 403 })));
    if (!oldFirst) await act(async () => failFormer());
    expect(screen.getByRole("alert")).toHaveTextContent("don't have access");
    files.meta("a.md", Promise.resolve(metadata("a.md")));
    fireEvent.click(screen.getByRole("button", { name: "Edit path for a.md" }));
    fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    expect(await screen.findByRole("heading", { name: "Selected file after retry" })).toBeVisible();
  });

  it.each([true, false])("settles an independent open while the same file is retrying its reload (reload succeeds: %s)", async (succeeds) => {
    const files = documentRead({ "a.md": "# Keep reading A\n\n[Read B](b.md)", "b.md": "# Old B" });
    openReader();
    await openLink("Read B");
    await screen.findByRole("heading", { name: "Old B" });
    const failed = files.body("b.md");
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await act(async () => failed.resolve(new Response(null, { status: 503 })));
    const reloading = files.body("b.md");
    const opening = files.body("b.md");
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(5));
    fireEvent.click(tab("a.md"));
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(7));
    await act(async () => reloading.resolve(succeeds ? new Response("# Reloaded B") : new Response(null, { status: 403 })));
    expect(selected()).toBe(tab("a.md"));
    await act(async () => opening.resolve(succeeds ? new Response("# Independently read B") : new Response(null, { status: 404 })));
    if (succeeds) {
      expect(screen.getAllByRole("tab", { name: "b.md" })).toHaveLength(1);
      expect(selected()).toBe(tab("b.md"));
      expect(screen.getByRole("heading", { name: "Reloaded B" })).toBeVisible();
    } else {
      expect(selected()).toBe(tab("a.md"));
      expect(screen.getByRole("alert")).toHaveTextContent("File not found");
      expect(screen.getByRole("button", { name: "Edit path for b.md" })).toBeVisible();
    }
  });

  it("can reuse the source the server confirms after closing a different potential match", async () => {
    const wide = "/shared";
    const narrow = "/shared/nested";
    const absolute = `${narrow}/report.md`;
    const files = documentRead({
      "a.md": "# A\n\n[Read wide](/shared/wide.md)\n\n[Read narrow](/shared/narrow.md)\n\n[Open report](/shared/nested/report.md)",
      "nested/report.md": "# Wide snapshot",
      "report.md": "# Narrow snapshot",
    });
    files.meta("/shared/wide.md", Promise.resolve(metadata("nested/report.md", wide, absolute)));
    files.meta("/shared/narrow.md", Promise.resolve(metadata("report.md", narrow, absolute)));
    openReader();
    await openLink("Read wide");
    await screen.findByRole("heading", { name: "Wide snapshot" });
    fireEvent.click(tab("a.md"));
    await openLink("Read narrow");
    await screen.findByRole("heading", { name: "Narrow snapshot" });
    fireEvent.click(tab("a.md"));
    const confirmation = deferred<Response>();
    files.meta(absolute, confirmation.promise);
    await openLink("Open report");
    await waitFor(() => expect(files.fileRequests(absolute)).toHaveLength(1));
    fireEvent.click(screen.getAllByRole("button", { name: "Close report.md" })[0]);
    await act(async () => confirmation.resolve(metadata("report.md", narrow, absolute)));
    expect(screen.getByRole("heading", { name: "Narrow snapshot" })).toBeVisible();
    expect(selected()).toBe(tab("report.md"));
    expect(files.fileRequests("report.md")).toHaveLength(1);
  });

  it.each([false, true])("retries a slow reuse confirmation without a second body read (later opening: %s)", async (later) => {
    const files = documentRead({ "a.md": "# Guide A\n\n[Read B](b.md)\n\n[Read C](c.md)", "b.md": "# Cached B" });
    openReader();
    await openLink("Read B");
    await screen.findByRole("heading", { name: "Cached B" });
    fireEvent.click(tab("a.md"));
    const stale = deferred<Response>();
    const retried = deferred<Response>();
    const c = files.body("c.md");
    files.meta("b.md", stale.promise);
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const open = (name: string) => act(async () => fireEvent.click(screen.getByRole("button", { name })));
      await open("Open file: Read B");
      if (later) await open("Open file: Read C");
      await act(async () => vi.advanceTimersByTime(5000));
      files.meta("b.md", retried.promise);
      await open("Retry confirmation");
      expect(files.fileRequests("b.md")).toHaveLength(4);
      expect(files.fileRequests("b.md")[2][1]?.signal?.aborted).toBe(true);
      expect(screen.queryByRole("button", { name: "Retry confirmation" })).not.toBeInTheDocument();
      await act(async () => stale.resolve(new Response(null, { status: 403 })));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(selected()).toBe(tab("a.md"));
      expect(screen.getByRole("heading", { name: "Guide A" })).toBeVisible();
      await act(async () => retried.resolve(metadata("b.md")));
      expect(selected()).toBe(tab(later ? "a.md" : "b.md"));
      expect(files.fileRequests("b.md")).toHaveLength(4);
      if (later) {
        expect(files.fileRequests("c.md")[1][1]?.signal?.aborted).toBe(false);
        await act(async () => c.resolve(new Response("# Ready C")));
        expect(selected()).toBe(tab("c.md"));
        expect(screen.getByRole("heading", { name: "Ready C" })).toBeVisible();
      } else {
        expect(screen.getByRole("heading", { name: "Cached B" })).toBeVisible();
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps parallel reuse confirmations independent after their source tab closes", async () => {
    const absolute = `${root}/b.md`;
    const files = documentRead({
      "a.md": `# A\n\n[Relative](b.md)\n\n[Absolute](${absolute})\n\n[Read C](c.md)`,
      "b.md": "# Cached B",
    });
    openReader();
    await openLink("Read C");
    await screen.findByRole("heading", { name: "c.md body" });
    fireEvent.click(tab("a.md"));
    await openLink("Relative");
    await screen.findByRole("heading", { name: "Cached B" });
    fireEvent.click(tab("a.md"));
    const relative = deferred<Response>();
    const absoluteCheck = deferred<Response>();
    files.meta("b.md", relative.promise);
    files.meta(absolute, absoluteCheck.promise);
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Relative" })));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Absolute" })));
      await act(async () => vi.advanceTimersByTime(5000));
      expect(screen.getByText(`Still confirming ${absolute}…`)).toBeVisible();
      expect(screen.getByRole("button", { name: `Retry confirmation for ${absolute}` })).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "Cancel confirmation for b.md" }));
      expect(files.fileRequests("b.md")[2][1]?.signal?.aborted).toBe(true);
      closeTab("a.md");
      expect(selected()).toBe(tab("c.md"));
      expect(files.fileRequests(absolute)[0][1]?.signal?.aborted).toBe(false);
      await act(async () => relative.resolve(metadata("cancelled.md")));
      expect(files.fileRequests("cancelled.md")).toHaveLength(0);
      await act(async () => absoluteCheck.resolve(metadata("b.md")));
      expect(selected()).toBe(tab("b.md"));
      expect(screen.getByRole("heading", { name: "Cached B" })).toBeVisible();
      expect(files.fileRequests("b.md")).toHaveLength(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("preserves whitespace in the current failed path and its draft", async () => {
    const paths = [`${root}/report  draft.md`, `${root}/report draft.md`];
    const links = paths.map((path, index) => `[Open ${index + 1}](${encodeURI(path)})`);
    const files = documentRead({ "a.md": `# Guide\n\n${links.join("\n\n")}` });
    openReader();
    for (const [index, path] of paths.entries()) {
      files.meta(path, () => new Response(null, { status: 404 }));
      await openLink(`Open ${index + 1}`);
      const alert = await screen.findByRole("alert");
      expect(alert.textContent).toContain(`Couldn't open ${path}.`);
      fireEvent.click(screen.getByRole("button", { name: `Edit path for ${paths[1]}` }));
      expect(screen.getByRole("textbox", { name: "File path" })).toHaveValue(path);
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    }
  });

  it("cancels a reuse confirmation when normalized path names and attempt suffixes collide", async () => {
    const names = ["report  draft.md", "report draft.md", "report draft.md, attempt 1"];
    const paths = names.map((name) => `${root}/${name}`);
    const links = paths.map((path, index) => `[Open ${index + 1}](${encodeURI(path)})`);
    const files = documentRead({ "a.md": `# Guide\n\n${links.join("\n\n")}` });
    openReader();
    for (const [index, path] of paths.entries()) {
      files.meta(path, () => metadata(names[index]));
      await openLink(`Open ${index + 1}`);
      await screen.findByRole("heading", { name: `${names[index].replace(/ +/g, " ")} body` });
      fireEvent.click(tab("a.md"));
    }
    const confirmations = paths.map(() => deferred<Response>());
    for (const index of [2, 0, 1]) {
      const path = paths[index];
      files.meta(path, confirmations[index].promise);
      await openLink(`Open ${index + 1}`);
      await waitFor(() => expect(files.fileRequests(path)).toHaveLength(2));
    }
    fireEvent.click(screen.getByRole("button", { name: `Cancel confirmation for ${paths[1]}, attempt 2` }));
    expect(files.fileRequests(paths[0])[1][1]?.signal?.aborted).toBe(true);
    expect(files.fileRequests(paths[1])[1][1]?.signal?.aborted).toBe(false);
    await act(async () => confirmations[0].resolve(metadata("cancelled.md")));
    expect(files.fileRequests("cancelled.md")).toHaveLength(0);
    await act(async () => confirmations[1].resolve(metadata(names[1])));
    expect(screen.getByRole("heading", { name: "report draft.md body" })).toBeVisible();
    expect(selected()).toHaveTextContent(names[1]);
    expect(files.fileRequests(names[1])).toHaveLength(1);
  });

  it("reuses a ready snapshot ahead of an older candidate for the same identity", async () => {
    const files = documentRead({ "start.md": "# Start\n\n[Read B](b.md)\n\n[Read alias](alias.md)", "b.md": "# Reloaded B" });
    const former = files.body("b.md");
    openReader("start.md");
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(2));
    await openLink("Read alias");
    await screen.findByRole("heading", { name: "alias.md body" });
    files.meta("alias.md", Promise.resolve(metadata("b.md")));
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await screen.findByRole("heading", { name: "Reloaded B" });
    expect(screen.getByRole("button", { name: "Cancel opening b.md" })).toBeVisible();
    fireEvent.click(tab("start.md"));
    await openLink("Read B");
    expect(await screen.findByRole("heading", { name: "Reloaded B" })).toBeVisible();
    await act(async () => former.resolve(new Response(null, { status: 404 })));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.getByRole("heading", { name: "Reloaded B" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each([false, true])("preserves the pending tab from an empty viewer with an explicit picker action of %s", async (pick) => {
    const files = documentRead({ "a.md": "# A\n\n[Read B](b.md)", "picked.md": "# Chosen from the source root" });
    const b = files.body("b.md");
    openReader();
    await openLink("Read B");
    closeTab("a.md");
    const choose = screen.getByRole("button", { name: "Choose a file" });
    expect(choose).toHaveFocus();
    if (pick) fireEvent.click(choose);
    await act(async () => b.resolve(new Response("# B ready")));
    if (pick) {
      expect(screen.getByRole("heading", { name: "Choose a file" })).toBeVisible();
      expect(screen.queryByRole("heading", { name: "B ready" })).not.toBeInTheDocument();
      fireEvent.click(await screen.findByRole("button", { name: "picked.md" }));
      expect(await screen.findByRole("heading", { name: "Chosen from the source root" })).toBeVisible();
      expect(tab("b.md")).toHaveAttribute("aria-selected", "false");
    } else expect(screen.getByRole("heading", { name: "B ready" })).toBeVisible();
  });

  it.each(["first", "second"] as const)("keeps the %s ready snapshot and locates within that snapshot", async (firstReady) => {
    const files = documentRead({ "a.md": "# Guide\n\n[First alias](p.md#L1)\n\n[Second alias](q.md#L3)" });
    files.meta("p.md", () => metadata("target.md"));
    files.meta("q.md", () => metadata("target.md"));
    const first = files.body("target.md");
    const second = files.body("target.md");
    openReader();
    await openLink("First alias");
    await waitFor(() => expect(files.fileRequests("target.md")).toHaveLength(1));
    await openLink("Second alias");
    await waitFor(() => expect(files.fileRequests("target.md")).toHaveLength(2));
    expect(selected()).toBe(tab("a.md"));
    const firstText = "# First snapshot\n\nFirst snapshot line";
    const secondText = "# Second snapshot\n\nSecond snapshot line";
    if (firstReady === "first") {
      await act(async () => first.resolve(new Response(firstText)));
      expect(selected()).toBe(tab("a.md"));
      await act(async () => second.resolve(new Response(secondText)));
    } else {
      await act(async () => second.resolve(new Response(secondText)));
      expect(selected()).toBe(tab("target.md"));
      await act(async () => first.resolve(new Response(firstText)));
    }
    expect(screen.getAllByRole("tab", { name: "target.md" })).toHaveLength(1);
    expect(selected()).toBe(tab("target.md"));
    const [kept, discarded] = firstReady === "first"
      ? ["First snapshot line", "Second snapshot line"] : ["Second snapshot line", "First snapshot line"];
    expect(screen.getByText(kept)).toHaveAttribute("data-located", "true");
    expect(screen.queryByText(discarded)).not.toBeInTheDocument();
  });

  it("scrolls to a repeated line target in an already-ready snapshot", async () => {
    const files = documentRead({ "a.md": "# Guide\n\n[Report line](b.md#L3)", "b.md": "# Report\n\nTarget line" });
    openReader();
    await openLink("Report line");
    const line = await screen.findByText("Target line");
    const body = line.closest<HTMLElement>(".file-viewer-body")!;
    vi.spyOn(line, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 42, 0, 0));
    body.scrollTop = 100;
    fireEvent.click(tab("a.md"));
    await openLink("Report line");
    await waitFor(() => expect(selected()).toBe(tab("b.md")));
    expect(body.scrollTop).toBe(142);
    expect(files.fileRequests("b.md")).toHaveLength(3);
  });

  it("keeps later activation eligible when an earlier byte attempt is retried in the background", async () => {
    const files = documentRead({ "a.md": "# Guide\n\n[Open B](b.md)\n\n[Open C](c.md)" });
    const retired = files.body("b.md");
    const retry = files.body("b.md");
    const latest = files.body("c.md");
    openReader();
    const earlier = await screen.findByRole("button", { name: "Open file: Open B" });
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(earlier));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Open C" })));
      await act(async () => vi.advanceTimersByTime(5000));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening b.md" })));
      const requests = files.fileRequests("b.md");
      expect(requests).toHaveLength(3);
      expect(requests[1][1]?.signal?.aborted).toBe(true);
      await act(async () => retry.resolve(new Response("# Retried B")));
      expect(selected()).toBe(tab("a.md"));
      await act(async () => latest.resolve(new Response("# Newer C")));
      expect(selected()).toBe(tab("c.md"));
      await act(async () => retired.resolve(new Response("# Retired B")));
      fireEvent.click(tab("b.md"));
      expect(screen.getByRole("heading", { name: "Retried B" })).toBeVisible();
      expect(screen.queryByText("Retired B")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("clears the old notice on a local jump and ignores a pending failure after a jump", async () => {
    const files = documentRead({ "a.md": "# Guide\n\n[Open B](b.md)\n\n[Open C](c.md)\n\n[Jump](#guide)" });
    const b = files.body("b.md");
    const c = files.body("c.md");
    openReader();
    await openLink("Open B");
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(2));
    await act(async () => b.resolve(new Response(null, { status: 503 })));
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open b.md.");
    fireEvent.click(screen.getByRole("link", { name: "Jump" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await openLink("Open C");
    await waitFor(() => expect(files.fileRequests("c.md")).toHaveLength(2));
    fireEvent.click(screen.getByRole("link", { name: "Jump" }));
    await act(async () => c.resolve(new Response(null, { status: 404 })));
    expect(selected()).toBe(tab("a.md"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps ready source rows across tab switches and unrelated loading feedback", async () => {
    const files = documentRead({ "a.md": "# Guide\n\n[Open source](main.py)\n\n[Later](later.md)", "main.py": "print('ready source')" });
    files.meta("main.py", () => metadata("main.py", root, undefined, undefined, undefined, "code"));
    const later = deferred<Response>();
    files.meta("later.md", later.promise);
    openReader();
    await openLink("Open source");
    await screen.findByText("print('ready source')");
    renderSource.mockClear();
    fireEvent.click(tab("a.md"));
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Later" })));
      await act(async () => vi.advanceTimersByTime(5000));
      expect(screen.getByText("later.md is taking longer to load")).toBeVisible();
      await act(async () => later.resolve(new Response(null, { status: 404 })));
      expect(screen.getByRole("alert")).toHaveTextContent("File not found.");
      fireEvent.click(tab("main.py"));
      expect(screen.getByText("print('ready source')")).toBeVisible();
      expect(renderSource).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the latest recovery when two openings fail in reverse order", async () => {
    const files = documentRead({ "a.md": "# A\n\n[Read B](b.md)\n\n[Read C](c.md)" });
    const b = files.body("b.md");
    const c = files.body("c.md");
    openReader();
    await openLink("Read B");
    await openLink("Read C");
    await waitFor(() => expect(files.fileRequests("c.md")).toHaveLength(2));
    await act(async () => c.resolve(new Response(null, { status: 503 })));
    await act(async () => b.resolve(new Response(null, { status: 403 })));
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open c.md.");
    fireEvent.click(screen.getByRole("button", { name: "Retry opening c.md" }));
    expect(await screen.findByRole("heading", { name: "c.md body" })).toBeVisible();
    fireEvent.click(tab("a.md"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("settles both sources independently and retires each notice on tab switch", async () => {
    const files = documentRead({
      "a.md": "# Guide A\n\n[Open guide B](b.md)\n\n[Open from A](p.md)",
      "b.md": "# Guide B\n\n[Open from B](q.md)",
    });
    const fromA = files.body("shared.md");
    const fromB = files.body("shared.md");
    files.meta("p.md", () => metadata("shared.md"));
    files.meta("q.md", () => metadata("shared.md"));
    openReader();
    await openLink("Open guide B");
    await screen.findByRole("heading", { name: "Guide B" });
    fireEvent.click(tab("a.md"));
    await openLink("Open from A");
    await waitFor(() => expect(files.fileRequests("shared.md")).toHaveLength(1));
    fireEvent.click(tab("b.md"));
    await openLink("Open from B");
    await waitFor(() => expect(files.fileRequests("shared.md")).toHaveLength(2));
    await act(async () => fromB.resolve(new Response(null, { status: 404 })));
    await act(async () => fromA.resolve(new Response(null, { status: 403 })));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.getByRole("heading", { name: "Guide B" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't open q.md. File not found.");
    expect(screen.getByRole("button", { name: "Edit path for q.md" })).toBeVisible();
    fireEvent.click(tab("a.md"));
    expect(screen.getByRole("heading", { name: "Guide A" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    fireEvent.click(tab("b.md"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("corrects a failed relative path using the failing request's parent directory", async () => {
    const directory = { path: "docs", path_id: "docs-id" };
    const files = documentRead({
      "docs/a.md": "# Original guide\n\n[Open missing plan](missing.md)",
      "docs/fixed.md": "# Corrected body",
    });
    files.meta("a.md", Promise.resolve(metadata("docs/a.md", root, undefined, undefined, directory)));
    files.meta("missing.md", Promise.resolve(new Response(null, { status: 404 })));
    files.meta("fixed.md", Promise.resolve(metadata("docs/fixed.md", root, undefined, undefined, directory)));
    const corrected = files.body("docs/fixed.md");
    openReader();
    await openLink("Open missing plan");
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't open missing.md. File not found.");
    fireEvent.click(screen.getByRole("button", { name: "Edit path for missing.md" }));
    const field = screen.getByRole("textbox", { name: "File path" });
    expect(field).toHaveValue("missing.md");
    fireEvent.change(field, { target: { value: "fixed.md" } });
    fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    await waitFor(() => expect(files.fileRequests("fixed.md")).toHaveLength(1));
    expect(screen.getByRole("heading", { name: "Original guide" })).toBeVisible();
    const binding = new URL(String(files.fileRequests("fixed.md")[0][0])).searchParams;
    expect(binding.get("source")).toBe(root);
    expect(binding.get("source_directory")).toBe(directory.path);
    expect(binding.get("source_directory_id")).toBe(directory.path_id);
    await act(async () => corrected.resolve(new Response("# Corrected body")));
    expect(await screen.findByRole("heading", { name: "Corrected body" })).toBeVisible();
  });

  it.each([false, true])("keeps the selected document's position when an earlier alias confirms (local jump: %s)", async (jump) => {
    const files = documentRead({
      "a.md": "# A\n\n[Read B](b.md)\n\n[Alias of B](alias.md#L3)",
      "b.md": "# B\n\nOld line\n\n[Local reading position](#local)\n\n## Local",
    });
    const alias = deferred<Response>();
    files.meta("alias.md", alias.promise);
    openReader();
    await openLink("Read B");
    await screen.findByRole("link", { name: "Local reading position" });
    fireEvent.click(tab("a.md"));
    await openLink("Alias of B");
    fireEvent.click(tab("b.md"));
    if (jump) fireEvent.click(screen.getByRole("link", { name: "Local reading position" }));
    await act(async () => alias.resolve(metadata("b.md")));
    expect(selected()).toBe(tab("b.md"));
    expect(screen.getByRole("button", { name: "Reading", pressed: true })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Local" })).toBeVisible();
    expect(screen.queryByText("Old line", { selector: "[data-line]" })).not.toBeInTheDocument();
  });

  it.each(["fails", "merges"] as const)("returns focus to the active tab when a candidate %s", async (outcome) => {
    const files = documentRead({ "a.md": "# A\n\n[Read alias](alias.md)" });
    const alias = deferred<Response>();
    files.meta("alias.md", alias.promise);
    openReader();
    await openLink("Read alias");
    const cancelOpening = screen.getByRole("button", { name: "Cancel opening alias.md" });
    act(() => cancelOpening.focus());
    expect(cancelOpening).toHaveFocus();
    await act(async () => alias.resolve(outcome === "fails" ? new Response(null, { status: 404 }) : metadata("a.md")));
    expect(cancelOpening).not.toBeInTheDocument();
    expect(tab("a.md")).toHaveFocus();
  });

  it("keeps the original text active through image transfer and decoding, including a later text action", async () => {
    const files = imageRead({
      "a.md": "# Original document\n\n[View diagram](diagram.png)\n\n[Read notes](notes.md)",
      "notes.md": "# Notes",
    });
    const receiveImage = files.imageBody();
    openReader();
    await openLink("View diagram");
    await waitFor(() => expect(files.fileRequests("diagram.png")).toHaveLength(2));
    expect(screen.getByRole("heading", { name: "Original document" })).toBeVisible();
    await receiveImage();
    const image = screen.getByAltText("diagram.png");
    const decoded = await startImageDecode(image);
    expect(selected()).toBe(tab("a.md"));
    await openLink("Read notes");
    await screen.findByRole("heading", { name: "Notes" });
    await act(async () => decoded.resolve());
    expect(selected()).toBe(tab("notes.md"));
    fireEvent.click(tab("diagram.png"));
    expect(screen.getByRole("img", { name: "diagram.png" })).toBe(image);
    closeTab("diagram.png");
    expect(image).not.toBeInTheDocument();
    expect(files.revoke).toHaveBeenCalledExactlyOnceWith("blob:diagram-1");
  });

  it("aborts a cancelled image read and replaces a slow decode without showing its late result", async () => {
    const files = imageRead({ "a.md": "# Original document\n\n[View diagram](diagram.png)" });
    const receiveCancelled = files.imageBody();
    const receiveRetried = files.imageBody();
    const receiveCurrent = files.imageBody();
    openReader();
    await screen.findByRole("heading", { name: "Original document" });
    await openLink("View diagram");
    await waitFor(() => expect(files.fileRequests("diagram.png")).toHaveLength(2));
    cancel("diagram.png");
    expect(files.fileRequests("diagram.png")[1][1]?.signal?.aborted).toBe(true);
    await receiveCancelled();
    expect(screen.queryByAltText("diagram.png")).not.toBeInTheDocument();

    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: View diagram" })));
      await receiveRetried();
      const retired = screen.getByAltText("diagram.png");
      const staleDecode = await startImageDecode(retired);
      await act(async () => vi.advanceTimersByTime(5000));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening diagram.png" })));
      expect(retired).not.toBeInTheDocument();
      expect(files.revoke).toHaveBeenCalledExactlyOnceWith("blob:diagram-1");
      await act(async () => staleDecode.reject(new Error("Late decode failure")));
      expect(screen.getByRole("heading", { name: "Original document" })).toBeVisible();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      await receiveCurrent();
      const current = screen.getByAltText("diagram.png");
      const decoded = await startImageDecode(current);
      await act(async () => decoded.resolve());
      expect(selected()).toBe(tab("diagram.png"));
      expect(screen.getByRole("img", { name: "diagram.png" })).toBe(current);
      fireEvent.click(screen.getByRole("button", { name: "Close" }));
      expect(current).not.toBeInTheDocument();
      expect(files.revoke.mock.calls).toEqual([["blob:diagram-1"], ["blob:diagram-2"]]);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["success", "decode failure"] as const)("settles a background image reload only in its own tab (%s)", async (outcome) => {
    const files = imageRead({ "a.md": "# Original document\n\n[View diagram](diagram.png)" });
    const receiveInitial = files.imageBody();
    openReader();
    const original = await screen.findByRole("heading", { name: "Original document" });
    const originalScroll = original.closest<HTMLElement>(".file-viewer-body")!;
    originalScroll.scrollTop = 95;
    await openLink("View diagram");
    await waitFor(() => expect(files.fileRequests("diagram.png")).toHaveLength(2));
    await receiveInitial();
    const oldImage = screen.getByAltText("diagram.png");
    const initialDecode = await startImageDecode(oldImage);
    await act(async () => initialDecode.resolve());
    expect(selected()).toBe(tab("diagram.png"));

    const receiveReload = files.imageBody();
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await waitFor(() => expect(files.fileRequests("diagram.png")).toHaveLength(4));
    await receiveReload();
    const [, replacement] = screen.getAllByAltText("diagram.png");
    const decoded = await startImageDecode(replacement);
    expect(screen.getByRole("img", { name: "diagram.png" })).toBe(oldImage);
    expect(files.revoke).not.toHaveBeenCalled();
    fireEvent.click(tab("a.md"));
    await act(async () => {
      if (outcome === "success") decoded.resolve();
      else decoded.reject(new Error("Invalid image"));
    });
    expect(selected()).toBe(tab("a.md"));
    expect(original).toBeVisible();
    expect(originalScroll.scrollTop).toBe(95);
    expect(oldImage).not.toBeInTheDocument();
    expect(files.revoke).toHaveBeenCalledWith("blob:diagram-1");

    fireEvent.click(tab("diagram.png"));
    if (outcome === "success") {
      expect(screen.getByRole("img", { name: "diagram.png" })).toBe(replacement);
      expect(files.revoke).toHaveBeenCalledTimes(1);
    } else {
      expect(replacement).not.toBeInTheDocument();
      expect(files.revoke).toHaveBeenCalledWith("blob:diagram-2");
      expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load the preview.");
      expect(screen.getByRole("button", { name: "Retry" })).toBeVisible();
    }
  });

  it("retries slow PDF bytes while the original text remains readable", async () => {
    const files = documentRead({ "a.md": "# Original document\n\n[Read PDF](paper.pdf)" });
    const pdf = deferred<Response>();
    files.meta("paper.pdf", pdf.promise);
    files.body("paper.pdf");
    files.body("paper.pdf");
    openReader();
    const link = await screen.findByRole("button", { name: "Open file: Read PDF" });
    const pdfMeta = () => metadata("paper.pdf", root, undefined, undefined, undefined, "pdf");
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(link));
      await act(async () => pdf.resolve(pdfMeta()));
      const firstFrame = document.querySelector("iframe");
      expect(firstFrame?.closest("[inert]")).not.toBeNull();
      await act(async () => vi.advanceTimersByTime(5000));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry opening paper.pdf" })));
      const requests = files.fileRequests("paper.pdf");
      expect(requests).toHaveLength(3);
      expect(requests[1][1]?.signal?.aborted).toBe(true);
      expect(firstFrame).not.toBeInTheDocument();
      expect(document.querySelector("iframe")).not.toBeNull();
      expect(screen.getByRole("heading", { name: "Original document" })).toBeVisible();
      expect(selected()).toBe(tab("a.md"));
    } finally {
      vi.useRealTimers();
    }
  });

  it("retries a slow PDF reload from another tab without changing its pending opening", async () => {
    const files = documentRead({ "a.md": "# Original guide\n\n[Read PDF](paper.pdf)\n\n[Read later](later.md)" });
    const pdfMeta = () => metadata("paper.pdf", root, undefined, undefined, undefined, "pdf");
    files.meta("paper.pdf", pdfMeta);
    openReader();
    await openLink("Read PDF");
    const pdf = await screen.findByRole("tab", { name: "paper.pdf", selected: true });
    const staleBody = files.body("paper.pdf");
    const later = files.body("later.md");
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Reload file" })));
      fireEvent.click(tab("a.md"));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Read later" })));
      await act(async () => vi.advanceTimersByTime(5000));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry reloading paper.pdf" })));
      expect(files.fileRequests("paper.pdf")[3][1]?.signal?.aborted).toBe(true);
      await act(async () => staleBody.resolve(new Response("retired PDF")));
      expect(files.fileRequests("paper.pdf")).toHaveLength(5);
      expect(screen.getByRole("heading", { name: "Original guide" })).toBeVisible();
      expect(selected()).toBe(tab("a.md"));
      await act(async () => later.resolve(new Response("# Later text")));
      expect(selected()).toBe(tab("later.md"));
      fireEvent.click(pdf);
      expect(screen.getByTitle("PDF preview")).not.toHaveAttribute("inert");
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["draws", "fails"] as const)("only commits the latest PDF reload after first-page drawing (%s)", async (result) => {
    const files = documentRead({ "a.md": "# Original guide\n\n[Read PDF](paper.pdf)" });
    files.meta("paper.pdf", () => metadata("paper.pdf", root, undefined, undefined, undefined, "pdf"));
    const initialBytes = files.body("paper.pdf");
    const staleBytes = files.body("paper.pdf");
    const currentBytes = files.body("paper.pdf");
    const firstPaint = deferred<void>();
    const stalePaint = deferred<void>();
    const currentPaint = deferred<void>();
    const cancelStale = vi.fn();
    pdfRenderer.paintPdfPage
      .mockImplementationOnce(() => ({ promise: firstPaint.promise, cancel: () => {} }))
      .mockImplementationOnce(() => ({ promise: stalePaint.promise, cancel: cancelStale }))
      .mockImplementationOnce(() => ({ promise: currentPaint.promise, cancel: () => {} }));
    openReader();
    const guide = await screen.findByRole("heading", { name: "Original guide" });
    const scroll = guide.closest<HTMLElement>(".file-viewer-body")!;
    scroll.scrollTop = 100;
    await openLink("Read PDF");
    await act(async () => initialBytes.resolve(new Response("First PDF")));
    await waitFor(() => expect(pdfRenderer.paintPdfPage).toHaveBeenCalledTimes(1));
    const initialFrame = screen.getByTitle("PDF preview");
    expect(initialFrame).toHaveAttribute("inert");
    expect(selected()).toBe(tab("a.md"));
    fireEvent.click(tab("a.md"));
    await act(async () => firstPaint.resolve());
    expect(selected()).toBe(tab("a.md"));
    fireEvent.click(tab("paper.pdf"));
    expect(screen.getByTitle("PDF preview")).toBe(initialFrame);

    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await act(async () => staleBytes.resolve(new Response("Former reload")));
    await waitFor(() => expect(pdfRenderer.paintPdfPage).toHaveBeenCalledTimes(2));
    const [, staleFrame] = screen.getAllByTitle("PDF preview");
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    await act(async () => currentBytes.resolve(new Response("Latest reload")));
    await waitFor(() => expect(pdfRenderer.paintPdfPage).toHaveBeenCalledTimes(3));
    const [, currentFrame] = screen.getAllByTitle("PDF preview");
    expect(initialFrame).toBeInTheDocument();
    expect(staleFrame).not.toBeInTheDocument();
    expect(cancelStale).toHaveBeenCalledOnce();
    await act(async () => stalePaint.resolve());
    expect(initialFrame).toBeInTheDocument();
    fireEvent.click(tab("a.md"));
    await act(async () => {
      if (result === "draws") currentPaint.resolve();
      else currentPaint.reject(new Error("Invalid PDF"));
    });
    expect(selected()).toBe(tab("a.md"));
    expect(guide).toBeVisible();
    expect(scroll.scrollTop).toBe(100);
    expect(initialFrame).not.toBeInTheDocument();
    fireEvent.click(tab("paper.pdf"));
    if (result === "draws") {
      expect(screen.getByTitle("PDF preview")).toBe(currentFrame);
      expect(currentFrame).not.toHaveAttribute("inert");
    } else {
      expect(currentFrame).not.toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent("Couldn't draw the first PDF page.");
      expect(screen.getByRole("button", { name: "Retry" })).toBeVisible();
    }
  });

  it("keeps identical canonical paths from distinct sources in separate tabs", async () => {
    const files = documentRead({ "a.md": "# Root copy", "chosen.md": "# Nested source copy" });
    files.meta("a.md", Promise.resolve(metadata("a.md", root, `${root}/shared/a.md`)));
    files.meta("picked.md", Promise.resolve(metadata("chosen.md", `${root}/shared`, `${root}/shared/a.md`)));
    openReader();
    await screen.findByRole("heading", { name: "Root copy" });
    fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
    fireEvent.change(await screen.findByRole("combobox", { name: "Source" }), { target: { value: `${root}/shared` } });
    fireEvent.click(await screen.findByRole("button", { name: "picked.md" }));
    expect(await screen.findByRole("heading", { name: "Nested source copy" })).toBeVisible();
    expect(screen.getAllByRole("tab", { name: "a.md" })).toHaveLength(2);
    expect(new URL(String(files.fileRequests("chosen.md")[0][0])).searchParams.get("source")).toBe(`${root}/shared`);
  });

  it("counts pending tabs toward capacity and still lets users reuse a confirmed file in place", async () => {
    const names = ["b", "c", "d", "e", "f", "g", "h", "i"];
    const links = names.map((name) => `[Read ${name}](${name}.md)`);
    const files = documentRead({ "a.md": [...links, "[Read B alias](b-alias.md)"].join("\n\n") });
    files.meta("b-alias.md", Promise.resolve(metadata("b.md")));
    files.body("h.md");
    openReader();
    for (const name of names.slice(0, 6)) {
      await openLink(name === "b" ? "Read B alias" : `Read ${name}`);
      await screen.findByRole("heading", { name: `${name}.md body` });
      fireEvent.click(tab("a.md"));
    }
    await openLink("Read h");
    await waitFor(() => expect(files.fileRequests("h.md")).toHaveLength(2));
    await openLink("Read h");
    expect(screen.getByText("Close a file tab or cancel an opening to make room.")).toBeVisible();
    expect(files.fileRequests("h.md")).toHaveLength(2);
    await openLink("Read i");
    expect(screen.getByText("Close a file tab or cancel an opening to make room.")).toBeVisible();
    expect(files.fileRequests("i.md")).toHaveLength(0);
    await openLink("Read B alias");
    expect(selected()).toBe(tab("a.md"));
    expect(files.fileRequests("b-alias.md")).toHaveLength(1);
    await openLink("Read b");
    await waitFor(() => expect(selected()).toBe(tab("b.md")));
    expect(files.fileRequests("b.md")).toHaveLength(2);
    expect(within(screen.getByRole("tablist")).getAllByRole("tab").map((entry) => entry.textContent)).toEqual(
      ["a.md", ...names.slice(0, 6).map((name) => `${name}.md`)],
    );
    expect(screen.getByRole("button", { name: "Cancel opening h.md" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Open file picker" }));
    fireEvent.click(within(await screen.findByRole("list")).getByRole("button", { name: "a.md" }));
    await waitFor(() => expect(selected()).toBe(tab("a.md")));
    expect(files.fileRequests("a.md")).toHaveLength(3);
    expect(new URL(String(files.fileRequests("a.md")[2][0])).pathname).toMatch(/\/meta$/);
    expect(screen.getByRole("button", { name: "Cancel opening h.md" })).toBeVisible();
    files.meta("b.md", () => Response.json({ detail: "The file path changed.", code: "target_changed" }, { status: 409 }));
    await openLink("Read b");
    expect(await screen.findByRole("alert")).toHaveTextContent("The target file changed.");
    files.meta("b.md", () => metadata("b.md"));
    fireEvent.click(screen.getByRole("button", { name: "Retry opening b.md" }));
    await waitFor(() => expect(selected()).toBe(tab("b.md")));
    const bRequests = files.fileRequests("b.md");
    expect(bRequests.filter(([input]) => new URL(String(input)).pathname.endsWith("/files"))).toHaveLength(1);
    expect(new URL(String(bRequests.at(-1)![0])).searchParams.get("reload_source")).toBeNull();
    fireEvent.click(tab("a.md"));
    files.meta("b.md", () => metadata("redirected.md"));
    await openLink("Read b");
    expect(await screen.findByText("Close a file tab or cancel an opening to make room.")).toBeVisible();
    expect(files.fileRequests("redirected.md")).toHaveLength(0);
    const confirmed = deferred<Response>();
    files.meta("b.md", confirmed.promise);
    await openLink("Read b");
    await screen.findByRole("button", { name: "Cancel confirmation" });
    cancel("h.md");
    await act(async () => confirmed.resolve(metadata("redirected.md")));
    expect(await screen.findByRole("heading", { name: "redirected.md body" })).toBeVisible();
    expect(files.fileRequests("redirected.md")).toHaveLength(1);
    expect(screen.getAllByRole("tab")).toHaveLength(8);
  }, 15_000);

  it.each(["close", "leave", "switch", "account", "delete"] as const)("retires the text response when users %s the originating context", async (exit) => {
    const files = documentRead({ "a.md": "# A\n\n[Read B](b.md)" });
    const b = files.body("b.md");
    openReader();
    await openLink("Read B");
    await waitFor(() => expect(files.fileRequests("b.md")).toHaveLength(2));
    const oldToken = localStorage.getItem("octopus_token");
    try {
      act(() => {
        const store = useSessionStore.getState();
        switch (exit) {
          case "close": store.closeViewer(); break;
          case "leave": store.openManage("account"); break;
          case "switch": store.setActiveSessionId("second"); break;
          case "account": store.setToken("tok-new"); break;
          case "delete": store.forgetSession("original"); break;
        }
      });
      await act(async () => b.resolve(new Response("# Retired response")));
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(files.fileRequests("b.md")[1][1]?.signal?.aborted).toBe(true);
    } finally {
      if (oldToken === null) localStorage.removeItem("octopus_token");
      else localStorage.setItem("octopus_token", oldToken);
    }
  });
});

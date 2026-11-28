/**
 * Renderer-dispatch tests for FileViewerDialog.
 *
 * The dialog hits two endpoints — /meta returns the `kind` (which
 * picks the renderer), then /files returns the bytes. We mock
 * window.fetch so each test can stub a specific kind without
 * needing a real backend. The relationship between extension and
 * kind is enforced server-side and covered by tests/test_file_viewer.py;
 * here we only verify the frontend acts on kind correctly.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { FileViewerDialog } from "./FileViewerDialog";
import { MessageBubble } from "./MessageBubble";
import { useSessionStore } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";

interface MetaPayload {
  kind: "markdown" | "code" | "text" | "image" | "pdf";
  mime_type: string;
  size: number;
}

function mockFetch(meta: MetaPayload, body: string) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    // Keep the requested path in the response so CodeBody can infer
    // the language hint from the extension (e.g. main.py → python).
    const reqPath = new URL(url).searchParams.get("path") ?? "test";
    if (url.includes("/files/meta")) {
      return new Response(JSON.stringify({
        source: "/workspace", absolute_path: `/workspace/${reqPath}`, path: reqPath,
        canonical_path_id: "file-path", canonical_directory: { path: ".", path_id: "root-path" }, ...meta,
      }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response(body, {
      status: 200,
      headers: { "content-type": meta.mime_type },
    });
  });
}

beforeEach(() => {
  useSessionStore.setState({ token: "tok", viewer: null, activeSessionId: null });
});

afterEach(() => {
  cleanup();
  useSessionStore.setState({ viewer: null });
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, "pdfViewerEnabled");
});

describe("FileViewerDialog", () => {
  it("keeps the original document visible until the linked text is ready", async () => {
    const target = deferred<Response>();
    const read = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      const path = url.searchParams.get("path");
      if (url.pathname.endsWith("/meta")) return Response.json({
        source: "/workspace", absolute_path: `/workspace/${path}`, path, canonical_path_id: path,
        canonical_directory: { path: ".", path_id: "root-path" }, kind: "markdown", size: 40,
      });
      return path === "a.md" ? new Response("# Original document\n\n[Read target](b.md)") : target.promise;
    });
    vi.stubGlobal("fetch", read);
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "a.md"));
    fireEvent.click(await screen.findByRole("button", { name: "Open file: Read target" }));
    expect(screen.getByRole("heading", { name: "Original document" })).toBeVisible();
    expect(screen.getByRole("tab", { name: "a.md", selected: true })).toBeVisible();
    expect(screen.getByRole("button", { name: "Cancel opening b.md" })).toBeVisible();
    await act(async () => target.resolve(new Response("# Target document")));
    expect(await screen.findByRole("heading", { name: "Target document" })).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "a.md" }));
    expect(screen.getByRole("heading", { name: "Original document" })).toBeVisible();
    expect(read).toHaveBeenCalledTimes(4);
  });

  it("opens a positioned local file URI through the original session's file API", async () => {
    const source = "/workspace";
    const target = "/workspace/中文 %20#draft.md";
    const read = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      const initial = url.searchParams.get("path") === "initial.md";
      if (url.pathname.endsWith("/meta")) return Response.json({
        kind: "markdown", mime_type: "text/plain", size: 64, source,
        absolute_path: initial ? "/workspace/initial.md" : target,
        path: initial ? "initial.md" : "中文 %20#draft.md",
        canonical_path_id: initial ? "initial-id" : "target-id", canonical_directory: { path: ".", path_id: "root-path" },
      });
      return new Response(initial ? "[Local draft](file://localhost/workspace/中文%20%2520%23draft.md#L2)"
        : "# Destination\nLocated line");
    });
    vi.stubGlobal("fetch", read);
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "initial.md"));
    fireEvent.click(await screen.findByRole("button", { name: /Local draft/ }));
    expect(await screen.findByText("Located line")).toHaveAttribute("data-located", "true");
    const request = new URL(String(read.mock.calls[2][0]));
    expect(request.pathname).toBe("/api/sessions/s1/files/meta");
    expect(request.searchParams.get("path")).toBe(target);
    expect(request.searchParams.get("source")).toBe(source);
    expect(request.searchParams.has("source_directory")).toBe(false);
  });

  it("remembers the text size for each account and legacy mode without requesting the file again", async () => {
    const read = mockFetch({ kind: "markdown", mime_type: "text/plain", size: 14 }, "# Test reading");
    vi.stubGlobal("fetch", read);
    render(<FileViewerDialog />);
    const openAs = async (user_id: string | null, label: string) => {
      act(() => {
        const store = useSessionStore.getState();
        store.setIdentity({ user_id, label, is_admin: false });
        store.openViewer("s1", "notes.md");
      });
      await screen.findByRole("heading", { name: "Test reading" });
      return screen.getByRole("combobox", { name: "Text size" });
    };
    const close = () => fireEvent.click(screen.getByRole("button", { name: "Close" }));
    try {
      const first = await openAs("reader-a", "Reader");
      expect(first).toHaveDisplayValue("Standard");
      const requests = read.mock.calls.length;
      fireEvent.change(first, { target: { value: "larger" } });
      expect(first).toHaveDisplayValue("Largest");
      expect(read).toHaveBeenCalledTimes(requests);
      close();
      const second = await openAs("reader-b", "Reader");
      expect(second).toHaveDisplayValue("Standard");
      fireEvent.change(second, { target: { value: "compact" } });
      close();
      const legacy = await openAs(null, "Legacy");
      expect(legacy).toHaveDisplayValue("Standard");
      fireEvent.change(legacy, { target: { value: "large" } });
      close();
      expect(await openAs("reader-a", "Renamed reader")).toHaveDisplayValue("Largest");
      close();
      expect(await openAs(null, "Legacy")).toHaveDisplayValue("Large");
      close();
      localStorage.setItem("octopus_file_reader_size:account:reader-b", "invalid");
      expect(await openAs("reader-b", "Reader")).toHaveDisplayValue("Standard");
    } finally {
      localStorage.removeItem("octopus_file_reader_size:account:reader-a");
      localStorage.removeItem("octopus_file_reader_size:account:reader-b");
      localStorage.removeItem("octopus_file_reader_size:legacy");
      act(() => useSessionStore.getState().setIdentity(null));
    }
  });

  it("locates a Markdown source line and switches to reading the same response", async () => {
    const fetchFile = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/showme/resolve")) return Response.json({
        path: "plan.md", target: { path: "plan.md", position: { kind: "lines", start: 3 } },
        metadata: {
          source: "/workspace", absolute_path: "/workspace/plan.md", path: "plan.md",
          canonical_path_id: "file-path", canonical_directory: { path: ".", path_id: "root-path" },
          kind: "markdown", size: 50,
        },
      });
      return new Response("# Plan\n\n**Look here**\n");
    });
    vi.stubGlobal("fetch", fetchFile);
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "plan.md:3", { mode: "command" }));

    await screen.findByRole("button", { name: "Source", pressed: true });
    expect(await screen.findByText("**Look here**")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Reading" }));
    expect(screen.getByRole("heading", { name: "Plan" })).toBeVisible();
    expect(fetchFile).toHaveBeenCalledTimes(2);
  });

  it("locates a same-document range beyond the current source section without requesting bytes again", async () => {
    const read = mockFetch({ kind: "markdown", mime_type: "text/plain", size: 4500 }, [
      "# Document",
      "",
      "[Go to range](#L2002-L2003)",
      "[Go by relative filename](.//plan.md#L2002-L2003)",
      "[Go by absolute filename](/workspace/./plan.md#L2002-L2003)",
      "[Missing section](#missing)",
      ...Array<string>(1995).fill(""),
      "Start of range",
      "End of range",
    ].join("\n"));
    vi.stubGlobal("fetch", read);
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "plan.md"));
    fireEvent.click(await screen.findByRole("link", { name: "Missing section" }));
    expect(screen.getByRole("note")).toHaveTextContent("The requested heading wasn't found.");

    fireEvent.click(screen.getByRole("link", { name: "Go to range" }));
    expect(screen.getByRole("button", { name: "Source", pressed: true })).toBeVisible();
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    expect(screen.getByText("Start of range")).toHaveAttribute("data-line", "2002");
    expect(screen.getByText("End of range")).toHaveAttribute("data-located", "true");
    fireEvent.click(screen.getByRole("button", { name: "Previous lines" }));
    expect(screen.queryByText("Start of range")).not.toBeInTheDocument();

    for (const label of [/Go by relative filename/, /Go by absolute filename/]) {
      fireEvent.click(screen.getByRole("button", { name: "Reading" }));
      fireEvent.click(screen.getByRole("button", { name: label }));
      expect(read).toHaveBeenCalledTimes(2);
      expect(screen.getByText("Start of range")).toHaveAttribute("data-located", "true");
    }
  });

  it.each(["Retry", "Reload file"])("keeps a plain link's confirmed source when %s restarts stalled bytes", async (action) => {
    const narrow = "/repo/docs";
    const path = `${narrow}/plan.md`;
    const stalled = deferred<Response>();
    const metadataReads: URLSearchParams[] = [];
    let permitted = true;
    const read = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      if (!url.pathname.endsWith("/meta")) return stalled.promise;
      metadataReads.push(url.searchParams);
      if (!permitted && url.searchParams.get("source") === narrow) return new Response(null, { status: 403 });
      const source = permitted ? narrow : "/repo";
      return Response.json({ source, absolute_path: path, path: permitted ? "plan.md" : "docs/plan.md",
        canonical_path_id: source, kind: "markdown", size: 16 });
    });
    vi.stubGlobal("fetch", read);
    render(<><MessageBubble sessionId="s1" message={{ role: "assistant", type: "text", content: `[Plan](${path})` }} />
      <FileViewerDialog /></>);
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open file: Plan" })));
      expect(metadataReads[0].get("source")).toBeNull();
      permitted = false;
      await act(async () => vi.advanceTimersByTime(5000));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: action })));
      expect(metadataReads[1].get("path")).toBe(path);
      expect(metadataReads[1].get("source")).toBe(narrow);
      expect(screen.getByRole("alert")).toHaveTextContent("You don't have access to this file.");
      await act(async () => stalled.resolve(new Response("# Retired plan")));
      expect(screen.queryByText("Retired plan")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["a local jump", "clearing the position"])("re-resolves the original command after a slow read retry and %s", async (change) => {
    const clearPosition = change === "clearing the position";
    const command = clearPosition ? "alias.md#L3" : "alias.md#start";
    let target = "original.md";
    let bodyReads = 0;
    const firstBody = deferred<Response>();
    const metadata = (file: string) => ({
      source: "/workspace", absolute_path: `/workspace/${file}`, path: file,
      canonical_path_id: file, canonical_directory: { path: ".", path_id: "root-path" },
      kind: "markdown", size: 80,
    });
    const fetchFile = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (new URL(String(input)).pathname.endsWith("/showme/resolve")) {
        expect(JSON.parse(String(init?.body))).toEqual({ text: command });
        return Response.json({
          target: { path: "alias.md", position: clearPosition ? { kind: "lines", start: 3 } : { kind: "heading", slug: "start" } },
          metadata: metadata(target),
        });
      }
      const path = new URL(String(input)).searchParams.get("path");
      if (String(input).includes("/meta")) {
        expect(path).toBe("/workspace/original.md");
        return Response.json(metadata("original.md"));
      }
      if (++bodyReads === 1) return firstBody.promise;
      return new Response(`# Start\n\n[Jump](#L5)\n\nReading ${path}`);
    });
    vi.stubGlobal("fetch", fetchFile);
    render(<FileViewerDialog />);
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await act(async () => useSessionStore.getState().openViewer("s1", command, { mode: "command" }));
      expect(bodyReads).toBe(1);
      await act(async () => vi.advanceTimersByTime(5000));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry" })));
      if (clearPosition) expect(screen.getByRole("button", { name: "Source", pressed: true })).toBeVisible();
      else expect(screen.getByRole("heading", { name: "Start" })).toBeVisible();
      await act(async () => firstBody.resolve(new Response("# Retired body")));
      expect(screen.queryByText("Retired body")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
    if (clearPosition) {
      fireEvent.click(screen.getByRole("button", { name: "Edit path" }));
      const position = screen.getByRole("textbox", { name: "Position (optional)" });
      expect(position).toHaveValue(":3");
      fireEvent.change(position, { target: { value: "" } });
      fireEvent.click(screen.getByRole("button", { name: "Open file" }));
      await screen.findByRole("button", { name: "Reading", pressed: true });
    } else {
      fireEvent.click(await screen.findByRole("link", { name: "Jump" }));
      expect(screen.getByText("Reading original.md")).toHaveAttribute("data-located", "true");
    }
    target = "replacement.md";
    fireEvent.click(screen.getByRole("button", { name: "Reload file" }));
    const replacement = await screen.findByText("Reading replacement.md");
    if (clearPosition) expect(screen.getByRole("button", { name: "Reading", pressed: true })).toBeVisible();
    else expect(replacement).toHaveAttribute("data-located", "true");
  });

  it("locates an explicitly linked footnote in the loaded document", async () => {
    const read = mockFetch({ kind: "markdown", mime_type: "text/plain", size: 120 }, [
      "[Open note](./plan.md#user-content-fn-%E4%B8%AD%E6%96%87)",
      "[Open note lowercase](./plan.md#user-content-fn-%e4%b8%ad%e6%96%87)",
      "[Open note mixed](./plan.md#user-content-fn-%E4%b8%AD%e6%96%87)",
      "[Open plain label](./plan.md#user-content-fn-a)",
      "[Open escaped label](./plan.md#user-content-fn-%2561)",
      "[Open escaped Chinese label](./plan.md#user-content-fn-%25e4%25b8%25ad%25e6%2596%2587)",
      "[Open legacy escape](./plan.md#user-content-fn-%63)",
      "[Open ambiguous escape](./plan.md#user-content-fn-%78y)",
      "Citations [^%61], [^a], [^%e4%b8%ad%e6%96%87], [^中文], [^%63], [^%78%79], and [^x%79]",
      "[^%61]: The escaped label.",
      "[^a]: The plain label.",
      "[^%e4%b8%ad%e6%96%87]: The escaped Chinese label.",
      "[^中文]: The footnote text.",
      "[^%63]: The only legacy match.",
      "[^%78%79]: One ambiguous match.",
      "[^x%79]: Another ambiguous match.",
    ].join("\n\n"));
    vi.stubGlobal("fetch", read);
    const scroll = vi.fn();
    const originalScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { value: scroll, configurable: true });
    try {
      render(<FileViewerDialog />);
      act(() => useSessionStore.getState().openViewer("s1", "plan.md"));
      for (const [label, note] of [
        [/Open plain label/, "The plain label."],
        [/Open escaped label/, "The escaped label."],
        [/Open escaped Chinese label/, "The escaped Chinese label."],
        [/Open note at/, "The footnote text."],
        [/Open note mixed at/, "The footnote text."],
        [/Open note lowercase at/, "The footnote text."],
        [/Open legacy escape/, "The only legacy match."],
      ] as const) {
        fireEvent.click(await screen.findByRole("button", { name: label }));
        expect(scroll.mock.contexts.at(-1)).toBe(screen.getByText(note, { exact: false }).closest("li"));
      }
      scroll.mockClear();
      fireEvent.click(screen.getByRole("button", { name: /Open ambiguous escape/ }));
      expect(screen.getByRole("note")).toHaveTextContent("The requested heading wasn't found.");
      expect(scroll).not.toHaveBeenCalled();
      expect(read).toHaveBeenCalledTimes(2);
    } finally {
      if (originalScroll) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", originalScroll);
      else Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
    }
  });

  it("clears a missed heading after following a generated footnote", async () => {
    const read = mockFetch({ kind: "markdown", mime_type: "text/plain", size: 85 }, [
      "[Missing section](#missing)", "Citation [^a]", "[^a]: Footnote.",
    ].join("\n\n"));
    vi.stubGlobal("fetch", read);
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "plan.md"));
    fireEvent.click(await screen.findByRole("link", { name: "Missing section" }));
    expect(screen.getByRole("note")).toHaveTextContent("The requested heading wasn't found.");
    fireEvent.click(screen.getByRole("link", { name: /^1$/ }));
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    expect(read).toHaveBeenCalledTimes(2);
  });

  it("keeps the failed target editable and reopens a copied absolute path literally", async () => {
    const path = "/workspace/中文 %20#计划␛r\\r\r.md";
    const displayedPath = "/workspace/中文 %20#计划␛␛r\\r␛r.md";
    const copy = vi.fn(async () => {});
    vi.stubGlobal("navigator", Object.assign(Object.create(navigator), { clipboard: { writeText: copy } }));
    const fileFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/showme/resolve")) return Response.json({ detail: "File not found." }, { status: 404 });
      if (!url.pathname.endsWith("/meta")) return new Response("# Corrected");
      expect(url.searchParams.get("path")).toBe(path);
      return Response.json({
        source: "/workspace", absolute_path: path, path: path.split("/").at(-1), canonical_path_id: "checked-path",
        canonical_directory: { path: ".", path_id: "checked-dir" }, kind: "markdown", size: 12,
      });
    });
    vi.stubGlobal("fetch", fileFetch);
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "missing 中文.md", { mode: "command" }));
    await screen.findByText(/file not found/i);
    fireEvent.click(screen.getByRole("button", { name: "Edit path" }));
    const field = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "File path" });
    expect(field).toHaveValue("missing 中文.md");
    field.select();
    fireEvent.paste(field, { clipboardData: { getData: () => path } });
    expect(field).toHaveValue(displayedPath);
    fireEvent.change(field, { target: { value: displayedPath + "x" } });
    fireEvent.change(field, { target: { value: displayedPath } });
    fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    await screen.findByRole("heading", { name: "Corrected" });
    fireEvent.click(screen.getByRole("button", { name: "Copy path" }));
    await waitFor(() => expect(copy).toHaveBeenCalledWith(path));
    fireEvent.click(screen.getByRole("button", { name: "Edit path" }));
    expect(screen.getByRole("textbox", { name: "File path" })).toHaveValue(displayedPath);
    fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    await screen.findByRole("heading", { name: "Corrected" });
    expect(fileFetch.mock.calls.filter(([url]) => new URL(String(url)).pathname.endsWith("/meta"))).toHaveLength(2);
  });

  it.each([200, 403])("retires bytes (HTTP %s) opened from another session when leaving and returning to the foreground", async (status) => {
    const response = deferred<Response>();
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      expect(url.pathname).toMatch(/^\/api\/sessions\/old\/files/);
      if (url.pathname.endsWith("/meta")) return Response.json({
        source: "/workspace", absolute_path: "/workspace/old.md", path: "old.md",
        canonical_path_id: "old-path", canonical_directory: { path: ".", path_id: "root-path" }, kind: "markdown", size: 20,
      });
      return response.promise;
    });
    vi.stubGlobal("fetch", fetch);
    useSessionStore.setState({ activeSessionId: "foreground", mainView: "chat" });
    render(<><MessageBubble sessionId="old" message={{ role: "assistant", type: "text", content: "[Old report](old.md)" }} />
      <FileViewerDialog /></>);
    fireEvent.click(screen.getByRole("button", { name: "Open file: Old report" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    act(() => {
      useSessionStore.getState().setActiveSessionId("other");
      useSessionStore.getState().setActiveSessionId("foreground");
    });
    await act(async () => response.resolve(new Response("# Old result", { status })));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("renders nothing while viewer state is null", () => {
    render(<FileViewerDialog />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("renders markdown body for kind=markdown", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "markdown", mime_type: "text/markdown; charset=utf-8", size: 22 },
        "# Hello\n\nWorld."
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "doc.md"));

    // The H1 the markdown renderer produces
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Hello" })).toBeInTheDocument()
    );
  });

  it("loads Markdown and relative images from the canonical document in the source session", async () => {
    const fileFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      if (url.pathname === "/api/sessions/original/files/meta") {
        return Response.json({
          source: "/repo", absolute_path: "/repo/docs/original.md", path: "docs/original.md", canonical_path_id: "original-path",
          canonical_directory: { path: "docs", path_id: "docs-path" }, kind: "markdown", size: 80,
        });
      }
      if (url.pathname === "/api/sessions/original/files" &&
        url.searchParams.get("source") === "/repo" && url.searchParams.get("path") === "docs/original.md" &&
        url.searchParams.get("canonical_path_id") === "original-path") {
        return new Response([
          "# Original",
          "![Diagram](../assets/图%2520%23.png?token=author)",
          "[External](https://example.test/help?q=1#ref)",
          "[Protocol relative](//example.test/help)",
          "[Site root](/help)",
          "![Remote](//example.test/figure.png)",
          "![Site asset](/figure.png)",
        ].join("\n\n"));
      }
      return new Response("# Wrong file");
    });
    vi.stubGlobal("fetch", fileFetch);
    useSessionStore.setState({ activeSessionId: "different" });
    render(<><MessageBubble sessionId="original" message={{ role: "assistant", type: "text", content: "[Old report](alias.md)" }} />
      <FileViewerDialog /></>);
    fireEvent.click(screen.getByRole("button", { name: "Open file: Old report" }));

    await screen.findByRole("heading", { name: "Original" });
    const diagram = new URL((await screen.findByRole("img", { name: "Diagram" })).getAttribute("src")!);
    expect(diagram.pathname).toBe("/api/sessions/original/files");
    expect(diagram.searchParams.get("source")).toBe("/repo");
    expect(diagram.searchParams.get("path")).toBe("../assets/图%20#.png");
    expect(diagram.searchParams.get("source_directory")).toBe("docs");
    expect(diagram.searchParams.get("source_directory_id")).toBe("docs-path");
    expect(diagram.searchParams.getAll("token")).toEqual(["tok"]);
    expect(fileFetch).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({ cache: "no-store" }));
    expect(screen.getByRole("link", { name: "External" })).toHaveAttribute("href", "https://example.test/help?q=1#ref");
    expect(screen.getByRole("link", { name: "Protocol relative" })).toHaveAttribute("href", "//example.test/help");
    expect(screen.getByRole("link", { name: "Site root" })).toHaveAttribute("href", "/help");
    expect(screen.getByRole("img", { name: "Remote" })).toHaveAttribute("src", "//example.test/figure.png");
    expect(screen.getByRole("img", { name: "Site asset" })).toHaveAttribute("src", "/figure.png");
  });

  it.each(["close", "change account"] as const)("retires pending link metadata on %s", async (transition) => {
    const metadata = deferred<Response>();
    const fetch = vi.fn(() => metadata.promise);
    vi.stubGlobal("fetch", fetch);
    useSessionStore.setState({ activeSessionId: "foreground", mainView: "chat" });
    render(<><MessageBubble sessionId="archived" message={{ role: "assistant", type: "text", content: "[Old report](report.md)" }} />
      <FileViewerDialog /></>);
    fireEvent.click(screen.getByRole("button", { name: "Open file: Old report" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const originalToken = localStorage.getItem("octopus_token");
    try {
      if (transition === "close") fireEvent.click(screen.getByRole("button", { name: "Close" }));
      else act(() => {
        const state = useSessionStore.getState();
        state.setToken("other-token");
        state.setToken("tok");
        state.setActiveSessionId("foreground");
      });
      await act(async () => metadata.resolve(Response.json({
        source: "/workspace", absolute_path: "/workspace/report.md", path: "report.md",
        canonical_path_id: "file-path", canonical_directory: { path: ".", path_id: "root-path" },
        kind: "markdown", size: 50,
      })));
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(fetch).toHaveBeenCalledTimes(1);
    } finally {
      if (originalToken === null) localStorage.removeItem("octopus_token");
      else localStorage.setItem("octopus_token", originalToken);
    }
  });

  it("returns focus to the foreground composer when the historical trigger unmounts", async () => {
    const metadata = deferred<Response>();
    vi.stubGlobal("fetch", vi.fn(() => metadata.promise));
    useSessionStore.setState({ activeSessionId: "foreground", mainView: "chat" });
    const pane = (showMessage: boolean) => <div data-chat-session="foreground">
      {showMessage && <MessageBubble sessionId="archived" message={{ role: "assistant", type: "text", content: "[Old report](report.md)" }} />}
      <div className="chat-input-bar"><textarea aria-label="Message" /></div>
      <FileViewerDialog />
    </div>;
    const view = render(pane(true));
    fireEvent.click(screen.getByRole("button", { name: "Open file: Old report" }));
    await waitFor(() => expect(screen.getByRole("dialog")).toContainElement(document.activeElement as HTMLElement));
    view.rerender(pane(false));
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus());
  });

  it.each(["reopen", "close", "change account"] as const)(
    "retires a pending fragment when the reader chooses to %s",
    async (transition) => {
      let finishOld!: (response: Response) => void;
      const pending = new Promise<Response>((resolve) => { finishOld = resolve; });
      let targetReads = 0;
      vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input));
        const filename = url.searchParams.get("path")!.replace(/^\.\//, "");
        if (url.pathname.endsWith("/meta")) {
          return Response.json({
            source: "/workspace", absolute_path: `/workspace/${filename}`, path: filename,
            canonical_path_id: filename, canonical_directory: { path: ".", path_id: "root-path" },
            kind: "markdown", size: 60,
          });
        }
        if (filename === "start.md") return new Response("[Follow](next.md#details)");
        if (++targetReads === 1) return pending;
        return new Response("# New reading\n\n## Details");
      }));

      const scroll = vi.fn();
      const originalScroll = Object.getOwnPropertyDescriptor(Element.prototype, "scrollIntoView");
      Object.defineProperty(Element.prototype, "scrollIntoView", { configurable: true, value: scroll });
      const { openViewer, closeViewer, setToken } = useSessionStore.getState();
      const savedToken = localStorage.getItem("octopus_token");
      try {
        render(<FileViewerDialog />);
        act(() => openViewer("s1", "start.md"));
        const follow = await screen.findByRole("button", { name: /Open file: Follow/ });
        act(() => follow.click());
        await waitFor(() => expect(targetReads).toBe(1));

        act(() => {
          if (transition === "change account") setToken("other-token");
          if (transition === "close") closeViewer();
          else openViewer("s1", "next.md");
        });
        if (transition === "close") expect(screen.queryByRole("dialog")).toBeNull();
        else await screen.findByRole("heading", { name: "New reading" });

        await act(async () => finishOld(new Response("# Retired reading\n\n## Details")));
        expect(screen.queryByRole("heading", { name: "Retired reading" })).toBeNull();
        expect(scroll).not.toHaveBeenCalled();
      } finally {
        if (originalScroll) Object.defineProperty(Element.prototype, "scrollIntoView", originalScroll);
        else Reflect.deleteProperty(Element.prototype, "scrollIntoView");
        if (savedToken === null) localStorage.removeItem("octopus_token");
        else localStorage.setItem("octopus_token", savedToken);
      }
    },
  );

  it("renders <img> for kind=image", async () => {
    const create = vi.fn(() => "blob:viewer-image");
    const revoke = vi.fn();
    vi.stubGlobal("URL", class extends URL {
      static createObjectURL = create;
      static revokeObjectURL = revoke;
    });
    const read = mockFetch({ kind: "image", mime_type: "image/png", size: 1024 }, "ignored-binary");
    vi.stubGlobal("fetch", read);
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "shot.png"));

    await waitFor(() => expect(document.querySelector("img")).toHaveAttribute("src", "blob:viewer-image"));
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    const image = document.querySelector("img")!;
    Object.defineProperty(image, "decode", { value: async () => {} });
    fireEvent.load(image);
    expect(await screen.findByRole("img", { name: "shot.png" })).toBeVisible();
    expect(read).toHaveBeenCalledWith(expect.stringContaining("/files?path=shot.png"),
      expect.objectContaining({ headers: { Authorization: "Bearer tok" } }));
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(revoke).toHaveBeenCalledWith("blob:viewer-image");
  });

  it("cancels SVG data preparation when the viewer closes", async () => {
    const reading = vi.spyOn(FileReader.prototype, "readAsDataURL").mockImplementation(() => {});
    const abort = vi.spyOn(FileReader.prototype, "abort").mockImplementation(function (this: FileReader) {
      this.dispatchEvent(new Event("loadend"));
    });
    vi.stubGlobal("fetch", mockFetch({ kind: "image", mime_type: "image/svg+xml", size: 64 }, "<svg/>"));
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "diagram.svg"));
    await waitFor(() => expect(reading).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(abort).toHaveBeenCalledOnce();
    expect(screen.queryByAltText("diagram.svg")).not.toBeInTheDocument();
  });

  it("requests explicit PDF preview and preserves the download URL", async () => {
    const meta = mockFetch({ kind: "pdf", mime_type: "application/pdf", size: 8000 }, "");
    const bytes = deferred<Response>();
    const read = vi.fn((input: RequestInfo | URL, _init?: RequestInit) =>
      new URL(String(input)).pathname.endsWith("/meta") ? meta(input) : bytes.promise);
    vi.stubGlobal("fetch", read);
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "doc.pdf"));

    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    const [input, init] = read.mock.calls[1];
    const preview = new URL(String(input));
    expect(preview.pathname).toBe("/api/sessions/s1/files");
    expect(preview.searchParams.get("preview")).toBe("true");
    expect(preview.searchParams.has("token")).toBe(false);
    expect(init?.headers).toEqual({ Authorization: "Bearer tok" });
    expect(screen.getByTitle("PDF preview")).toHaveAttribute("inert");
    preview.searchParams.set("token", "tok");
    const open = new URL(screen.getByRole("link", { name: "Open in new tab" }).getAttribute("href")!);
    preview.searchParams.sort();
    open.searchParams.sort();
    expect(open.href).toBe(preview.href);
    const download = screen.getByRole("link", { name: "Download file" });
    expect(new URL(download.getAttribute("href")!).searchParams.has("preview")).toBe(false);
    expect(download).toHaveAttribute("download", "doc.pdf");
  });

  it("keeps explicit open and download when PDF rendering is unavailable", async () => {
    Object.defineProperty(navigator, "pdfViewerEnabled", { configurable: true, value: false });
    vi.stubGlobal("fetch", mockFetch({ kind: "pdf", mime_type: "application/pdf", size: 8000 }, "ignored"));
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "doc.pdf"));
    await screen.findByText(/PDF preview is unavailable/);
    expect(document.querySelector("iframe")).toBeNull();
    expect(screen.getByRole("link", { name: "Open in new tab" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Download file" })).toBeVisible();
  });

  it("renders <pre><code> for kind=code", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "code", mime_type: "text/plain; charset=utf-8", size: 14 },
        "def main():\n    pass\n"
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "main.py"));

    await waitFor(() => {
      const code = document.querySelector("pre code");
      expect(code).not.toBeNull();
      expect(code!.textContent).toContain("def main");
      expect(code!.className).toContain("language-python");
    });
  });

  it("renders plain <pre> for kind=text", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "text", mime_type: "text/plain; charset=utf-8", size: 5 },
        "logged"
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "out.log"));

    await waitFor(() => {
      // text body is a bare <pre> with text directly inside (no <code>)
      const pres = document.querySelectorAll("pre");
      const match = Array.from(pres).find(
        (p) => p.querySelector("code") === null && p.textContent?.includes("logged")
      );
      expect(match).toBeDefined();
    });
  });

  it("shows an error when the meta endpoint returns 404", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ detail: "File not found: nope.md" }), {
          status: 404,
          headers: { "content-type": "application/json" },
        })
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "nope.md"));

    await waitFor(() =>
      expect(screen.getByText(/Couldn't open file/i)).toBeInTheDocument()
    );
    expect(screen.getByText(/File not found/i)).toBeInTheDocument();
  });

  it("closes when the close button is clicked", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "text", mime_type: "text/plain; charset=utf-8", size: 1 },
        "x"
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "x.txt"));

    await waitFor(() => expect(document.querySelector("pre")).not.toBeNull());
    const closeBtn = screen.getByRole("button", { name: "Close" });
    act(() => closeBtn.click());
    expect(useSessionStore.getState().viewer).toBeNull();
  });
});

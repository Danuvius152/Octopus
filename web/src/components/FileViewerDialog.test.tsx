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
        const follow = await screen.findByRole("link", { name: "Follow" });
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
    fireEvent.load(document.querySelector("img")!);
    expect(screen.getByRole("img", { name: "shot.png" })).toBeVisible();
    expect(read).toHaveBeenCalledWith(expect.stringContaining("/files?path=shot.png"),
      expect.objectContaining({ headers: { Authorization: "Bearer tok" } }));
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(revoke).toHaveBeenCalledWith("blob:viewer-image");
  });

  it("requests explicit PDF preview and preserves the download URL", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "pdf", mime_type: "application/pdf", size: 8000 },
        "ignored"
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "doc.pdf"));

    await waitFor(() => {
      const iframe = document.querySelector("iframe");
      expect(iframe).not.toBeNull();
      expect(iframe!.getAttribute("src")).toContain("/files?path=doc.pdf");
      expect(new URL(iframe!.getAttribute("src")!).searchParams.get("preview")).toBe("true");
    });
    const frameUrl = new URL(document.querySelector("iframe")!.src);
    expect(frameUrl.searchParams.get("frame")).toBe("true");
    frameUrl.searchParams.delete("frame");
    expect(screen.getByRole("link", { name: "Open in new tab" })).toHaveAttribute("href", frameUrl.href);
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
    const closeBtn = screen.getByRole("button", { name: /close/i });
    act(() => closeBtn.click());
    expect(useSessionStore.getState().viewer).toBeNull();
  });
});
